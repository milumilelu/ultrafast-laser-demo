"""G01：人工单脉冲解析基准（执行细则 10 节 / 任务书 10 节）。

断言：中心 200 nm、半径 10 μm、体积 31.4159265358979 μm³；
标量相对误差 ≤ 1e-12，网格积分体积相对误差 ≤ 1%。

网格收敛记录：``dx = dy = w/20``、``w/40``、``w/80``，逐级报告体积相对误差。
"""

from __future__ import annotations

import math

import pytest

from conftest import DELTA, EP, F0_PEAK, FTH, PEAK_DEPTH, R_A, V_ANALYTIC, W0, load_example, make_config
from ufdemo.beam import enclosed_energy_fraction, gaussian_fluence_perp, peak_fluence
from ufdemo.materials import load_material_card
from ufdemo.solver import solve

pytestmark = pytest.mark.g01


def test_scalar_analytic_values():
    """标量解析值本身（相对误差 1e-12）。"""
    assert peak_fluence(EP, W0) == pytest.approx(F0_PEAK, rel=1e-12)
    assert F0_PEAK == pytest.approx(73890.5609893065, rel=1e-12)
    assert EP == pytest.approx(1.16067021786817e-5, rel=1e-12)
    assert DELTA * math.log(F0_PEAK / FTH) == pytest.approx(PEAK_DEPTH, rel=1e-12)
    assert W0 * math.sqrt(math.log(F0_PEAK / FTH) / 2.0) == pytest.approx(R_A, rel=1e-12)
    assert math.pi * W0 * W0 * DELTA / 4.0 * (math.log(F0_PEAK / FTH) ** 2) == pytest.approx(V_ANALYTIC, rel=1e-12)
    assert V_ANALYTIC * 1e18 == pytest.approx(31.4159265358979, rel=1e-12)


def test_beam_integrates_to_pulse_energy():
    """整平面能量积分与解析值一致（任务书 T03 验收）。

    (a) 环形精确积分：验证 ``F_perp`` 公式本身，容差取机器精度量级；
    (b) 细网格数值积分：验证采样后的数值积分，容差取 1e-6。
    """
    import numpy as np

    r_cut = 40 * W0
    edges = np.linspace(0.0, r_cut, 20001)
    rings = EP * (np.exp(-2.0 * edges[:-1] ** 2 / W0 ** 2) - np.exp(-2.0 * edges[1:] ** 2 / W0 ** 2))
    assert float(np.sum(rings)) == pytest.approx(EP, rel=1e-12)
    assert abs(float(np.sum(rings)) - EP) / EP < 1e-13

    r = np.linspace(0.0, r_cut, 200001)
    F = gaussian_fluence_perp(r ** 2, EP, W0)
    _trapz = getattr(np, "trapezoid", None) or np.trapz
    integral = _trapz(F * 2.0 * math.pi * r, r)
    assert integral == pytest.approx(EP, rel=1e-6)
    assert enclosed_energy_fraction(W0, W0) == pytest.approx(1 - math.exp(-2.0), rel=1e-12)


def test_single_pulse_grid_depth_and_volume():
    """示例配置 analytic_single_pulse.json 的网格结果。"""
    raw = load_example("analytic_single_pulse.json")
    cfg = make_config(raw)
    material = load_material_card(raw["material_card_file"])
    res = solve(cfg, material)

    assert res.status == "completed"
    assert res.events_processed == 1

    st = res.statistics
    assert st["center_depth_internal"] == pytest.approx(PEAK_DEPTH, rel=1e-12)
    assert st["max_depth_internal"] == pytest.approx(PEAK_DEPTH, rel=1e-12)
    assert st["removal_volume_internal"] == pytest.approx(V_ANALYTIC, rel=1e-2)

    # 去除半径：最外侧仍有去除的单元中心半径应接近 w（受网格采样限制）
    depth = res.surface.depth
    ys, xs = res.surface.y, res.surface.x
    nz = [(y, x) for iy, y in enumerate(ys) for ix, x in enumerate(xs) if depth[iy, ix] > 0]
    r_max = max(math.hypot(y, x) for y, x in nz)
    dx = cfg.grid.dx_m
    assert abs(r_max - R_A) <= dx  # 位置容差按网格间距定义，不强制与标量公式同精度


def test_volume_convergence_record():
    """体积收敛记录：w/20 → w/40 → w/80。"""
    base = load_example("analytic_single_pulse.json")
    material = load_material_card(base["material_card_file"])
    errors = []
    for divisor in (20, 40, 80):
        raw = load_example("analytic_single_pulse.json")
        raw["grid"]["dx_m"] = W0 / divisor
        raw["grid"]["dy_m"] = W0 / divisor
        cfg = make_config(raw)
        res = solve(cfg, material)
        err = abs(res.statistics["removal_volume_internal"] - V_ANALYTIC) / V_ANALYTIC
        errors.append(err)
        print(f"dx = w/{divisor} = {W0 / divisor:.3e} m → 体积相对误差 {err:.3e}")
        assert err <= 1e-2
    # 收敛：细化网格不应变差（允许极小数值抖动）
    assert errors[1] <= errors[0] * 1.05
    assert errors[2] <= errors[1] * 1.05


def test_no_incubation_and_no_defocus_linearity_of_kernel():
    """单脉冲核在阈值处为零，随 ln(F/Fth) 线性（核实现检查）。"""
    from ufdemo.response import FixedThresholdLogLaw, HistoryState

    law = FixedThresholdLogLaw(threshold_internal=FTH, delta_internal=DELTA)
    import numpy as np

    F = np.array([[FTH, FTH * math.exp(1.0), FTH * 0.5, FTH * math.exp(2.0)]])
    inc = law.increment(F, HistoryState(exposure_count=None))
    vals = inc.values[0]
    assert vals[0] == 0.0
    assert vals[1] == pytest.approx(DELTA, rel=1e-12)
    assert vals[2] == 0.0
    assert vals[3] == pytest.approx(2 * DELTA, rel=1e-12)
    assert inc.output_semantics == "event_depth_increment"
    assert inc.depth_direction == "surface_normal"
