"""窗口半径策略：按**超阈值半径**开窗（性能开关，默认关闭）。

背景（实测，1,040 事件、`axial_defocus`、δ=3.6525 µm 的标定基线卡）：

* 深度烧到 **132 µm** ≫ zR=4.47 µm ⇒ 离焦 30 倍 ⇒ 光斑 1.33→**39 µm** ⇒
  ε(1e-8) 尾部截断窗口直径 **237 µm** —— 200 µm 的域被吃满，单事件 13.6 ms；
* 而窗口内**能流超阈值**（对去除真有贡献）的格子只占 **0.8%**。

所以真正该算的半径是「能流仍可能达到阈值」的半径
``r = w*sqrt(ln(F_peak/F_th)/2)``，而不是 ε 尾部截断半径 ``3.035w``。

三条必须守住的东西：

1. **去除量必须逐位不变** —— 阈值型响应核在 ``F <= F_th`` 处返回**恰好 0**，
   半径之外的格子本来就不产生增量。用 ``np.array_equal`` 断言，不设容差。
2. **缺阈值 / 非法余量必须报错**，不得静默退化成 ε 截断（那会让调用者以为
   收缩已启用、实际没启用）。
3. **剂量观测量口径的变化必须可追踪** —— ``cumulative_fluence`` /
   ``illumination_count`` / 估计截获能量的统计范围会变小，诊断里如实上报被
   裁掉的格子比例，且**不得**把由此产生的能量差说成「能流尾部超出计算域」。
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from conftest import FIXTURE_PATH, ROOT, load_example, make_config, run_example
from ufdemo.beam import (
    WINDOW_POLICY_ABOVE_THRESHOLD,
    WINDOW_POLICY_TAIL,
    BeamOptions,
    beam_patch,
    max_ablation_radius,
    peak_fluence,
)
from ufdemo.config import SolverConfig
from ufdemo.errors import CONFIG_INVALID, RESPONSE_SEMANTICS_INVALID, UFDemoError
from ufdemo.materials import MaterialSpec, load_material_card
from ufdemo.paths import iter_events
from ufdemo.response import build_pulse_law
from ufdemo.solver import solve
from ufdemo.surface import SurfaceState

POLICY = WINDOW_POLICY_ABOVE_THRESHOLD


# --- 1. 半径：与深度无关的**严格上界** --------------------------------------

#: 与标定基线一致的参数（δ=3.6525 µm / Fth=7.8496e4 J/m² / 域内实测口径）
A_CASE = (2.66665e-4, 1.3254e-6, 7.8496e4)     # (E, w0 ,Fth)


def test_bound_is_reached_at_the_optimal_defocus():
    """上界在 u* = A/e 处被取到，此时 r = w0·sqrt(A/(2e))。"""
    E, w0, thr = A_CASE
    A = peak_fluence(E, w0) / thr
    r = max_ablation_radius(E, w0, thr)
    u_star = A / math.e
    assert r == pytest.approx(w0 * math.sqrt(u_star * math.log(A / u_star) / 2.0), rel=1e-12)
    assert max_ablation_radius(E, w0, thr, 1.5) == pytest.approx(1.5 * r, rel=1e-12)
    # 该处能流**恰好**等于阈值（上界是紧的）
    assert (A / u_star) * math.exp(-2.0 * r * r / (u_star * w0 * w0)) == pytest.approx(1.0, rel=1e-12)


def test_bound_is_rigorous_over_all_defocus_values():
    """**任意**离焦 u >= 1 下，上界半径处的能流都不超过阈值 —— 逐点验证。"""
    E, w0, thr = A_CASE
    A = peak_fluence(E, w0) / thr
    r = max_ablation_radius(E, w0, thr)
    for u in np.geomspace(1.0, A * 0.999, 400):
        assert (A / u) * math.exp(-2.0 * r * r / (u * w0 * w0)) <= 1.0 + 1e-12
    # 离焦大到 u > A 时，该格连**轴上**都不超阈值（上界自然覆盖，无需另设分支）
    for u in np.geomspace(A * 1.001, A * 100.0, 50):
        assert (A / u) <= 1.0


def test_non_monotonic_defocus_regression():
    """**回归**：ablating 半径对离焦**非单调** —— 不能用「最深点的阈值半径」。

    ``r(u) = w0*sqrt(u*ln(A/u)/2)`` 在 ``u* = A/e`` 取极大值。深孔末期
    （u 远大于 u*）的「阈值半径」比该极大值**小**，于是会切掉中等深度处
    仍然可烧蚀的环带 —— 实测让 N>=2 候选的平均深度偏差 ~1.5e-3（相对），
    而 N=1 恰好没触发（这就是当初误判成「逐位一致」的原因）。
    """
    E, w0, thr = A_CASE
    A = peak_fluence(E, w0) / thr
    bound = max_ablation_radius(E, w0, thr)
    # 深孔（132 µm，zR=4.4651 µm）处的「阈值半径」明显小于上界
    u_deep = 1.0 + (132e-6 / 4.4651e-6) ** 2
    r_deep = w0 * math.sqrt(u_deep * math.log(A / u_deep) / 2.0)
    assert r_deep < 0.90 * bound
    # 而 (r_deep, bound) 之间确实还有能烧的格子（取一个中等离焦）
    r_mid = 0.95 * bound
    u_mid = 0.5 * A
    assert (A / u_mid) * math.exp(-2.0 * r_mid ** 2 / (u_mid * w0 * w0)) > 1.0
    # 相反：老口径（按最深点）会漏掉它们 —— 这正是必须用上界的原因
    assert r_mid > r_deep


def test_radius_is_zero_when_peak_never_reaches_threshold():
    """A <= 1 ⇒ 半径 0：**任何**格子都不可能超阈值，整个平面增量为 0。"""
    E, w = 2.0e-4, 1.3e-6
    assert max_ablation_radius(E, w, peak_fluence(E, w)) == 0.0          # 恰好等于峰值
    assert max_ablation_radius(E, w, peak_fluence(E, w) * 10.0) == 0.0   # 高于峰值
    assert max_ablation_radius(0.0, w, 1.0) == 0.0                       # 无能量


# --- 2. 非法入参必须报错（不得静默退化）--------------------------------------

def test_config_rejects_invalid_policy_and_margin():
    with pytest.raises(UFDemoError) as e1:
        SolverConfig.from_dict({"window_radius_policy": "whatever"})
    assert e1.value.code == CONFIG_INVALID
    with pytest.raises(UFDemoError) as e2:
        SolverConfig.from_dict({"window_threshold_margin": 0.9})
    assert e2.value.code == CONFIG_INVALID
    # 默认关闭、默认余量 >= 1
    d = SolverConfig.from_dict({})
    assert d.window_radius_policy == WINDOW_POLICY_TAIL
    assert d.window_threshold_margin >= 1.0
    assert d.to_dict()["window_radius_policy"] == WINDOW_POLICY_TAIL


def _flat_surface_patch(cfg, options, card):
    surface = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    event = next(iter(iter_events(cfg.path, cfg.laser)))
    return beam_patch(event, surface, options), event, surface


def test_beam_patch_rejects_missing_threshold_and_bad_policy():
    cfg = make_config(load_example("analytic_single_pulse.json"))
    card = load_material_card(FIXTURE_PATH)
    with pytest.raises(UFDemoError) as e1:
        _flat_surface_patch(
            cfg, BeamOptions(window_radius_policy=POLICY, fluence_threshold=None), card
        )
    assert e1.value.code == RESPONSE_SEMANTICS_INVALID
    with pytest.raises(UFDemoError) as e2:
        _flat_surface_patch(
            cfg, BeamOptions(window_radius_policy=POLICY, fluence_threshold=-1.0), card
        )
    assert e2.value.code == RESPONSE_SEMANTICS_INVALID
    with pytest.raises(UFDemoError) as e3:
        _flat_surface_patch(cfg, BeamOptions(window_radius_policy="nonsense"), card)
    assert e3.value.code == CONFIG_INVALID
    with pytest.raises(UFDemoError) as e4:
        _flat_surface_patch(
            cfg, BeamOptions(window_radius_policy=POLICY, fluence_threshold=1e4,
                             window_threshold_margin=0.5), card
        )
    assert e4.value.code == CONFIG_INVALID


# --- 3. 平面上的窗口确实按超阈值半径收缩 -------------------------------------

def test_window_uses_threshold_radius_on_flat_surface():
    """初始平面（s=0）上：超阈半径 = w0·sqrt(ln(F_peak/F_th)/2)·margin，且窗口变小。"""
    cfg = make_config(load_example("analytic_single_pulse.json"))
    card = load_material_card(FIXTURE_PATH)
    margin = 1.25
    law = build_pulse_law(card, unit=cfg.unit)
    w0, E = cfg.laser.spot_radius_m, cfg.laser.pulse_energy_J
    expected_r = max_ablation_radius(E, w0, law.threshold_internal, margin)

    tail_patch, _, _ = _flat_surface_patch(cfg, BeamOptions(), card)
    thr_patch, _, _ = _flat_surface_patch(
        cfg,
        BeamOptions(window_radius_policy=POLICY, fluence_threshold=law.threshold_internal,
                    window_threshold_margin=margin),
        card,
    )
    assert thr_patch.window_radius_policy == POLICY
    assert thr_patch.threshold_radius_m == pytest.approx(expected_r, rel=1e-12)
    assert thr_patch.window_cells < tail_patch.window_cells          # 确实更小
    assert thr_patch.tail_window_cells == tail_patch.window_cells     # 对照口径 = 既有窗口
    assert tail_patch.threshold_radius_m is None
    # 上界半径处能流不超过阈值 ⇒ 半径外的格子增量恒为 0
    assert peak_fluence(E, w0) * math.exp(-2.0 * expected_r ** 2 / (w0 * w0)) <= law.threshold_internal


# --- 4. 去除量逐位不变（核心断言）-------------------------------------------

def _deep_hole_override(threshold_scale: float = 1.0) -> dict:
    """标定基线的响应覆盖（与界面/规划同一条装配路径）。"""
    bl = json.loads(
        (ROOT / "data" / "baselines" / "alsic_223fs_candidate.json").read_text(encoding="utf-8")
    )
    return {
        "kind": "log_fixed",
        "output_semantics": "event_depth_increment",
        "fluence_basis": "incident_peak_fluence",
        "depth_direction": "surface_normal",
        "threshold_J_m2": bl["thresholdJm2"] * threshold_scale,
        "delta_m": bl["deltaM"],
    }


def _deep_hole_case(policy: str, margin: float = 1.25, threshold_scale: float = 1.0):
    """标定基线的「深孔」算例：一趟烧到 ~132 µm ≫ zR，窗口被撑满整个域。"""
    from ufdemo.calibration import ExperimentRow, PredictionSpec, build_row_config

    ov = _deep_hole_override(threshold_scale)
    row = ExperimentRow(sample_id="win", pulse_duration_fs=223.0, repetition_rate_kHz=20.0,
                        scan_speed_mm_s=50.0, hatch_spacing_um=4.0, pass_count=1, mean_depth_um=1.0)
    spec = PredictionSpec(material_card_file="tests/fixtures/analytic_fixture.json",
                          window_um=200.0, dx_um=1.0, machining_region_um=100.0,
                          response_override=ov)
    cfg = build_row_config(row, spec=spec)
    cfg.solver.window_radius_policy = policy
    cfg.solver.window_threshold_margin = margin
    raw = json.loads((ROOT / "tests/fixtures/analytic_fixture.json").read_text(encoding="utf-8"))
    raw["response"] = {**(raw.get("response") or {}), **ov}
    return cfg, MaterialSpec.from_dict(raw)


def test_policy_leaves_removal_bitwise_identical_on_deep_hole_case():
    cfg_tail, mat = _deep_hole_case(WINDOW_POLICY_TAIL)
    cfg_thr, _ = _deep_hole_case(POLICY)
    res_tail = solve(cfg_tail, mat)
    res_thr = solve(cfg_thr, mat)
    assert res_tail.ok and res_thr.ok

    h_tail = np.asarray(res_tail.surface.height)
    h_thr = np.asarray(res_thr.surface.height)
    # **不设容差**：阈值半径之外的格子增量为 0，必须逐位一致
    assert np.array_equal(h_tail, h_thr)
    assert float((res_thr.surface.initial_height - h_thr).max()) > 1e-4   # 确实是深孔算例

    tw = res_thr.diagnostics["fluence_ledger"]["threshold_window"]
    assert tw["enabled"] is True
    assert tw["events"] == res_thr.events_processed > 0
    assert tw["window_cells_total"] < tw["tail_window_cells_total"]
    # 深孔末期窗口内绝大多数格子都低于阈值 —— 收缩幅度必须可观
    assert tw["cells_skipped_fraction"] > 0.5
    assert tw["threshold_internal"] > 0.0
    assert tw["collapsed_events"] == 0


def test_multi_pass_candidates_stay_bitwise_identical():
    """**回归**：N>=2 的候选也必须逐位一致。

    当初用「窗口内最深点的阈值半径」时，N=1 恰好逐位一致、N>=2 却偏差 1.5e-3
    —— 只测 N=1 会漏掉这个错。这里直接评估 h=2/N=2 这个**当初失败过**的候选。
    """
    from ufdemo.planning import evaluate_candidate

    cfg_tail, mat = _deep_hole_case(WINDOW_POLICY_TAIL)
    ov = _deep_hole_override()
    common = dict(
        material_card_file="tests/fixtures/analytic_fixture.json",
        region_um=(100.0, 100.0), domain_um=(200.0, 200.0), dx_um=1.0,
        pulse_duration_fs=223.0, repetition_rate_kHz=20.0, scan_speed_mm_s=50.0,
        target_depth_um=60.0, tolerance_um=12.0, response_override=ov,
    )
    a = evaluate_candidate(2.0, 2, **common, window_radius_policy=WINDOW_POLICY_TAIL)
    b = evaluate_candidate(2.0, 2, **common, window_radius_policy=POLICY)
    assert a.mean_depth_um is not None and b.mean_depth_um is not None
    assert a.mean_depth_um == b.mean_depth_um            # 不设容差
    assert a.max_depth_um == b.max_depth_um
    assert a.coverage_fraction == b.coverage_fraction
    assert a.status == b.status
    # 启用时必须如实上报策略与裁掉比例，且不产生越界噪声
    assert b.window_radius_policy == POLICY
    assert b.window_cells_skipped_fraction is not None
    assert a.window_radius_policy == WINDOW_POLICY_TAIL
    assert a.window_cells_skipped_fraction is None
    assert not [n for n in a.notes if "above_threshold" in n]
    assert [n for n in b.notes if "above_threshold" in n]


def test_policy_diagnostics_do_not_call_threshold_truncation_domain_truncation():
    """口径变化必须写清楚：**不得**把「低于阈值」说成「能流尾部超出计算域」。"""
    cfg, mat = _deep_hole_case(POLICY)
    res = solve(cfg, mat)
    ledger = res.diagnostics["fluence_ledger"]
    assert ledger["window_radius_policy"] == POLICY
    assert ledger["threshold_window"]["enabled"] is True
    hit = [w for w in res.warnings if "above_threshold" in w]
    assert hit, "启用收缩后必须给出可追踪的说明"
    text = hit[0]
    assert "低于响应阈值" in text and "恰好为 0" in text
    assert "不是「能流尾部超出计算域」" in text
    assert "剂量观测量" in text
    # 默认口径下不得出现这条说明
    cfg_tail, mat_tail = _deep_hole_case(WINDOW_POLICY_TAIL)
    res_tail = solve(cfg_tail, mat_tail)
    assert not [w for w in res_tail.warnings if "above_threshold" in w]
    assert res_tail.diagnostics["fluence_ledger"]["threshold_window"]["enabled"] is False
    assert res_tail.diagnostics["fluence_ledger"]["threshold_window"]["events"] == 0


# --- 5. 阈值高到整个平面都烧不动时，窗口收缩为空 -----------------------------

def test_window_collapses_when_threshold_is_unreachable():
    """阈值放大到峰值能流以下 ⇒ 所有事件增量为 0，窗口收缩为空且如实计数。"""
    scale = 1e4                                   # F_peak/F_th ≪ 1
    cfg_thr, mat = _deep_hole_case(POLICY, threshold_scale=scale)
    cfg_tail, _ = _deep_hole_case(WINDOW_POLICY_TAIL, threshold_scale=scale)
    res_thr = solve(cfg_thr, mat)
    res_tail = solve(cfg_tail, mat)
    assert res_thr.ok and res_tail.ok

    tw = res_thr.diagnostics["fluence_ledger"]["threshold_window"]
    assert tw["collapsed_events"] == res_thr.events_processed > 0
    assert tw["window_cells_total"] == 0
    # 两种口径都必须给出「没有任何去除」（逐位一致，且全为 0）
    h_thr = np.asarray(res_thr.surface.height)
    assert np.array_equal(h_thr, np.asarray(res_tail.surface.height))
    assert float(np.abs(h_thr - res_thr.surface.initial_height).max()) == 0.0
    assert any("超阈值半径" in w for w in res_thr.warnings)


# --- 6. 真实算例走完整配置路径：逐位一致 -------------------------------------

@pytest.mark.parametrize("example", ["line_scan.json", "oblique_plane_60deg.json"])
def test_policy_bitwise_identical_through_full_config(example):
    """斜入射也走同一条断言：收缩只切掉「本来就不产生增量」的格子。"""
    _, res_tail = run_example(example)
    _, res_thr = run_example(example, **{"solver.window_radius_policy": POLICY})
    assert res_tail.ok and res_thr.ok
    assert np.array_equal(np.asarray(res_tail.surface.height),
                          np.asarray(res_thr.surface.height))
    tw = res_thr.diagnostics["fluence_ledger"]["threshold_window"]
    assert tw["enabled"] is True
    assert tw["window_cells_total"] <= tw["tail_window_cells_total"]


def test_default_policy_is_unchanged_by_this_feature():
    """默认必须是既有行为：不传字段 = tail_epsilon，且诊断字段如实标 False。"""
    cfg, res = run_example("analytic_single_pulse.json")
    ledger = res.diagnostics["fluence_ledger"]
    assert ledger["window_radius_policy"] == WINDOW_POLICY_TAIL
    assert ledger["threshold_window"]["enabled"] is False
    assert ledger["threshold_window"]["cells_skipped_fraction"] is None
