"""冻结几何批量加速（批次 I / T16、T17）。

执行细则 9.1、任务书 11.1 的落地实现。核心口径（不得违反）：

* **保留逐脉冲 NumPy 路径为参考实现**；分组只做等价加速，不改物理。
* 每个脉冲**单独**计算非线性响应后相加：``Δh_block = Σ_j a(F_j)``。
  **禁止**先合并能流成 ``ΣF_j`` 再算一次对数——那会改变非线性响应。
* 事件块与空间块同时限额，**不构建全事件×全网格张量**。
* 局部误差估计（B 与两个 B/2 试算比较）只用于**控制步长**；
  最终验收以**完整逐脉冲对照**为准。
* 试算**不得**提前累计正式剂量、事件计数或快照；拒绝则不提交任何状态。
* B=1 直接使用参考（逐脉冲）更新；奇数块按两个尽量等长的子块处理。
* 分相、历史耦合、动态角度触发**回退**到参考实现，并保存原因。
* Numba 为可选依赖，缺失时回退 NumPy；**不得并行化依赖历史的事件轴**。

本模块只依赖 ``beam`` / ``response`` 的公开接口，不导入 Streamlit/Plotly
（分层约定见 ADR-0010）。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

import numpy as np

from .beam import BeamOptions, beam_patch
from .errors import CONFIG_INVALID, RESOURCE_BUDGET_EXCEEDED, UFDemoError

DEFAULT_LOCAL_REL_TOL = 1e-3
DEFAULT_BATCH_SIZE = 64
UINT32_MAX = 2**32 - 1


# ---------------------------------------------------------------------------
# 策略与只读几何视图
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchPolicy:
    """分组批量的步长控制参数（来自配置层，不在此处编造物理量）。"""

    batch_size: int = DEFAULT_BATCH_SIZE
    min_batch_size: int = 1
    local_rel_tol: float = DEFAULT_LOCAL_REL_TOL
    # 局部误差的绝对项（内部长度单位）。细则 10 节建议 ``0.01 * delta_test``，
    # 由调用方按算例显式给出；0 表示纯相对判据。
    local_abs_tol_internal: float = 0.0
    # 几何漂移辅判据：块内最大去除量与特征长度之比超过该值时直接缩小 B。
    geometry_drift_limit: float = 0.25
    # 单次空间窗格单元数上限（空间块限额）
    max_cell_block: int = 1 << 22

    def validate(self) -> None:
        if self.batch_size < 1:
            raise UFDemoError(CONFIG_INVALID, "batch_size 必须 >= 1", field_path="solver.batch_size", actual=self.batch_size)
        if not (1 <= self.min_batch_size <= self.batch_size):
            raise UFDemoError(
                CONFIG_INVALID,
                "min_batch_size 必须落在 [1, batch_size]",
                field_path="solver.min_batch_size",
                actual=self.min_batch_size,
                requirement=f"1 <= min_batch_size <= batch_size({self.batch_size})",
            )
        if not (0.0 < self.local_rel_tol < 1.0):
            raise UFDemoError(CONFIG_INVALID, "local_rel_tol 必须落在 (0,1)", field_path="solver.local_rel_tol", actual=self.local_rel_tol)
        if self.local_abs_tol_internal < 0.0:
            raise UFDemoError(CONFIG_INVALID, "local_abs_tol_internal 不得为负", field_path="solver.local_abs_tol_internal", actual=self.local_abs_tol_internal)
        if self.max_cell_block < 1:
            raise UFDemoError(CONFIG_INVALID, "max_cell_block 必须 >= 1", field_path="solver.max_cell_block", actual=self.max_cell_block)


@dataclass
class GeometryView:
    """冻结几何的只读视图。

    ``beam_patch`` 只读取 ``laser`` / ``grid`` / ``x`` / ``y`` /
    ``height`` / ``initial_height``；用本对象即可在**不改动正式表面**的前提下
    以指定高度场计算能流——这就是「冻结几何」的实现方式。
    """

    laser: Any
    grid: Any
    x: Any
    y: Any
    height: Any
    initial_height: Any

    @staticmethod
    def of(surface: Any, *, height: Any | None = None) -> "GeometryView":
        return GeometryView(
            laser=surface.laser,
            grid=surface.grid,
            x=surface.x,
            y=surface.y,
            height=surface.height if height is None else height,
            initial_height=surface.initial_height,
        )

    def with_height(self, height: Any) -> "GeometryView":
        return replace(self, height=height)


# ---------------------------------------------------------------------------
# 可选 Numba 局部核（T16）
# ---------------------------------------------------------------------------


@dataclass
class LocalKernel:
    """固定阈值对数核的局部数值实现。

    只编译**逐单元**的局部核（``a = delta*ln(F/Fth)``，先掩膜后取对数），
    不触碰事件轴，因此不存在跨事件依赖被并行化的问题（任务书 3.6、[R5]）。
    Numba 缺失时自动回退 NumPy，结果与 ``FixedThresholdLogLaw.increment`` 同式。
    """

    kind: str  # "numpy" | "numba"
    jit_time_s: float | None = None
    _impl: Any = None

    @staticmethod
    def build(prefer_numba: bool) -> "LocalKernel":
        if prefer_numba and numba_available():
            t0 = time.perf_counter()
            impl = _numba_log_kernel()
            impl(np.ones((1, 1), dtype=np.float64), 1.0, 1.0)  # 触发 JIT 编译
            return LocalKernel(kind="numba", jit_time_s=time.perf_counter() - t0, _impl=impl)
        return LocalKernel(kind="numpy", jit_time_s=None, _impl=None)

    def apply(self, fluence: Any, *, threshold_internal: float, delta_internal: float) -> np.ndarray:
        f = np.asarray(fluence, dtype=np.float64)
        if self._impl is not None:
            return self._impl(f, float(threshold_internal), float(delta_internal))
        mask = f > threshold_internal
        values = np.zeros_like(f)
        if np.any(mask):
            values[mask] = delta_internal * np.log(f[mask] / threshold_internal)
        return values


def _numba_log_kernel():
    """惰性构造 numba 局部核（njit，非 parallel）；只在 numba 可用时调用。"""
    from numba import njit

    @njit(cache=True, fastmath=False)
    def _kernel2d(f, threshold, delta):
        ny, nx = f.shape
        out = np.zeros((ny, nx), dtype=np.float64)
        for iy in range(ny):
            for ix in range(nx):
                fi = f[iy, ix]
                if fi > threshold:
                    out[iy, ix] = delta * math.log(fi / threshold)
        return out

    return _kernel2d


def numba_available() -> bool:
    """Numba 是可选依赖；缺失时回退 NumPy（任务书 8 节）。"""
    try:
        import numba  # noqa: F401

        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 块累计（构造它不改动任何状态）
# ---------------------------------------------------------------------------


@dataclass
class BlockAccumulation:
    """一个事件块在冻结几何下累计出的增量与统计。

    本对象**只是候选数据**：构造它不会改动 ``SurfaceState``；
    调用方在误差判据通过后才把 ``delta_h`` 提交给表面。
    """

    shape: tuple[int, int]
    delta_h: np.ndarray
    touch_counts: np.ndarray
    fluence_sum: np.ndarray
    illum_counts: np.ndarray
    n_events: int
    n_ablating_events: int
    emitted_energy_J: float
    estimated_intercepted_energy_J: float
    max_domain_truncated_fraction: float
    patch_cache_hits: int = 0
    n_patches: int = 0
    notes: list[str] = field(default_factory=list)
    # 受限阈值协议（批次 H）在批量块内的精确累计：每个事件的超阈判定仍只依据
    # **本事件入射能流**（冻结几何下即为该事件的 patch.fluence），与逐脉冲完全同口径。
    exceed_counts: np.ndarray | None = None
    exceed_or: np.ndarray | None = None

    @property
    def max_removal_internal(self) -> float:
        return float(np.max(self.delta_h)) if self.delta_h.size else 0.0

    def candidate_volume_internal(self, dx_m: float, dy_m: float) -> float:
        return float(np.sum(self.delta_h) * dx_m * dy_m)


def _blank_accumulation(shape: tuple[int, int]) -> BlockAccumulation:
    ny, nx = shape
    return BlockAccumulation(
        shape=(ny, nx),
        delta_h=np.zeros((ny, nx), dtype=np.float64),
        touch_counts=np.zeros((ny, nx), dtype=np.uint32),
        fluence_sum=np.zeros((ny, nx), dtype=np.float64),
        illum_counts=np.zeros((ny, nx), dtype=np.uint32),
        n_events=0,
        n_ablating_events=0,
        emitted_energy_J=0.0,
        estimated_intercepted_energy_J=0.0,
        max_domain_truncated_fraction=0.0,
    )


def accumulate_block(
    view: GeometryView,
    events: Sequence[Any],
    law: Any,
    *,
    kernel: LocalKernel | None = None,
    track_statistics: bool = True,
    max_cell_block: int = 1 << 22,
    threshold_protocol: Any = None,
) -> BlockAccumulation:
    """在**冻结几何**下累计一个事件块的候选去除量（不提交任何状态）。

    * 几何：整块使用 ``view.height``（调用方传入块起点高度）；
    * 非线性：每个事件**各自**调用响应核 ``a(F_j)`` 后相加，绝不先累加能流；
    * 相同焦点的连续脉冲（定点多脉冲）命中补丁缓存，只算一次光束；
    * ``track_statistics=False`` 时只算 ``delta_h``（供误差试算省内存）；
    * ``threshold_protocol`` 非空时，逐事件按**本事件入射能流**累计超阈观测量。
    """
    ny, nx = int(view.grid.ny), int(view.grid.nx)
    acc = _blank_accumulation((ny, nx))
    delta_h = acc.delta_h

    use_threshold = threshold_protocol is not None and bool(getattr(threshold_protocol, "available", False))
    if use_threshold:
        acc.exceed_counts = np.zeros((ny, nx), dtype=np.uint32)
        acc.exceed_or = np.zeros((ny, nx), dtype=bool)

    thr = float(law.threshold_internal)
    delta = float(law.delta_internal)
    opt = BeamOptions(geometry_feedback="fixed_geometry")

    cache_key: tuple[Any, ...] | None = None
    cached_patch: Any = None

    for ev in events:
        key = (ev.focus_xyz_m, float(ev.energy_J))
        if cache_key is not None and key == cache_key:
            patch = cached_patch
            acc.patch_cache_hits += 1
        else:
            patch = beam_patch(ev, view, opt)
            cache_key, cached_patch = key, patch
            acc.n_patches += 1

        acc.emitted_energy_J += float(patch.emitted_energy_J)
        acc.estimated_intercepted_energy_J += float(patch.estimated_intercepted_energy_J)
        acc.max_domain_truncated_fraction = max(
            acc.max_domain_truncated_fraction, float(patch.domain_truncated_fraction)
        )
        for note in patch.notes:
            if note not in acc.notes:
                acc.notes.append(note)

        if patch.empty:
            continue

        iy0, iy1, ix0, ix1 = patch.iy0, patch.iy1, patch.ix0, patch.ix1
        n_cells = (iy1 - iy0) * (ix1 - ix0)
        if n_cells > int(max_cell_block):
            raise UFDemoError(
                RESOURCE_BUDGET_EXCEEDED,
                "单事件空间窗格超过空间块限额",
                field_path="solver.acceleration",
                actual=n_cells,
                requirement=f"<= max_cell_block ({max_cell_block})",
                suggestion="提高网格间距、缩小计算域，或调大 max_cell_block。",
            )

        win = (slice(iy0, iy1), slice(ix0, ix1))

        # 受限阈值协议：只喂**本事件入射能流**（冻结几何下即 patch.fluence）
        if use_threshold:
            from .thresholds import classify_exceedance

            exceed = classify_exceedance(threshold_protocol, patch.fluence, patch.mask)
            if exceed is not None and exceed.size:
                assert acc.exceed_counts is not None and acc.exceed_or is not None
                acc.exceed_counts[win] += exceed.astype(np.uint32)
                acc.exceed_or[win] |= exceed

        # 每个事件**单独**计算非线性响应（红线：不得先累加能流再取一次对数）
        if kernel is not None:
            vals = kernel.apply(patch.fluence, threshold_internal=thr, delta_internal=delta)
        else:
            f = np.asarray(patch.fluence, dtype=np.float64)
            m = f > thr
            vals = np.zeros_like(f)
            if np.any(m):
                vals[m] = delta * np.log(f[m] / thr)
        vals = np.where(patch.mask, vals, 0.0)

        delta_h[win] += vals
        touched = vals > 0.0
        if touched.any():
            acc.n_ablating_events += 1
            if track_statistics:
                if int(acc.touch_counts[win][touched].max()) >= UINT32_MAX:
                    raise UFDemoError(
                        RESOURCE_BUDGET_EXCEEDED,
                        "受照计数溢出 uint32（批量块）",
                        field_path="surface.exposure_count",
                        actual=UINT32_MAX,
                        requirement="计数 < 2^32",
                    )
                acc.touch_counts[win] += touched.astype(np.uint32)
        if track_statistics:
            m = np.asarray(patch.mask, dtype=bool)
            if m.any():
                acc.fluence_sum[win] += np.where(m, patch.fluence, 0.0)
                acc.illum_counts[win] += m.astype(np.uint32)

    acc.n_events = len(events)
    return acc


# ---------------------------------------------------------------------------
# 局部误差估计（步长控制）
# ---------------------------------------------------------------------------


@dataclass
class ErrorEstimate:
    """一次「B 与两个 B/2」试算的差异（细则 9.1 的局部误差估计）。"""

    batch_size: int
    half_size: int
    max_abs_internal: float
    rel_l2: float
    volume_abs_internal: float
    ok: bool
    rel_tol: float
    abs_tol_internal: float
    reason: str | None = None
    geometry_drift: float = 0.0
    drift_limit: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_size": self.batch_size,
            "half_size": self.half_size,
            "max_abs_internal": self.max_abs_internal,
            "rel_l2": self.rel_l2,
            "volume_abs_internal": self.volume_abs_internal,
            "ok": self.ok,
            "rel_tol": self.rel_tol,
            "abs_tol_internal": self.abs_tol_internal,
            "reason": self.reason,
            "geometry_drift": self.geometry_drift,
            "drift_limit": self.drift_limit,
        }


def _rel_l2(a: np.ndarray, b: np.ndarray) -> float:
    num = float(np.linalg.norm((a - b).ravel()))
    den = float(np.linalg.norm(b.ravel()))
    return num / den if den > 0.0 else num


def drift_reference_for(config: Any) -> float | None:
    """几何漂移判据的参考长度；``fixed_geometry`` 下**不存在**漂移，返回 ``None``。

    语义要点：``beam_patch`` 在 ``fixed_geometry`` 下用 ``initial_height`` 算离焦，
    与当前高度**无关**——因此块内去除多少都不会改变后续能流，冻结几何是**精确**的，
    漂移判据不适用。只有在 ``axial_defocus``（用当前高度算离焦）下，块内高度变化
    才会真正影响后续脉冲的能流，此时以离焦尺度（``zR``，缺省 ``w0``）作为参考长度。
    """
    if getattr(config.solver, "geometry_feedback", "fixed_geometry") == "fixed_geometry":
        return None
    zr = getattr(config.laser, "rayleigh_range_m", None)
    if zr:
        return float(zr)
    return float(config.laser.spot_radius_m)


def estimate_local_error(
    view: GeometryView,
    events: Sequence[Any],
    law: Any,
    *,
    kernel: LocalKernel | None = None,
    rel_tol: float = DEFAULT_LOCAL_REL_TOL,
    abs_tol_internal: float = 0.0,
    drift_reference_internal: float | None = None,
    drift_limit: float = 0.25,
    threshold_protocol: Any = None,
    max_cell_block: int = 1 << 22,
) -> tuple[ErrorEstimate, BlockAccumulation]:
    """比较「整块一步」与「两个半步」的更新场，作为局部步长误差。

    两个半步：先用块起点几何算前半块，再以前半块结束后的几何算后半块。
    奇数的块按两个**尽量等长**的子块切分（细则 9.1）。
    返回 ``(误差估计, 整块累计结果)``。B=1 时不做估计（交由参考更新）。
    """
    n = len(events)
    if n <= 1:
        full = accumulate_block(
            view, events, law, kernel=kernel, threshold_protocol=threshold_protocol, max_cell_block=max_cell_block
        )
        est = ErrorEstimate(
            batch_size=n, half_size=n, max_abs_internal=0.0, rel_l2=0.0,
            volume_abs_internal=0.0, ok=True, rel_tol=rel_tol, abs_tol_internal=abs_tol_internal,
            reason="batch_size<=1：使用参考（逐脉冲）更新，无需局部误差估计",
        )
        return est, full

    # 几何与当前高度**无关**时（``fixed_geometry``：光束用 initial_height 算离焦），
    # 块内「一步」与「两个半步」在数学上恒等（半步的中间几何不参与能流计算），
    # 因此无需付出半步试算的额外开销——这是严格等价的优化，不是降低校验强度。
    # 调用方用 ``drift_reference_internal=None`` 表示这一情形（见 drift_reference_for）。
    if drift_reference_internal is None:
        full = accumulate_block(
            view, events, law, kernel=kernel, track_statistics=True,
            threshold_protocol=threshold_protocol, max_cell_block=max_cell_block,
        )
        est = ErrorEstimate(
            batch_size=n, half_size=(n + 1) // 2 if n % 2 else n // 2,
            max_abs_internal=0.0, rel_l2=0.0, volume_abs_internal=0.0,
            ok=True, rel_tol=rel_tol, abs_tol_internal=abs_tol_internal,
            reason=(
                "几何与当前高度无关（fixed_geometry）：能流只用初始面计算，"
                "块内一步与两个半步恒等，已跳过半步试算（严格等价，非降低校验）。"
            ),
            geometry_drift=0.0, drift_limit=drift_limit,
        )
        return est, full

    full = accumulate_block(
        view, events, law, kernel=kernel, track_statistics=True,
        threshold_protocol=threshold_protocol, max_cell_block=max_cell_block,
    )

    half = (n + 1) // 2 if n % 2 else n // 2  # 奇数块：前半取较大的一半
    acc_a = accumulate_block(
        view, events[:half], law, kernel=kernel, track_statistics=False, max_cell_block=max_cell_block
    )
    mid_view = view.with_height(view.height - acc_a.delta_h)
    acc_b = accumulate_block(
        mid_view, events[half:], law, kernel=kernel, track_statistics=False, max_cell_block=max_cell_block
    )

    two_half = acc_a.delta_h + acc_b.delta_h
    diff = full.delta_h - two_half

    dx, dy = float(view.grid.dx_m), float(view.grid.dy_m)
    vol_diff = abs(float(np.sum(diff)) * dx * dy)
    max_abs = float(np.max(np.abs(diff))) if diff.size else 0.0
    rel_l2 = _rel_l2(full.delta_h, two_half)

    drift = 0.0
    drift_ok = True
    if drift_reference_internal and drift_reference_internal > 0.0:
        drift = full.max_removal_internal / float(drift_reference_internal)
        drift_ok = drift <= drift_limit

    scale = float(np.max(np.abs(two_half))) if two_half.size else 0.0
    tol = rel_tol * scale + abs_tol_internal
    ok = (max_abs <= tol) and drift_ok

    reason = None
    if not ok:
        if not drift_ok:
            reason = (
                f"几何漂移过大：块内最大去除量/特征长度 = {drift:.4g} > {drift_limit:.4g}；"
                "冻结几何在该块内不再成立，缩小 batch_size 重试。"
            )
        else:
            reason = (
                f"局部误差 {max_abs:.6g} 超过容差 {tol:.6g}"
                f"（rel_tol={rel_tol:g}×尺度 {scale:.6g} + abs {abs_tol_internal:.6g}）。"
            )

    est = ErrorEstimate(
        batch_size=n, half_size=half, max_abs_internal=max_abs, rel_l2=rel_l2,
        volume_abs_internal=vol_diff, ok=ok, rel_tol=rel_tol,
        abs_tol_internal=abs_tol_internal, reason=reason,
        geometry_drift=drift, drift_limit=drift_limit,
    )
    return est, full


# ---------------------------------------------------------------------------
# 回退判据（细则 9.1 末）
# ---------------------------------------------------------------------------


@dataclass
class FallbackDecision:
    use_batch: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"use_batch": self.use_batch, "reason": self.reason}


def check_fallback_conditions(
    *,
    structured: bool,
    history_enabled: bool,
    geometry_feedback: str,
    dynamic_angle: bool = False,
) -> FallbackDecision:
    """判定当前工况是否**禁止**使用批量模式；触发即回退参考实现并保存原因。"""
    if structured:
        return FallbackDecision(
            False,
            "分相结构（含相界面截断）不在批量第一版支持范围内：相标签会随去除更新，"
            "冻结几何无法表达跨相界面截断。已回退逐脉冲参考实现。",
        )
    if history_enabled:
        return FallbackDecision(
            False,
            "启用了历史耦合的响应核不在批量第一版支持范围内：批量不得改变事件顺序依赖。"
            "已回退逐脉冲参考实现。",
        )
    if dynamic_angle:
        return FallbackDecision(
            False,
            "「批量 + 动态角度」组合在第一版明确不开放（细则 9.1 末）。已回退逐脉冲参考实现。",
        )
    if geometry_feedback not in ("fixed_geometry", "axial_defocus"):
        return FallbackDecision(
            False,
            f"几何反馈模式 {geometry_feedback!r} 未被批量路径支持。已回退逐脉冲参考实现。",
        )
    return FallbackDecision(True, None)


def grouped_solve(**kwargs: Any) -> FallbackDecision:
    """分组批量的**工况判定**入口（能否走批量；不能则给出回退原因）。"""
    return check_fallback_conditions(
        structured=bool(kwargs.get("structured", False)),
        history_enabled=bool(kwargs.get("history_enabled", False)),
        geometry_feedback=str(kwargs.get("geometry_feedback", "fixed_geometry")),
        dynamic_angle=bool(kwargs.get("dynamic_angle", False)),
    )


# ---------------------------------------------------------------------------
# 入口：块求解（试算 → 接受/缩小 → 返回候选数据）
# ---------------------------------------------------------------------------


@dataclass
class BlockPlan:
    """一次被接受的块及其诊断（供求解器提交状态）。"""

    events: list[Any]
    accumulation: BlockAccumulation
    estimate: ErrorEstimate
    n_rejected: int
    total_trials: int
    fallback_reason: str | None = None

    @property
    def batch_size(self) -> int:
        return len(self.events)


def solve_block(
    view: GeometryView,
    events: Sequence[Any],
    law: Any,
    *,
    policy: BatchPolicy,
    kernel: LocalKernel | None = None,
    drift_reference_internal: float | None = None,
    threshold_protocol: Any = None,
) -> BlockPlan:
    """对给定事件序列做「试算 → 接受/缩小 → 返回候选」。

    **只返回候选数据**，不修改任何表面状态；试算过程中不累计正式剂量、
    事件计数或快照（细则 9.1）。
    """
    policy.validate()
    pending = list(events)
    n_rejected = 0
    trials = 0
    fallback_reason: str | None = None
    current_b = min(len(pending), policy.batch_size)

    def _reference_chunk(chunk: list[Any], *, note: str) -> BlockPlan:
        acc = accumulate_block(
            view, chunk, law, kernel=kernel,
            threshold_protocol=threshold_protocol, max_cell_block=policy.max_cell_block,
        )
        est = ErrorEstimate(
            batch_size=len(chunk), half_size=len(chunk), max_abs_internal=0.0,
            rel_l2=0.0, volume_abs_internal=0.0, ok=True,
            rel_tol=policy.local_rel_tol, abs_tol_internal=policy.local_abs_tol_internal,
            reason=note,
        )
        return BlockPlan(chunk, acc, est, n_rejected, trials, fallback_reason)

    while True:
        b = current_b
        if b <= 1:
            return _reference_chunk(pending[:1], note="batch_size=1：参考（逐脉冲）更新")

        chunk = pending[:b]
        trials += 1
        est, acc = estimate_local_error(
            view, chunk, law, kernel=kernel,
            rel_tol=policy.local_rel_tol,
            abs_tol_internal=policy.local_abs_tol_internal,
            drift_reference_internal=drift_reference_internal,
            drift_limit=policy.geometry_drift_limit,
            threshold_protocol=threshold_protocol,
            max_cell_block=policy.max_cell_block,
        )
        if est.ok:
            return BlockPlan(chunk, acc, est, n_rejected, trials, fallback_reason)

        n_rejected += 1
        fallback_reason = est.reason
        next_b = max(policy.min_batch_size, b // 2)
        if next_b >= b:
            next_b = 1
        if next_b <= 1:
            return _reference_chunk(pending[:1], note="缩小到 B=1：使用参考（逐脉冲）更新")
        current_b = next_b
        # 继续用更小的 B 重试（不提交任何状态）
