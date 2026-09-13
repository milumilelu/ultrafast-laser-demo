"""合成相结构：铺层与颗粒、相查询与跨相界面（批次 G / T11–T13）。

依据任务书 3.3、8 节与执行细则第 8 节：

* 结构接口至少提供 ``phase_at(x, y, z)`` 与 ``next_different_interface(x, y, z)``；
* 初版采用**解析**颗粒/纤维与沿 z 的分段相列，不默认创建高分辨率三维体素场；
* 生成器保存种子、算法版本、生成参数、实际体积分数及其计算/估计方法；
  有限样本不强制等于目标比例（45% 不等于随机样本必然 45%）。

跨相截断的红线（§8 界面更新规则 1–5）：

1. 当前脉冲只调用当前暴露相的响应；
2. 深度与「到下一不同相界面的距离」比较，取较小者；
3. 达到界面后更新相标签，**下一真实脉冲**才对新相响应；
4. 记录截断事件数、单元次数与「未应用候选去除体积」——**不叫剩余能量**；
5. 同相相邻区间预先合并；接触界面使用统一浮点容差，防止卡在零厚层。

另外两条"不得"（执行细则 7 节表）：

* 不得把块体单晶 SiC 卡当作复合材料颗粒相的标定（相响应必须内联自带来源）；
* 不得把整体等效阈值拆给树脂/纤维两相（相响应不得复用整体阈值）。

本模块只做结构几何与相标签，**不做能量传输建模**：被截断的候选量是
**有损近似**的记录，不是界面能量传输结果。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .errors import (
    CONDITION_MISMATCH,
    CONFIG_INVALID,
    MATERIAL_CAPABILITY_MISSING,
    NUMERIC_NONFINITE,
    RESPONSE_SEMANTICS_INVALID,
    UFDemoError,
)

# ---------------------------------------------------------------------------
# 枚举与常量
# ---------------------------------------------------------------------------

STRUCTURE_TYPES: tuple[str, ...] = (
    "homogeneous",
    "particle_composite",
    "laminated_fiber_composite",
)

# 相响应的来源。本批只开放**内联合成定义**：相响应必须自带，
# 不允许指向别的材料卡（这就是"不得借用块体 4H-SiC 卡当颗粒相标定"的落地方式）。
PHASE_SOURCES: tuple[str, ...] = ("synthetic_definition",)

PHASE_ROLES: tuple[str, ...] = ("matrix", "particle", "fiber", "layer", "substrate")

STRUCTURE_ALGORITHM_VERSION = "structure_v1"

# 接触界面的统一浮点容差（相对总厚度）。取"到达界面即算进入下一相"，
# 避免恰好落到边界时被反复判为同一相而卡在零厚层。
CONTACT_TOL_REL = 1e-9

# 相响应里禁止沿用的整体阈值来源标记（细则 7 节表：CFRP 整体阈值不是两相阈值）
FORBIDDEN_PHASE_RESPONSE_SOURCES: tuple[str, ...] = (
    "overall_protocol_threshold",
    "parent_card_threshold",
    "borrowed_material_card",
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _broadcast_xyz(x: Any, y: Any, z: Any):
    """把 ``x``/``y``/``z``（标量或数组）广播到同一形状，返回 float64 三元组。"""
    import numpy as np

    xx = np.asarray(x, dtype=np.float64)
    yy = np.asarray(y, dtype=np.float64)
    zz = np.asarray(z, dtype=np.float64)
    shape = np.broadcast_shapes(xx.shape, yy.shape, zz.shape)
    return (
        np.broadcast_to(xx, shape),
        np.broadcast_to(yy, shape),
        np.broadcast_to(zz, shape),
    )


# ---------------------------------------------------------------------------
# 相
# ---------------------------------------------------------------------------


@dataclass
class PhaseSpec:
    """一个相的内联响应与身份。

    相响应**自带**阈值与去除尺度；不允许从父材料卡或其它材料卡"借"参数。
    """

    name: str
    phase_id: int
    role: str
    threshold_internal: float
    delta_internal: float
    depth_direction: str = "surface_normal"
    output_semantics: str = "event_depth_increment"
    response_source: str = "synthetic_definition"
    unit_mode: str = "dimensionless"
    source_equation: str | None = None
    material_id: str | None = None
    note: str = ""

    def fingerprint(self) -> tuple[Any, ...]:
        return (self.phase_id, self.threshold_internal, self.delta_internal, self.depth_direction)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "phase_id": self.phase_id,
            "role": self.role,
            "threshold_internal": self.threshold_internal,
            "delta_internal": self.delta_internal,
            "depth_direction": self.depth_direction,
            "output_semantics": self.output_semantics,
            "response_source": self.response_source,
            "unit_mode": self.unit_mode,
            "source_equation": self.source_equation,
            "material_id": self.material_id,
            "note": self.note,
        }


def build_phase(raw: Mapping[str, Any], *, phase_id: int, unit: Any, source_hint: str = "structure") -> PhaseSpec:
    """由配置字典构造相。缺失/非法字段直接拒绝，不补近似值。"""
    if not isinstance(raw, Mapping):
        raise UFDemoError(
            CONFIG_INVALID,
            "相定义必须是 JSON 对象",
            field_path=f"{source_hint}.phases",
            actual=type(raw).__name__,
        )
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise UFDemoError(CONFIG_INVALID, "相缺少 name", field_path=f"{source_hint}.phases[].name", actual=name)
    role = str(raw.get("role", "layer"))
    if role not in PHASE_ROLES:
        raise UFDemoError(
            CONFIG_INVALID,
            "相 role 非法",
            field_path=f"{source_hint}.phases[{name}].role",
            actual=role,
            requirement=f"取值属于 {list(PHASE_ROLES)}",
        )
    response_source = str(raw.get("response_source", "synthetic_definition"))
    if response_source in FORBIDDEN_PHASE_RESPONSE_SOURCES:
        raise UFDemoError(
            MATERIAL_CAPABILITY_MISSING,
            f"相 {name!r} 的 response_source={response_source!r} 不被允许",
            field_path=f"{source_hint}.phases[{name}].response_source",
            actual=response_source,
            requirement=(
                "相响应必须内联自带（synthetic_definition）；"
                "不得把父材料卡的整体等效阈值拆给分相，也不得借用其它材料卡"
            ),
            suggestion=(
                "CFRP 的整体阈值（如 Fth1）不是树脂/纤维分别的阈值；"
                "分相阈值需各自标定后再填入。"
            ),
        )

    # "不得把块体 4H-SiC 卡当成颗粒相标定"：本批只接受内联合成定义。
    material_id = raw.get("material_id")
    if material_id not in (None, ""):
        raise UFDemoError(
            MATERIAL_CAPABILITY_MISSING,
            f"相 {name!r} 不能引用其它材料卡（material_id={material_id!r}）",
            field_path=f"{source_hint}.phases[{name}].material_id",
            actual=material_id,
            requirement="相响应必须内联自带（response_source=synthetic_definition，material_id=null）",
            suggestion=(
                "不得把块体单晶 4H-SiC 卡当作铝基复合材料中颗粒相的标定，"
                "也不得把 YSZ 加工支路阈值套给其它相；"
                "该相缺标定时保持合成模式，不输出物理深度。"
            ),
        )

    thr = raw.get("threshold_over_F_ref", raw.get("threshold_internal"))
    delta = raw.get("delta_over_L_ref", raw.get("delta_internal"))
    if raw.get("delta_over_delta_ref") is not None and delta is None:
        raise UFDemoError(
            RESPONSE_SEMANTICS_INVALID,
            f"相 {name!r} 使用了旧字段 delta_over_delta_ref",
            field_path=f"{source_hint}.phases[{name}].delta_over_delta_ref",
            actual=raw.get("delta_over_delta_ref"),
            requirement="改用 delta_over_L_ref（δ/L_ref，与层厚/光斑同一尺度）",
            suggestion="δ/L_ref 与 δ/delta_ref 相差 L_ref/delta_ref 倍（本工程默认 100 倍）。",
        )
    for label, value in (("threshold", thr), ("delta", delta)):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or not (value > 0)
        ):
            raise UFDemoError(
                RESPONSE_SEMANTICS_INVALID,
                f"相 {name!r} 的 {label} 必须是有限正值",
                field_path=f"{source_hint}.phases[{name}].{label}",
                actual=value,
                requirement="有限正数；缺失时不得以 0 或相近材料值代替",
                suggestion="补齐该相自己的阈值与去除尺度；不得由阈值反推物理深度。",
            )

    semantics = str(raw.get("output_semantics", "event_depth_increment"))
    if semantics != "event_depth_increment":
        raise UFDemoError(
            RESPONSE_SEMANTICS_INVALID,
            f"相 {name!r} 的 output_semantics={semantics!r} 不能进入逐事件核",
            field_path=f"{source_hint}.phases[{name}].output_semantics",
            actual=semantics,
            requirement="event_depth_increment",
            suggestion="平均率/累计/体积语义的相只进评估器，不参与逐事件更新。",
        )

    depth_direction = str(raw.get("depth_direction", "surface_normal"))
    if not depth_direction:
        raise UFDemoError(
            RESPONSE_SEMANTICS_INVALID,
            f"相 {name!r} 缺少 depth_direction",
            field_path=f"{source_hint}.phases[{name}].depth_direction",
            actual=None,
            requirement="surface_normal / vertical_height",
        )

    override = raw.get("phase_id")
    if override is not None:
        if not isinstance(override, int) or isinstance(override, bool) or not (0 < override < 65535):
            raise UFDemoError(
                CONFIG_INVALID,
                f"相 {name!r} 的 phase_id 必须是 1..65534 的整数",
                field_path=f"{source_hint}.phases[{name}].phase_id",
                actual=override,
                requirement="1..65534（0 保留为未定义）",
            )
        phase_id = int(override)

    return PhaseSpec(
        name=name,
        phase_id=int(phase_id),
        role=role,
        threshold_internal=float(thr),
        delta_internal=float(delta),
        depth_direction=depth_direction,
        output_semantics=semantics,
        response_source=response_source,
        unit_mode=str(getattr(unit, "mode", "dimensionless")),
        source_equation=raw.get("source_equation"),
        material_id=None,
        note=str(raw.get("note", "")),
    )


def assert_phase_threshold_not_split(phase: PhaseSpec, parent_material: Any, unit: Any = None) -> None:
    """红线：**整体等效阈值不得拆给分相**（细则 7 节表 CFRP 行）。

    判定：相阈值与父卡整体阈值相等（相对容差 1e-9）时视为"直接沿用整体阈值"。

    父卡阈值必须先换到**与相阈值同一尺度**再比：卡上的 ``threshold_J_m2`` 是物理
    量流，而无量纲配置下相阈值以 ``F_ref`` 为单位，直接比较会永远判为"不相等"，
    使这条红线形同虚设。合成卡（``threshold_over_F_ref``）本就是归一量，无需换算。
    """
    r = dict(getattr(parent_material, "response", {}) or {})
    raw_resp = dict((getattr(parent_material, "raw", {}) or {}).get("response", {}) or {})
    if raw_resp.get("threshold_over_F_ref") is not None:
        overall: Any = raw_resp.get("threshold_over_F_ref")
    else:
        overall = r.get("threshold_J_m2", r.get("threshold_internal"))
        if overall is not None and unit is not None and getattr(unit, "mode", "SI") != "SI":
            f_ref = getattr(unit, "F_ref_J_m2", None)
            if f_ref:
                overall = float(overall) / float(f_ref)
    if overall is None or not (isinstance(overall, (int, float)) and overall > 0):
        return
    if abs(phase.threshold_internal - float(overall)) / abs(float(overall)) <= 1e-9:
        raise UFDemoError(
            MATERIAL_CAPABILITY_MISSING,
            f"相 {phase.name!r} 的阈值与父卡整体阈值相同（{overall!r}）",
            field_path=f"structure.phases[{phase.name}].threshold",
            actual=phase.threshold_internal,
            requirement="分相阈值必须各自标定，不能复用整体等效阈值",
            suggestion=(
                "整体等效参数（如 CFRP 的 Fth1）不是树脂与纤维分别的参数；"
                "不要把它拆给两相。缺分相标定时只做合成结构展示，不输出物理深度。"
            ),
        )


# ---------------------------------------------------------------------------
# 结构基类
# ---------------------------------------------------------------------------


class Structure:
    """相结构基类。

    ``phase_at`` 与 ``next_different_interface`` 都要支持标量与数组输入；
    ``phase_id`` 语义与 :class:`ufdemo.surface.SurfaceState` 一致（uint16）。
    """

    is_uniform: bool = True
    structure_type: str = "homogeneous"
    algorithm_version: str = STRUCTURE_ALGORITHM_VERSION

    def __init__(self, phases: Sequence[PhaseSpec], *, seed: int = 0, target_volume_fraction: float | None = None) -> None:
        if not phases:
            raise UFDemoError(CONFIG_INVALID, "相结构至少要有一个相", field_path="structure.phases")
        ids = [p.phase_id for p in phases]
        if len(set(ids)) != len(ids):
            raise UFDemoError(CONFIG_INVALID, "相 phase_id 重复", field_path="structure.phases[].phase_id", actual=ids)
        self.phases: list[PhaseSpec] = list(phases)
        self.seed = int(seed)
        self.target_volume_fraction = target_volume_fraction
        self.phase_by_id: dict[int, PhaseSpec] = {p.phase_id: p for p in self.phases}
        self.phase_by_name: dict[str, PhaseSpec] = {p.name: p for p in self.phases}
        self.warnings: list[str] = []
        self.notes: list[str] = []

    # -- 通用 --------------------------------------------------------------
    def phase_name(self, pid: int) -> str:
        p = self.phase_by_id.get(int(pid))
        return p.name if p is not None else f"phase_{pid}"

    def laws(self, *, unit_mode: str | None = None) -> dict[int, Any]:
        """每个相的逐事件响应核。"""
        from .response import FixedThresholdLogLaw

        out: dict[int, Any] = {}
        for p in self.phases:
            out[p.phase_id] = FixedThresholdLogLaw(
                threshold_internal=p.threshold_internal,
                delta_internal=p.delta_internal,
                depth_direction=p.depth_direction,
                output_semantics=p.output_semantics,
                unit_mode=unit_mode or p.unit_mode,
                source_equation=p.source_equation,
            )
        return out

    def phase_id_of(self, name: str) -> int:
        p = self.phase_by_name.get(name)
        if p is None:
            raise UFDemoError(
                CONFIG_INVALID,
                f"结构中不存在相 {name!r}",
                field_path="structure.layers[].matrix_phase",
                actual=name,
                requirement=f"已定义相：{sorted(self.phase_by_name)}",
            )
        return p.phase_id

    # -- 接口（子类实现）---------------------------------------------------
    def phase_at(self, x: Any, y: Any, z: Any):  # pragma: no cover - 抽象
        raise NotImplementedError

    def next_different_interface(self, x: Any, y: Any, z: Any):  # pragma: no cover - 抽象
        raise NotImplementedError

    # -- 统计与报告 --------------------------------------------------------
    def phase_cell_counts(self, phase_ids: Any) -> dict[str, int]:
        import numpy as np

        arr = np.asarray(phase_ids)
        out: dict[str, int] = {}
        for p in self.phases:
            out[p.name] = int(np.count_nonzero(arr == p.phase_id))
        return out

    def volume_fraction_report(self, sample_x: Any = None, sample_y: Any = None, sample_z: Any = None) -> dict[str, Any]:
        """目标与实际体积分数的**分别**报告（任务书 G06：有限样本不强制等于目标）。"""
        actual = None
        method = "not_estimated"
        n_samples = 0
        if sample_x is not None and sample_y is not None and sample_z is not None:
            import numpy as np

            xs = np.asarray(sample_x, dtype=np.float64).ravel()
            ys = np.asarray(sample_y, dtype=np.float64).ravel()
            zs = np.asarray(sample_z, dtype=np.float64).ravel()
            n = min(xs.size, ys.size, zs.size)
            pids = np.asarray(self.phase_at(xs[:n], ys[:n], zs[:n]))
            n_samples = int(n)
            counted = {p.name: int(np.count_nonzero(pids == p.phase_id)) for p in self.phases}
            actual = {k: (v / n if n else None) for k, v in counted.items()}
            method = "grid_sample"
        return {
            "target_volume_fraction": self.target_volume_fraction,
            "actual_volume_fraction": actual,
            "volume_fraction_method": method,
            "n_samples": n_samples,
            "note": (
                "目标比例不代表有限随机样本必然正好等于该值；实际值与目标值分别报告。"
            ),
        }

    def summary(self) -> dict[str, Any]:
        base = {
            "structure_type": self.structure_type,
            "algorithm_version": self.algorithm_version,
            "seed": self.seed,
            "target_volume_fraction": self.target_volume_fraction,
            "n_phases": len(self.phases),
            "phases": [p.to_dict() for p in self.phases],
            "is_uniform": self.is_uniform,
            "notes": list(self.notes),
            "warnings": list(self.warnings),
            "truncation_note": (
                "跨相截断是有损近似：被截断的候选量记为「未应用候选去除体积」，"
                "不是剩余热量，也不是界面能量传输结果。"
            ),
        }
        base.update(self._extra_summary())
        return base

    def _extra_summary(self) -> dict[str, Any]:
        return {}


# ---------------------------------------------------------------------------
# 均质（单相）
# ---------------------------------------------------------------------------


class HomogeneousStructure(Structure):
    """单相均质结构。``phase_at`` 恒为 0，``next_different_interface`` 为 ``inf``。"""

    is_uniform = True
    structure_type = "homogeneous"

    def __init__(self, phases: Sequence[PhaseSpec] | None = None) -> None:
        super().__init__(phases or [PhaseSpec(name="homogeneous", phase_id=0, role="layer", threshold_internal=1.0, delta_internal=1.0)])

    def phase_at(self, x: Any, y: Any, z: Any):
        import numpy as np

        if np.isscalar(x):
            return 0
        return np.zeros(np.asarray(x).shape, dtype=np.uint16)

    def next_different_interface(self, x: Any, y: Any, z: Any):
        import numpy as np

        if np.isscalar(x):
            return float("inf")
        return np.full(np.asarray(x).shape, np.inf, dtype=np.float64)


# ---------------------------------------------------------------------------
# 铺层（含铺层角与层厚）
# ---------------------------------------------------------------------------


@dataclass
class PlyLayer:
    """一层铺层几何（已合并同相相邻区间后）。"""

    thickness: float
    matrix_phase: str
    fiber_phase: str | None = None
    fiber_angle_deg: float = 0.0
    fiber_volume_fraction: float | None = None
    fiber_width: float | None = None
    n_merged_from: int = 1

    def fingerprint(self) -> tuple[Any, ...]:
        return (
            self.matrix_phase,
            self.fiber_phase,
            float(self.fiber_angle_deg),
            None if self.fiber_volume_fraction is None else float(self.fiber_volume_fraction),
            None if self.fiber_width is None else float(self.fiber_width),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thickness": self.thickness,
            "matrix_phase": self.matrix_phase,
            "fiber_phase": self.fiber_phase,
            "fiber_angle_deg": self.fiber_angle_deg,
            "fiber_volume_fraction": self.fiber_volume_fraction,
            "fiber_width": self.fiber_width,
            "n_merged_from": self.n_merged_from,
        }


class LayeredStructure(Structure):
    """沿 z 的分段相列；铺层可带纤维方向与纤维体积分数。

    * 层由**初始表面**向下堆叠（``z_top`` 为初始高度）；
    * 纤维以**面内条纹**表示，条纹方向 = 铺层角，沿 z 不变，
      因此下一不同相界面就是本层层底（解析、精确）；
    * 相邻且指纹相同的层**预先合并**，禁止仅因人为层边界截断。
    """

    is_uniform = False
    structure_type = "laminated_fiber_composite"

    def __init__(
        self,
        layers: Sequence[PlyLayer],
        phases: Sequence[PhaseSpec],
        *,
        z_top: float = 0.0,
        seed: int = 0,
        target_volume_fraction: float | None = None,
    ) -> None:
        super().__init__(phases, seed=seed, target_volume_fraction=target_volume_fraction)
        if not layers:
            raise UFDemoError(CONFIG_INVALID, "铺层结构至少需要一层", field_path="structure.layers")
        for ly in layers:
            if not (ly.thickness > 0):
                raise UFDemoError(
                    CONFIG_INVALID,
                    "铺层厚度必须为正",
                    field_path="structure.layers[].thickness_m",
                    actual=ly.thickness,
                )
            if ly.matrix_phase not in self.phase_by_name:
                raise UFDemoError(CONFIG_INVALID, f"未定义相 {ly.matrix_phase!r}", field_path="structure.layers[].matrix_phase", actual=ly.matrix_phase)
            if ly.fiber_phase is not None:
                if ly.fiber_phase not in self.phase_by_name:
                    raise UFDemoError(CONFIG_INVALID, f"未定义相 {ly.fiber_phase!r}", field_path="structure.layers[].fiber_phase", actual=ly.fiber_phase)
                if not (ly.fiber_volume_fraction is not None and 0.0 < ly.fiber_volume_fraction < 1.0):
                    raise UFDemoError(
                        CONFIG_INVALID,
                        "纤维体积分数必须落在 (0,1)",
                        field_path="structure.layers[].fiber_volume_fraction",
                        actual=ly.fiber_volume_fraction,
                    )
                if not (ly.fiber_width is not None and ly.fiber_width > 0):
                    raise UFDemoError(
                        CONFIG_INVALID,
                        "纤维宽度必须为正",
                        field_path="structure.layers[].fiber_width_m",
                        actual=ly.fiber_width,
                    )

        self.z_top = float(z_top)
        self.original_layer_count = len(layers)

        # 同相相邻区间预先合并（细则 8 节界面更新规则 5）
        merged: list[PlyLayer] = []
        for ly in layers:
            if merged and merged[-1].fingerprint() == ly.fingerprint():
                merged[-1].thickness += ly.thickness
                merged[-1].n_merged_from += 1
            else:
                merged.append(
                    PlyLayer(
                        thickness=ly.thickness,
                        matrix_phase=ly.matrix_phase,
                        fiber_phase=ly.fiber_phase,
                        fiber_angle_deg=ly.fiber_angle_deg,
                        fiber_volume_fraction=ly.fiber_volume_fraction,
                        fiber_width=ly.fiber_width,
                    )
                )
        self.layers = merged
        self.merged_layer_count = len(merged)
        # 各层到顶面的累计距离（左闭右开区间：层 i 占 [bounds[i], bounds[i+1])）
        bounds = [0.0]
        for ly in merged:
            bounds.append(bounds[-1] + ly.thickness)
        self.bounds = bounds
        self.total_thickness = bounds[-1]
        self._tol = CONTACT_TOL_REL * max(1.0, self.total_thickness)
        if self.merged_layer_count < self.original_layer_count:
            self.notes.append(
                f"同相相邻铺层已预先合并：{self.original_layer_count} → {self.merged_layer_count} 层"
                "（不因人为层边界截断）。"
            )

    # -- 几何 --------------------------------------------------------------
    def _layer_index(self, s: Any):
        """由"距顶面深度" s 求层号（数组/标量）。到达界面即算进入下一相。"""
        import numpy as np

        tol = self._tol
        arr = np.asarray(s, dtype=np.float64)
        idx = np.zeros(arr.shape, dtype=np.int64)
        for i in range(self.merged_layer_count):
            # s + tol >= bounds[i+1] → 已在下一层
            idx = np.where(arr + tol >= self.bounds[i + 1], i + 1, idx)
        return np.clip(idx, 0, self.merged_layer_count - 1)

    def _stripe_is_fiber(self, layer: PlyLayer, x: Any, y: Any):
        import numpy as np

        if layer.fiber_phase is None:
            return None
        th = np.deg2rad(float(layer.fiber_angle_deg))
        t = -np.asarray(x, dtype=np.float64) * np.sin(th) + np.asarray(y, dtype=np.float64) * np.cos(th)
        period = float(layer.fiber_width) / float(layer.fiber_volume_fraction)
        s = np.mod(t, period)
        return s < float(layer.fiber_width)

    def phase_at(self, x: Any, y: Any, z: Any):
        import numpy as np

        scalar = np.isscalar(x) and np.isscalar(y) and np.isscalar(z)
        xx, yy, zz = _broadcast_xyz(x, y, z)
        s = np.clip(self.z_top - zz, 0.0, None)  # 高于初始表面 → 视为最上层材料
        lyr = self._layer_index(s)

        out = np.zeros(lyr.shape, dtype=np.uint16)
        for i, layer in enumerate(self.layers):
            sel = lyr == i
            if not np.any(sel):
                continue
            pid_matrix = self.phase_id_of(layer.matrix_phase)
            if layer.fiber_phase is None:
                out[sel] = pid_matrix
                continue
            is_fiber = self._stripe_is_fiber(layer, xx, yy)
            pid_fiber = self.phase_id_of(layer.fiber_phase)
            out = np.where(sel & is_fiber, pid_fiber, np.where(sel, pid_matrix, out))
        if scalar:
            return int(out.reshape(-1)[0])
        return out

    def next_different_interface(self, x: Any, y: Any, z: Any):
        """解析：沿 -z 到**第一个相标签真的发生变化**的层底距离。

        细则 8 节规则 5 要求「同相相邻区间预先合并……防止仅因人为层边界截断」。
        纤维条纹沿 z 不变，所以层内没有界面；但相邻两铺层在同一个 (x,y) 上可能
        **落到同一个相**（例如 0°/90° 两层都在纤维条纹上）。此时层底并不是相界面，
        不能在那里截断——否则该列会在人为层界上反复被限幅，等于给同相连续材料
        强加了一道界面。因此这里逐层向下找「相标签不同」的第一个层底；
        一路同相到底则返回 ``inf``。
        """
        import numpy as np

        scalar = np.isscalar(x) and np.isscalar(y) and np.isscalar(z)
        xx, yy, zz = _broadcast_xyz(x, y, z)
        s = np.clip(self.z_top - zz, 0.0, None)
        idx = self._layer_index(s)
        cur = np.asarray(self.phase_at(xx, yy, zz))
        out = np.full(idx.shape, np.inf, dtype=np.float64)
        for i in range(self.merged_layer_count - 1):
            if not np.any(idx == i):
                continue
            boundary_z = self.z_top - self.bounds[i + 1]
            # 取层底**略下方**评估下一层在该 (x,y) 的相（复用同一浮点容差）
            nxt = np.asarray(self.phase_at(xx, yy, boundary_z - self._tol))
            differs = (idx == i) & (nxt != cur)
            out = np.where(differs, np.maximum(zz - boundary_z, 0.0), out)
        if scalar:
            return float(out.reshape(-1)[0])
        return out

    def _extra_summary(self) -> dict[str, Any]:
        return {
            "z_top": self.z_top,
            "total_thickness": self.total_thickness,
            "n_layers_input": self.original_layer_count,
            "n_layers_merged": self.merged_layer_count,
            "layer_bounds": list(self.bounds),
            "layers": [ly.to_dict() for ly in self.layers],
            "same_phase_merge": self.merged_layer_count < self.original_layer_count,
        }


# ---------------------------------------------------------------------------
# 颗粒
# ---------------------------------------------------------------------------


class ParticleStructure(Structure):
    """球形颗粒 + 基体。确定性种子生成；不建立三维体素场。

    ``next_different_interface`` 用解析射线-球求交：沿 -z 求"进入/离开某个球"
    的最近距离。既不跳过界面，也不用体素近似。
    """

    is_uniform = False
    structure_type = "particle_composite"

    def __init__(
        self,
        *,
        matrix_phase: str,
        particle_phase: str,
        phases: Sequence[PhaseSpec],
        radius: float,
        z_top: float = 0.0,
        x_range: tuple[float, float] = (-1.0, 1.0),
        y_range: tuple[float, float] = (-1.0, 1.0),
        depth: float = 1.0,
        seed: int = 0,
        target_volume_fraction: float | None = None,
        max_placement_attempts: int = 20000,
        min_gap_rel: float = 0.0,
        diameter_spread_rel: float = 0.0,
    ) -> None:
        import numpy as np

        super().__init__(phases, seed=seed, target_volume_fraction=target_volume_fraction)
        if matrix_phase not in self.phase_by_name:
            raise UFDemoError(CONFIG_INVALID, f"未定义基体相 {matrix_phase!r}", field_path="structure.particles.matrix_phase", actual=matrix_phase)
        if particle_phase not in self.phase_by_name:
            raise UFDemoError(CONFIG_INVALID, f"未定义颗粒相 {particle_phase!r}", field_path="structure.particles.particle_phase", actual=particle_phase)
        if not (radius > 0):
            raise UFDemoError(CONFIG_INVALID, "颗粒半径必须为正", field_path="structure.particles.mean_diameter_m", actual=radius)
        if not (depth > 0):
            raise UFDemoError(CONFIG_INVALID, "颗粒层厚度必须为正", field_path="structure.particles.depth_m", actual=depth)
        if not (0.0 <= min_gap_rel < 1.0):
            raise UFDemoError(CONFIG_INVALID, "min_gap_rel 必须落在 [0,1)", field_path="structure.particles.min_gap_rel", actual=min_gap_rel)
        if not (0.0 <= diameter_spread_rel < 1.0):
            raise UFDemoError(CONFIG_INVALID, "diameter_spread_rel 必须落在 [0,1)", field_path="structure.particles.diameter_spread_rel", actual=diameter_spread_rel)

        self.matrix_phase = matrix_phase
        self.particle_phase = particle_phase
        self.mean_radius = float(radius)
        self.z_top = float(z_top)
        self.x_range = (float(x_range[0]), float(x_range[1]))
        self.y_range = (float(y_range[0]), float(y_range[1]))
        self.depth = float(depth)
        self.min_gap_rel = float(min_gap_rel)
        self.diameter_spread_rel = float(diameter_spread_rel)
        self.max_placement_attempts = int(max_placement_attempts)

        if target_volume_fraction is None:
            raise UFDemoError(
                CONFIG_INVALID,
                "颗粒结构必须给出 target_volume_fraction（用于推算颗粒数）",
                field_path="structure.particles.target_volume_fraction",
                actual=None,
            )
        if not (0.0 < target_volume_fraction < 1.0):
            raise UFDemoError(
                CONFIG_INVALID,
                "target_volume_fraction 必须落在 (0,1)",
                field_path="structure.particles.target_volume_fraction",
                actual=target_volume_fraction,
            )

        rng = np.random.default_rng(int(seed))
        self.rng = rng
        # 半径分布：以均值半径为正态中心，裁剪到 ±spread
        if diameter_spread_rel > 0:
            sigma = self.diameter_spread_rel * self.mean_radius / 2.0
            rad = np.clip(rng.normal(self.mean_radius, sigma, size=4096), self.mean_radius * (1 - self.diameter_spread_rel), self.mean_radius * (1 + self.diameter_spread_rel))
        else:
            rad = np.full(4096, self.mean_radius)

        v_domain = (self.x_range[1] - self.x_range[0]) * (self.y_range[1] - self.y_range[0]) * self.depth
        v_mean = 4.0 / 3.0 * np.pi * self.mean_radius ** 3
        self.n_particles_target = int(round(float(target_volume_fraction) * v_domain / v_mean))
        self.n_particles_target = max(0, self.n_particles_target)

        # 球心放在"球体完整落在域内"的盒子里：横向留半径边距，z 方向允许球顶与
        # 初始表面相切（否则颗粒永远不暴露，示例失去意义）。
        r_max = self.mean_radius * (1.0 + self.diameter_spread_rel)
        cx_lo, cx_hi = self.x_range[0] + r_max, self.x_range[1] - r_max
        cy_lo, cy_hi = self.y_range[0] + r_max, self.y_range[1] - r_max
        cz_lo, cz_hi = self.z_top - self.depth + r_max, self.z_top
        if not (cx_hi > cx_lo) or not (cy_hi > cy_lo) or not (cz_hi > cz_lo):
            raise UFDemoError(
                CONFIG_INVALID,
                "颗粒半径相对计算域过大，无法放置完整颗粒",
                field_path="structure.particles.mean_diameter_m",
                actual={"radius": self.mean_radius, "x_range": list(self.x_range), "y_range": list(self.y_range), "depth": self.depth},
                requirement="域在每个方向都要能容纳一个完整颗粒",
                suggestion="缩小颗粒直径、放大计算域，或减小颗粒层厚度。",
            )
        self.placement_box = {
            "x": [cx_lo, cx_hi],
            "y": [cy_lo, cy_hi],
            "z": [cz_lo, cz_hi],
        }

        centers: list[tuple[float, float, float, float]] = []
        attempts = 0
        place_tol = 1.0 - self.min_gap_rel  # 允许的间距折减（1.0 = 不允许重叠）
        while len(centers) < self.n_particles_target and attempts < self.max_placement_attempts:
            attempts += 1
            r = float(rad[len(centers) % rad.size])
            cx = float(rng.uniform(cx_lo, cx_hi))
            cy = float(rng.uniform(cy_lo, cy_hi))
            cz = float(rng.uniform(cz_lo, cz_hi))
            ok = True
            for (ox, oy, oz, orr) in centers:
                if (cx - ox) ** 2 + (cy - oy) ** 2 + (cz - oz) ** 2 < ((r + orr) * place_tol) ** 2:
                    ok = False
                    break
            if ok:
                centers.append((cx, cy, cz, r))
        self.centers = centers
        self.placement_attempts = attempts
        self.n_particles = len(centers)
        if self.n_particles < self.n_particles_target:
            self.warnings.append(
                f"颗粒放置达到尝试上限：目标 {self.n_particles_target} 个，实际放置 {self.n_particles} 个"
                f"（{attempts} 次尝试）。实际体积分数按实际放置数报告。"
            )
        if centers:
            self._cx = np.array([c[0] for c in centers], dtype=np.float64)
            self._cy = np.array([c[1] for c in centers], dtype=np.float64)
            self._cz = np.array([c[2] for c in centers], dtype=np.float64)
            self._r = np.array([c[3] for c in centers], dtype=np.float64)
        else:
            self._cx = self._cy = self._cz = self._r = np.zeros(0, dtype=np.float64)

        self.nominal_volume_fraction = (
            float(np.sum(4.0 / 3.0 * np.pi * self._r ** 3) / v_domain) if self.n_particles else 0.0
        )

    # -- 几何 --------------------------------------------------------------
    def phase_at(self, x: Any, y: Any, z: Any):
        import numpy as np

        scalar = np.isscalar(x) and np.isscalar(y) and np.isscalar(z)
        xx, yy, zz = _broadcast_xyz(x, y, z)
        pid_matrix = self.phase_id_of(self.matrix_phase)
        pid_particle = self.phase_id_of(self.particle_phase)
        d2 = (xx - self._cx.reshape((-1,) + (1,) * xx.ndim)) ** 2 + (yy - self._cy.reshape((-1,) + (1,) * xx.ndim)) ** 2
        inside = np.zeros(xx.shape, dtype=bool)
        for k in range(self.n_particles):
            r = self._r[k]
            near = d2[k] < r * r
            if not np.any(near):
                continue
            half2 = r * r - d2[k]
            inside |= (zz - self._cz[k]) ** 2 < half2
        out = np.where(inside, pid_particle, pid_matrix).astype(np.uint16)
        if scalar:
            return int(out.reshape(-1)[0])
        return out

    def next_different_interface(self, x: Any, y: Any, z: Any):
        """解析射线-球求交：沿 -z 方向到最近一次相变化的距离。"""
        import numpy as np

        scalar = np.isscalar(x) and np.isscalar(y) and np.isscalar(z)
        xx, yy, zz = _broadcast_xyz(x, y, z)
        if not self.n_particles:
            if scalar:
                return float("inf")
            return np.full(xx.shape, np.inf, dtype=np.float64)

        out = np.full(xx.shape, np.inf, dtype=np.float64)
        for k in range(self.n_particles):
            r = self._r[k]
            d2 = (xx - self._cx[k]) ** 2 + (yy - self._cy[k]) ** 2
            hit = d2 < r * r
            if not np.any(hit):
                continue
            half = np.sqrt(np.maximum(r * r - d2, 0.0))
            top = self._cz[k] + half
            bottom = self._cz[k] - half
            # 在球内 → 到球底（离开颗粒）
            inside_dist = np.where(hit & (zz >= bottom) & (zz <= top), zz - bottom, np.inf)
            # 在球上方 → 到球顶（进入颗粒）
            enter_dist = np.where(hit & (zz > top), zz - top, np.inf)
            cand = np.minimum(inside_dist, enter_dist)
            out = np.minimum(out, np.maximum(cand, 0.0))

        out = np.where(np.isfinite(out), np.maximum(out, 0.0), np.inf)
        if scalar:
            return float(out.reshape(-1)[0])
        return out

    def _extra_summary(self) -> dict[str, Any]:
        return {
            "matrix_phase": self.matrix_phase,
            "particle_phase": self.particle_phase,
            "mean_radius": self.mean_radius,
            "mean_diameter": 2.0 * self.mean_radius,
            "diameter_spread_rel": self.diameter_spread_rel,
            "min_gap_rel": self.min_gap_rel,
            "depth": self.depth,
            "z_top": self.z_top,
            "x_range": list(self.x_range),
            "y_range": list(self.y_range),
            "n_particles_target": self.n_particles_target,
            "n_particles_placed": self.n_particles,
            "placement_attempts": self.placement_attempts,
            "placement_box": self.placement_box,
            "nominal_volume_fraction": self.nominal_volume_fraction,
            "nominal_volume_fraction_method": "analytic_sphere_volume_unclipped_to_domain",
        }


# ---------------------------------------------------------------------------
# 由配置构造
# ---------------------------------------------------------------------------


def _phase_id_assigner() -> Any:
    counter = {"next": 1}

    def assign() -> int:
        v = counter["next"]
        counter["next"] += 1
        return v

    return assign


def check_structure_config(config: Any, parent_material: Any = None) -> tuple[list[UFDemoError], list[str], list[str]]:
    """配置层的结构化准入检查。

    返回 ``(errors, notes, warnings)``。调用方（``config.validate_run``）把 errors
    并入失败清单——**在配置层拦截**，而不是等到求解时才发现（细则 2.3 / 8 节）。

    拦截项：

    * 结构类型与材料卡声明不一致（不允许"换个材料跑同一套结构"）；
    * 非合成模式使用两相（缺分相标定时只开放 synthetic_demo）；
    * 相列表为空 / 结构与卡上的 ``structure_type`` 冲突；
    * 结构 schema 非法（缺 δ、缺深度方向、借用其它材料卡、拆整体阈值…）。
    """
    errors: list[UFDemoError] = []
    notes: list[str] = []
    warnings: list[str] = []

    st = getattr(config, "structure", None)
    if st is None or not getattr(st, "enabled", False):
        return errors, notes, warnings

    # 1. 与材料卡声明的结构类型一致（卡上的 structure_type 是材料身份的一部分）
    card_stype = getattr(parent_material, "structure_type", None)
    if card_stype is not None and card_stype != st.structure_type:
        errors.append(
            UFDemoError(
                CONDITION_MISMATCH,
                f"配置的结构类型 {st.structure_type!r} 与材料卡声明 {card_stype!r} 不一致",
                field_path="structure.structure_type",
                actual=st.structure_type,
                requirement=f"与 material.structure_type={card_stype!r} 一致",
                suggestion=(
                    "结构类型属于材料身份的一部分：铝基 SiC 只开放无量纲合成颗粒，"
                    "CFRP 只开放合成铺层。不要把一种材料的结构套到另一种材料上。"
                ),
            )
        )

    # 2. 两相缺分相标定时只开放合成模式
    if getattr(config, "run_mode", None) != "synthetic_demo":
        errors.append(
            UFDemoError(
                CONDITION_MISMATCH,
                "分相结构只开放合成模式",
                field_path="run_mode",
                actual=getattr(config, "run_mode", None),
                requirement="run_mode=synthetic_demo",
                suggestion=(
                    "两个相的阈值与去除尺度尚未各自标定时，只能做合成结构展示；"
                    "不要在 reference_case 下输出物理深度。"
                ),
            )
        )

    # 3. 相与几何
    if not st.phases:
        errors.append(
            UFDemoError(
                CONFIG_INVALID,
                "相结构缺少 phases 定义",
                field_path="structure.phases",
                actual=[],
                requirement="至少一个相，且各相自带阈值与去除尺度",
            )
        )
    if st.structure_type == "laminated_fiber_composite" and not st.layers:
        errors.append(
            UFDemoError(CONFIG_INVALID, "铺层结构缺少 layers", field_path="structure.layers", actual=[])
        )
    if st.structure_type == "particle_composite" and not st.particles:
        errors.append(
            UFDemoError(CONFIG_INVALID, "颗粒结构缺少 particles", field_path="structure.particles", actual=None)
        )
    if errors:
        return errors, notes, warnings

    # 4. 结构 schema（构造一次即可暴露缺字段/非法枚举/红线冲突）
    try:
        built = load_structure(config, parent_material)
    except UFDemoError as exc:
        errors.append(exc)
        return errors, notes, warnings
    if built is None:  # pragma: no cover - 上面已判定 enabled
        return errors, notes, warnings

    notes.append(
        f"合成结构：{built.structure_type}｜算法 {built.algorithm_version}｜种子 {built.seed}｜"
        f"相 {[p.name for p in built.phases]}。跨相截断是有损近似，"
        "被截断的候选量记为「未应用候选去除体积」，不是剩余能量。"
    )
    warnings.extend(list(built.warnings))
    return errors, notes, warnings


def load_structure(config: Any, parent_material: Any = None) -> Structure | None:
    """按 ``config.structure`` 构造结构。均质时返回 ``None``（不改变既有路径）。"""
    st = getattr(config, "structure", None)
    if st is None or getattr(st, "structure_type", "homogeneous") == "homogeneous":
        return None

    unit = config.unit
    assign = _phase_id_assigner()
    phases: list[PhaseSpec] = []
    for raw in st.phases:
        phases.append(build_phase(raw, phase_id=assign(), unit=unit, source_hint="structure"))
    ids = [p.phase_id for p in phases]
    if len(set(ids)) != len(ids):
        raise UFDemoError(CONFIG_INVALID, "相 phase_id 重复", field_path="structure.phases[].phase_id", actual=ids)

    # 红线：整体等效阈值不得拆给分相
    if parent_material is not None:
        for p in phases:
            assert_phase_threshold_not_split(p, parent_material, unit)

    if st.structure_type == "laminated_fiber_composite":
        layers: list[PlyLayer] = []
        for raw in st.layers:
            fib = raw.get("fiber_phase")
            layers.append(
                PlyLayer(
                    thickness=unit.length_to_internal(_req_pos(raw.get("thickness_m"), "structure.layers[].thickness_m")),
                    matrix_phase=str(raw.get("matrix_phase")),
                    fiber_phase=None if fib in (None, "") else str(fib),
                    fiber_angle_deg=float(raw.get("fiber_angle_deg", 0.0) or 0.0),
                    fiber_volume_fraction=(None if raw.get("fiber_volume_fraction") is None else float(raw["fiber_volume_fraction"])),
                    fiber_width=(None if raw.get("fiber_width_m") is None else unit.length_to_internal(float(raw["fiber_width_m"]))),
                )
            )
        return LayeredStructure(
            layers,
            phases,
            # grid 字段已是内部单位，不能再除一次 L_ref
            z_top=config.grid.initial_height_m,
            seed=int(st.seed),
            target_volume_fraction=st.target_volume_fraction,
        )

    if st.structure_type == "particle_composite":
        pr = dict(st.particles or {})
        diameter = _req_pos(pr.get("mean_diameter_m"), "structure.particles.mean_diameter_m")
        depth = _req_pos(pr.get("depth_m"), "structure.particles.depth_m")
        radius = unit.length_to_internal(diameter) / 2.0
        depth_i = unit.length_to_internal(depth)
        z_top = config.grid.initial_height_m
        # 采样域取网格范围（已是内部单位），z 取初始面至颗粒层底
        x_lo = config.grid.center_x_m - (config.grid.nx - 1) / 2.0 * config.grid.dx_m
        x_hi = config.grid.center_x_m + (config.grid.nx - 1) / 2.0 * config.grid.dx_m
        y_lo = config.grid.center_y_m - (config.grid.ny - 1) / 2.0 * config.grid.dy_m
        y_hi = config.grid.center_y_m + (config.grid.ny - 1) / 2.0 * config.grid.dy_m
        return ParticleStructure(
            matrix_phase=str(pr.get("matrix_phase")),
            particle_phase=str(pr.get("particle_phase")),
            phases=phases,
            radius=radius,
            z_top=z_top,
            x_range=(min(x_lo, x_hi), max(x_lo, x_hi)),
            y_range=(min(y_lo, y_hi), max(y_lo, y_hi)),
            depth=depth_i,
            seed=int(st.seed),
            target_volume_fraction=st.target_volume_fraction,
            max_placement_attempts=int(pr.get("max_placement_attempts", 20000)),
            min_gap_rel=float(pr.get("min_gap_rel", 0.0)) if pr.get("min_gap_rel") is not None else 0.0,
            diameter_spread_rel=float(pr.get("diameter_spread_rel", 0.0) or 0.0),
        )

    raise UFDemoError(
        CONFIG_INVALID,
        f"未支持的结构类型 {st.structure_type!r}",
        field_path="structure.structure_type",
        actual=st.structure_type,
        requirement=f"取值属于 {list(STRUCTURE_TYPES)}",
    )


def _req_pos(value: Any, path: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not (value > 0)
    ):
        raise UFDemoError(
            CONFIG_INVALID,
            "结构尺寸必须是有限正值",
            field_path=path,
            actual=value,
            requirement="有限正数",
        )
    return float(value)


def surface_phase_ids(structure: Structure, x: Any, y: Any, height: Any):
    """初始暴露相标签（越界自动回落到最上层材料）。"""
    import numpy as np

    arr = np.asarray(structure.phase_at(x, y, height))
    return arr.astype(np.uint16)


def sample_volume_fraction(structure: Structure, *, n: int = 20000, seed: int | None = None) -> dict[str, Any]:
    """对几何做确定性网格/随机采样，给出实际体积分数（与目标分别报告）。"""
    import numpy as np

    if structure.structure_type != "particle_composite":
        # 铺层的纤维体积分数由条纹几何解析给出
        layers = getattr(structure, "layers", [])
        total = sum(ly.thickness for ly in layers) or 1.0
        fiber_frac = 0.0
        fiber_name = None
        for ly in layers:
            if ly.fiber_phase is not None:
                fiber_frac += ly.thickness * float(ly.fiber_volume_fraction) / total
                fiber_name = ly.fiber_phase
        return {
            "target_volume_fraction": structure.target_volume_fraction,
            "actual_volume_fraction": (None if fiber_name is None else {fiber_name: fiber_frac}),
            "volume_fraction_method": "analytic_from_ply_pattern",
            "n_samples": 0,
            "note": (
                "铺层结构的纤维体积分数按条纹几何（fiber_width / period）解析给出，"
                "与 target_volume_fraction 分别报告；单层设定值不代表任意层组合的整体比例。"
            ),
        }

    rng = np.random.default_rng(structure.seed if seed is None else seed)
    # 用结构自身的域范围做确定性采样（含 z 方向分层）
    xs = rng.uniform(*structure.x_range, size=n)
    ys = rng.uniform(*structure.y_range, size=n)
    zs = structure.z_top - rng.uniform(0.0, structure.depth, size=n)
    rep = structure.volume_fraction_report(xs, ys, zs)
    rep["nominal_volume_fraction"] = structure.nominal_volume_fraction
    rep["nominal_volume_fraction_method"] = "analytic_sphere_volume_unclipped_to_domain"
    return rep
