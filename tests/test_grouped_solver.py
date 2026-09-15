"""G08：分组模式与逐脉冲参考的一致性、误差与回退（批次 I / T16、T17）。

执行细则 10 节的 G08 断言与任务书 10.2 的误差定义：

* 固定阈值、冻结几何、无相切换 → **接近浮点精度一致**；
* 打开允许的弱几何变化后 → 总去除体积与深度场偏差均 **≤ 1%**：
  ``abs(d_group-d_ref) <= 0.01*abs(d_ref) + 0.01*delta_test``；
* 局部误差估计（B 与两个 B/2）→ 拒绝则缩小 B，最终回到 B=1；
* 完整逐脉冲对照 → 报告**最大绝对差**与**归一化 L2 差**；
* 频繁相切换 / 未支持历史 / 未支持组合 → **不启用批量**且保存原因。

另含两条红线回归：**不得先累加能流再取一次对数**；试算**不得提交任何状态**。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from ufdemo.accelerators import (
    BatchPolicy,
    GeometryView,
    LocalKernel,
    accumulate_block,
    check_fallback_conditions,
    drift_reference_for,
    estimate_local_error,
    numba_available,
    solve_block,
)
from ufdemo.config import RunConfig, validate_run
from ufdemo.materials import load_material_card
from ufdemo.response import HistoryState, build_pulse_law
from ufdemo.solver import solve
from ufdemo.surface import SurfaceState

pytestmark = pytest.mark.g08

ROOT = Path(__file__).resolve().parents[1]
TEN_PULSES = ROOT / "examples" / "ten_pulses.json"
DELTA_TEST_INTERNAL = 1e-7  # 解析 fixture 的 delta（内部长度单位）


# ---------------------------------------------------------------------------
# 公共夹具
# ---------------------------------------------------------------------------


def _raw() -> dict[str, Any]:
    return json.loads(TEN_PULSES.read_text(encoding="utf-8"))


def _card(raw: dict[str, Any]):
    return load_material_card(Path(raw["material_card_file"]))


def _cfg(raw: dict[str, Any]) -> RunConfig:
    return RunConfig.from_dict(raw)


def _run(mode: str, *, batch_size: int = 64, zR: float | None = None,
         geometry_feedback: str | None = None, accel: str = "off"):
    raw = _raw()
    if zR is not None:
        raw["laser"]["rayleigh_range_m"] = zR
    raw["solver"]["mode"] = mode
    raw["solver"]["batch_size"] = batch_size
    raw["solver"]["acceleration"] = accel
    # 这些测试比的是**批量与逐脉冲的等价**，不是窗口口径 ⇒ 显式钉住既有 ε 口径，
    # 与默认策略（above_threshold，ADR-0022）解耦；分组路径不支持超阈值开窗。
    raw["solver"]["window_radius_policy"] = "tail_epsilon"
    if geometry_feedback is not None:
        raw["solver"]["geometry_feedback"] = geometry_feedback
    return solve(_cfg(raw), _card(raw))


def _depth(res) -> np.ndarray:
    return res.surface.initial_height - res.surface.height


def _volume(depth: np.ndarray, raw: dict[str, Any]) -> float:
    g = raw["grid"]
    return float(np.sum(depth)) * g["dx_m"] * g["dy_m"]


def _domain_area(raw: dict[str, Any]) -> float:
    g = raw["grid"]
    return g["nx"] * g["ny"] * g["dx_m"] * g["dy_m"]


# ---------------------------------------------------------------------------
# 1. 冻结几何：接近浮点精度一致
# ---------------------------------------------------------------------------


def test_fixed_geometry_matches_reference_within_float_precision():
    """G08 主断言：固定阈值 + 冻结几何 + 同相 → 与逐脉冲参考接近浮点一致。"""
    ref = _run("reference")
    grp = _run("grouped", batch_size=10)
    assert ref.status == "completed" and grp.status == "completed"

    d_ref = _depth(ref)
    d_grp = _depth(grp)
    max_abs = float(np.max(np.abs(d_grp - d_ref)))
    scale = float(np.max(np.abs(d_ref)))

    # 接近浮点精度：相对差远小于 1e-12（实测 ~2e-16，只有加法顺序差异）
    assert max_abs <= 1e-12 * scale + 1e-18, f"分组与参考偏差过大：{max_abs:.3e}（尺度 {scale:.3e}）"
    assert max_abs < 1e-12


def test_counters_match_reference_exactly():
    """计数语义与逐脉冲一致：曝光/照射次数**逐位相同**，剂量在浮点末位内一致。

    ``cumulative_fluence`` 是求和量，分组是一次性加块内和、逐脉冲是逐次累加，
    加法顺序不同会带来末位差异（这不是计数错误）。其余整数计数必须逐位相等。
    """
    ref = _run("reference")
    grp = _run("grouped", batch_size=5)
    assert np.array_equal(ref.surface.exposure_count, grp.surface.exposure_count)
    assert np.array_equal(ref.surface.illumination_count, grp.surface.illumination_count)
    assert np.array_equal(ref.surface.phase_id, grp.surface.phase_id)
    assert np.allclose(ref.surface.cumulative_fluence, grp.surface.cumulative_fluence, rtol=1e-14, atol=0.0)


def test_event_count_and_statistics_agree():
    """事件数与中心深度一致（分组不得漏算或多算事件）。"""
    ref = _run("reference")
    grp = _run("grouped", batch_size=4)
    assert ref.diagnostics["events"]["n_events"] == grp.diagnostics["events"]["n_events"] == 10
    assert ref.events_total == grp.events_total
    assert float(grp.surface.max_depth()) == pytest.approx(float(ref.surface.max_depth()), rel=1e-12)


# ---------------------------------------------------------------------------
# 2. 批大小不变性 / B=1 / 奇数块
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("batch_size", [1, 2, 3, 5, 10, 64])
def test_batch_size_invariance(batch_size: int):
    """批大小不改变物理结果（1/2/3/5/10/64 与参考一致）。"""
    ref = _run("reference")
    grp = _run("grouped", batch_size=batch_size)
    max_abs = float(np.max(np.abs(_depth(grp) - _depth(ref))))
    assert max_abs <= 1e-12, f"batch_size={batch_size} 时偏差 {max_abs:.3e}"


def test_batch_one_uses_reference_update_without_patch_reuse():
    """B=1：每块只含 1 个事件，因此无块内复用（补丁总数 == 事件数、命中 0）。"""
    raw = _raw()
    cfg = _cfg(raw)
    card = _card(raw)
    law = build_pulse_law(card, unit=cfg.unit)
    surface = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    from ufdemo.paths import iter_events

    events = list(iter_events(cfg.path, cfg.laser))
    policy = BatchPolicy(batch_size=1)
    rest = list(events)
    total_patches = 0
    total_hits = 0
    while rest:
        plan = solve_block(GeometryView.of(surface), rest[:1], law, policy=policy)
        assert plan.batch_size == 1
        total_patches += plan.accumulation.n_patches
        total_hits += plan.accumulation.patch_cache_hits
        rest = rest[plan.batch_size:]
    assert total_patches == len(events)
    assert total_hits == 0


def test_odd_batch_split_uses_two_balanced_halves():
    """奇数块按两个尽量等长的子块切分（前半取较大的一半）。"""
    raw = _raw()
    cfg = _cfg(raw)
    card = _card(raw)
    law = build_pulse_law(card, unit=cfg.unit)
    surface = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    from ufdemo.paths import iter_events

    events = list(iter_events(cfg.path, cfg.laser))[:5]  # 奇数 5
    plan = solve_block(
        GeometryView.of(surface), events, law, policy=BatchPolicy(batch_size=5)
    )
    assert plan.batch_size == 5
    # 5 → 前半 3、后半 2（尽量等长，前半不小于后半）
    assert plan.estimate.half_size == 3


# ---------------------------------------------------------------------------
# 3. 红线：不得先累加能流再取一次对数
# ---------------------------------------------------------------------------


def test_no_fluence_preaccumulation_red_line():
    """可判别构造：两个等能脉冲的正确结果是 2·δ·ln(F/Fth)，而不是 δ·ln(2F/Fth)。

    解析 fixture 取 ``F0 = e²·Fth``，故正确 = 4δ，错误（先累加能流）= δ·ln(2e²)≈2.69δ。
    """
    raw = _raw()
    cfg = _cfg(raw)
    card = _card(raw)
    law = build_pulse_law(card, unit=cfg.unit)
    delta = float(law.delta_internal)
    thr = float(law.threshold_internal)

    surface = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    from ufdemo.paths import iter_events

    events = list(iter_events(cfg.path, cfg.laser))[:2]
    acc = accumulate_block(GeometryView.of(surface), events, law)

    # 中心单元（网格中心）
    iy, ix = acc.delta_h.shape[0] // 2, acc.delta_h.shape[1] // 2
    center = float(acc.delta_h[iy, ix])

    f0 = float(cfg.laser.peak_fluence_internal)
    correct = 2.0 * delta * np.log(f0 / thr)
    wrong = delta * np.log(2.0 * f0 / thr)

    assert center == pytest.approx(correct, rel=1e-12), "疑似先累加能流再取对数"
    assert abs(center - wrong) > 0.5 * abs(correct - wrong), "可判别构造失效：两条路径结果未分离"


# ---------------------------------------------------------------------------
# 4. 弱几何变化：≤ 1%（G08 的第二个主断言）
# ---------------------------------------------------------------------------


def test_weak_geometry_change_within_one_percent():
    """axial_defocus + 弱离焦 → 体积与深度场偏差均 ≤ 1%（细则 10 节容差）。"""
    zR = 1e-5  # 实测深度偏差约 0.56%，落在判据内且有区分度
    ref = _run("reference", zR=zR, geometry_feedback="axial_defocus")
    grp = _run("grouped", batch_size=10, zR=zR, geometry_feedback="axial_defocus")

    raw = _raw()
    d_ref, d_grp = _depth(ref), _depth(grp)

    # 深度场判据：abs(d_group-d_ref) <= 0.01*abs(d_ref) + 0.01*delta_test
    tol_field = 0.01 * np.abs(d_ref) + 0.01 * DELTA_TEST_INTERNAL
    assert np.all(np.abs(d_grp - d_ref) <= tol_field), "深度场超过 1%+0.01δ 容差"

    # 体积判据：abs(Vg-Vr) <= 0.01*abs(Vr) + A_domain*0.01*delta_test
    v_ref = _volume(d_ref, raw)
    v_grp = _volume(d_grp, raw)
    tol_vol = 0.01 * abs(v_ref) + _domain_area(raw) * 0.01 * DELTA_TEST_INTERNAL
    assert abs(v_grp - v_ref) <= tol_vol, "体积超过 1%+A·0.01δ 容差"

    # 误差确有量级（说明确实在测几何耦合，而非退化为恒等）
    rel = float(np.max(np.abs(d_grp - d_ref))) / float(np.max(np.abs(d_ref)))
    assert 1e-4 < rel < 1e-2


def test_geometry_drift_triggers_rejection_and_shrinkage():
    """几何漂移过大 → 局部判据拒绝该块并缩小 batch_size（不提交被拒状态）。

    漂移判据只在几何反馈真正影响能流时才有意义：``fixed_geometry`` 下
    ``beam_patch`` 用 ``initial_height`` 算离焦，与当前高度无关 → 参考长度为 ``None``。
    """
    zR = 5e-6  # 2μm 去除 / 5μm 离焦 = 0.4 > 0.25 漂移上限
    raw = _raw()
    raw["laser"]["rayleigh_range_m"] = zR
    raw["solver"]["geometry_feedback"] = "axial_defocus"
    cfg = _cfg(raw)
    card = _card(raw)
    law = build_pulse_law(card, unit=cfg.unit)
    surface = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    from ufdemo.paths import iter_events

    events = list(iter_events(cfg.path, cfg.laser))
    view = GeometryView.of(surface)
    assert drift_reference_for(cfg) == pytest.approx(zR)

    est, _acc = estimate_local_error(
        view, events, law,
        drift_reference_internal=zR, drift_limit=0.25,
        rel_tol=cfg.solver.local_rel_tol,
    )
    assert not est.ok
    assert est.geometry_drift > 0.25
    assert "几何漂移" in (est.reason or "")

    plan = solve_block(
        view, events, law,
        policy=BatchPolicy(batch_size=10),
        drift_reference_internal=zR,
    )
    assert plan.n_rejected >= 1
    assert plan.fallback_reason is not None


def test_drift_reference_is_none_for_fixed_geometry():
    """``fixed_geometry`` 下冻结几何是**精确**的（能流只依赖初始面），无漂移参考长度。"""
    raw = _raw()
    raw["solver"]["geometry_feedback"] = "fixed_geometry"
    assert drift_reference_for(_cfg(raw)) is None

    raw["solver"]["geometry_feedback"] = "axial_defocus"
    raw["laser"]["rayleigh_range_m"] = 2e-5
    assert drift_reference_for(_cfg(raw)) == pytest.approx(2e-5)
    # 无 zR 时退化为光斑半径尺度
    raw["laser"]["rayleigh_range_m"] = None
    assert drift_reference_for(_cfg(raw)) == pytest.approx(raw["laser"]["spot_radius_m"])


def test_rejected_trial_does_not_commit_state():
    """试算过程不得改动表面（不提交高度、计数、剂量）。"""
    raw = _raw()
    cfg = _cfg(raw)
    card = _card(raw)
    law = build_pulse_law(card, unit=cfg.unit)
    surface = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    h0 = surface.height.copy()
    exp0 = surface.exposure_count.copy()
    flu0 = surface.cumulative_fluence.copy()

    from ufdemo.paths import iter_events

    events = list(iter_events(cfg.path, cfg.laser))
    solve_block(GeometryView.of(surface), events, law, policy=BatchPolicy(batch_size=4))

    assert np.array_equal(surface.height, h0)
    assert np.array_equal(surface.exposure_count, exp0)
    assert np.array_equal(surface.cumulative_fluence, flu0)


# ---------------------------------------------------------------------------
# 5. 回退判据（细则 9.1 末）
# ---------------------------------------------------------------------------


def test_fallback_for_structured_and_history():
    """分相结构 / 历史耦合 → 不启用批量，并给出原因。"""
    d1 = check_fallback_conditions(
        structured=True, history_enabled=False, geometry_feedback="fixed_geometry"
    )
    assert not d1.use_batch and "分相" in (d1.reason or "")

    d2 = check_fallback_conditions(
        structured=False, history_enabled=True, geometry_feedback="fixed_geometry"
    )
    assert not d2.use_batch and "历史" in (d2.reason or "")

    d3 = check_fallback_conditions(
        structured=False, history_enabled=False, geometry_feedback="fixed_geometry",
        dynamic_angle=True,
    )
    assert not d3.use_batch and "动态角度" in (d3.reason or "")

    ok = check_fallback_conditions(
        structured=False, history_enabled=False, geometry_feedback="fixed_geometry"
    )
    assert ok.use_batch and ok.reason is None


def test_config_layer_rejects_grouped_red_lines():
    """配置层拦截：分组 × 分相 / 分组 × 历史 / 分组 × 动态角度。"""
    card = _card(_raw())
    for key, value in (
        ("structured_interface", True),
        ("history_enabled", True),
        ("dynamic_angle", True),
    ):
        raw = _raw()
        raw["solver"]["mode"] = "grouped"
        raw["solver"][key] = value
        rep = validate_run(_cfg(raw), card)
        assert not rep.ok, f"{key} 未被拦截"
        assert any(e["code"] == "CONFIG_INVALID" for e in rep.errors)


def test_grouped_structured_is_blocked_before_solving():
    """求解器侧兜底：分组 × 分相在**进入求解前**就被配置层拦下，返回 failed 而不跑批量。

    这验证了「未开放组合在配置层拦截、不靠运行期静默降级」的约定在真实
    ``solve()`` 路径上生效（而不是只在校验函数里成立）。
    """
    raw = _raw()
    raw["solver"]["mode"] = "grouped"
    raw["solver"]["structured_interface"] = True
    res = solve(_cfg(raw), _card(raw))
    assert res.status == "failed"
    assert any(e["code"] == "CONFIG_INVALID" for e in res.errors)
    # 没有跑过任何事件，也没有产生批量诊断
    assert res.diagnostics.get("events", {}).get("n_events", 0) == 0


# ---------------------------------------------------------------------------
# 6. 误差报告（最大绝对差 + 归一化 L2）
# ---------------------------------------------------------------------------


def test_local_error_reports_max_abs_and_l2():
    """局部误差估计必须同时给出最大绝对差与归一化 L2 差。"""
    raw = _raw()
    cfg = _cfg(raw)
    card = _card(raw)
    law = build_pulse_law(card, unit=cfg.unit)
    surface = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    from ufdemo.paths import iter_events

    events = list(iter_events(cfg.path, cfg.laser))
    est, acc = estimate_local_error(
        GeometryView.of(surface), events, law, drift_reference_internal=None
    )
    d = est.to_dict()
    for key in ("max_abs_internal", "rel_l2", "volume_abs_internal", "batch_size", "half_size"):
        assert key in d
    assert est.max_abs_internal >= 0.0 and est.rel_l2 >= 0.0
    assert acc.n_events == len(events)


def test_full_reference_comparison_reports_metrics():
    """完整逐脉冲对照报告两项指标（求解器诊断 + 实测值可算）。"""
    ref = _run("reference")
    grp = _run("grouped", batch_size=5)
    d_ref, d_grp = _depth(ref), _depth(grp)
    max_abs = float(np.max(np.abs(d_grp - d_ref)))
    l2 = float(np.linalg.norm((d_grp - d_ref).ravel())) / float(np.linalg.norm(d_ref.ravel()))
    assert max_abs < 1e-12
    assert l2 < 1e-12

    diag = grp.diagnostics["acceleration"]
    assert diag["effective_mode"] == "grouped"
    assert "max_local_error_internal" in diag and "max_rel_l2_local" in diag
    assert "不是全局误差证明" in diag["note"]


# ---------------------------------------------------------------------------
# 7. 加速效果与快照近似标注
# ---------------------------------------------------------------------------


def test_fixed_point_pulses_reuse_beam_patch():
    """定点多脉冲命中补丁缓存：光束计算次数远小于脉冲数。"""
    grp = _run("grouped", batch_size=10)
    diag = grp.diagnostics["acceleration"]
    assert diag["n_beam_patches"] == 1, "相同焦点的连续脉冲应只算一次光束"
    assert diag["patch_cache_hits"] == 9
    assert diag["patch_reuse_ratio"] == pytest.approx(0.9)


def test_snapshot_on_block_boundary_is_annotated():
    """分组模式下快照在块边界记录，必须如实写入近似说明。"""
    raw = _raw()
    raw["solver"]["mode"] = "grouped"
    raw["solver"]["batch_size"] = 5
    raw["output"]["snapshot_policy"] = "events"
    raw["output"]["snapshot_events"] = [4, 9]
    res = solve(_cfg(raw), _card(raw))
    assert res.status == "completed"
    if res.diagnostics["acceleration"]["snapshot_on_block_boundary"]:
        assert any("块边界" in a for a in res.metadata["approximations"])


# ---------------------------------------------------------------------------
# 8. Numba 局部核（T16）：可选依赖，缺失即回退
# ---------------------------------------------------------------------------


def test_numba_kernel_matches_numpy_when_available():
    """numba 局部核与 NumPy 参考核逐位同式；缺失时回退且如实标注。"""
    f = np.array([[2.0, 1.0], [0.5, 4.0]], dtype=np.float64)
    np_kernel = LocalKernel.build(prefer_numba=False)
    out_np = np_kernel.apply(f, threshold_internal=1.0, delta_internal=0.1)

    if numba_available():
        nb_kernel = LocalKernel.build(prefer_numba=True)
        assert nb_kernel.kind == "numba"
        assert nb_kernel.jit_time_s is not None and nb_kernel.jit_time_s >= 0.0
        out_nb = nb_kernel.apply(f, threshold_internal=1.0, delta_internal=0.1)
        assert np.array_equal(out_np, out_nb), "numba 与 numpy 局部核结果必须一致"
    else:  # pragma: no cover - 取决于环境
        assert np_kernel.kind == "numpy"


def test_numba_backend_result_identical_to_numpy_backend():
    """以 acceleration=numba 求解，结果与 numpy 后端逐位一致（只换后端不改物理）。"""
    if not numba_available():  # pragma: no cover - 取决于环境
        pytest.skip("本环境未安装 numba")

    a = _run("grouped", batch_size=5, accel="off")
    b = _run("grouped", batch_size=5, accel="numba")
    assert b.metadata["acceleration"]["local_kernel"] == "numba"
    assert np.array_equal(a.surface.height, b.surface.height)
    assert (
        a.diagnostics["acceleration"]["n_beam_patches"]
        == b.diagnostics["acceleration"]["n_beam_patches"]
    )


def test_acceleration_metadata_recorded_consistently():
    """参考模式下也记录 acceleration 元数据（界面/导出同源，不留空字段）。"""
    ref = _run("reference")
    acc = ref.metadata["acceleration"]
    assert acc["requested_mode"] == "reference"
    assert acc["effective_mode"] == "reference"
    assert ref.metadata["enabled_features"]["acceleration"] == "off"
