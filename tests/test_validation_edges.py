"""G03：零输入、边界和阈值（执行细则 10 节 / 任务书 10 节）。

覆盖：

* 零脉冲 / 全部关闭出光 → 零去除；
* 等于阈值为零；
* 无效能量、负半径、NaN 参数 → 拒绝；
* 照射落在有限域外 → 不把遗漏能量归一化回域内；
* 缺 δ 的真实材料卡请求深度 → 明确报错，不悄悄切换合成模式；
* 累计/平均响应语义 → 拒绝进入事件核。
"""

from __future__ import annotations

import copy
import math

import pytest

from conftest import DELTA, EP, FTH, ROOT, W0, load_example, make_config
from ufdemo.config import RunConfig, validate_run
from ufdemo.errors import (
    CONFIG_INVALID,
    MATERIAL_CAPABILITY_MISSING,
    RESPONSE_SEMANTICS_INVALID,
    UFDemoError,
)
from ufdemo.materials import load_material_card
from ufdemo.response import FixedThresholdLogLaw, HistoryState, build_pulse_law
from ufdemo.solver import solve

pytestmark = pytest.mark.g03

FIXTURE = ROOT / "tests" / "fixtures" / "analytic_fixture.json"


def _base() -> dict:
    return load_example("analytic_single_pulse.json")


def _solve(raw: dict):
    cfg = make_config(raw)
    return solve(cfg, load_material_card(raw["material_card_file"]))


def test_zero_events_zero_removal():
    raw = _base()
    raw["path"]["segments"] = []
    res = _solve(raw)
    assert res.status == "completed"
    assert res.events_processed == 0
    assert res.statistics["removal_volume_internal"] == 0.0
    assert res.statistics["max_depth_internal"] == 0.0
    assert any("零事件" in n for n in res.metadata["validation"]["notes"])


def test_all_laser_off_zero_removal():
    raw = _base()
    raw["path"]["segments"][0]["laser_on"] = False
    res = _solve(raw)
    assert res.events_processed == 0
    assert res.statistics["removal_volume_internal"] == 0.0


def test_threshold_equality_gives_zero_depth():
    """F == Fth 时去除量为零（先掩膜后取对数）。"""
    import numpy as np

    law = FixedThresholdLogLaw(threshold_internal=FTH, delta_internal=DELTA)
    F = np.array([[FTH, FTH * (1 - 1e-15), 0.0, FTH * 3.0]])
    inc = law.increment(F, HistoryState(exposure_count=None))
    assert inc.values[0, 0] == 0.0
    assert inc.values[0, 1] == 0.0
    assert inc.values[0, 2] == 0.0
    assert inc.values[0, 3] > 0.0


def test_threshold_scaled_config_gives_zero_removal():
    """把光斑调到阈值以下：整场零去除，且不产生负值。"""
    raw = _base()
    raw["laser"]["pulse_energy_J"] = FTH * math.pi * W0 ** 2 / 2.0  # 峰值恰好等于阈值
    res = _solve(raw)
    assert res.status == "completed"
    assert res.statistics["removal_volume_internal"] == 0.0
    assert res.statistics["max_depth_internal"] == 0.0


@pytest.mark.parametrize(
    "field,value",
    [
        ("pulse_energy_J", -1.0),
        ("pulse_energy_J", 0.0),
        ("pulse_energy_J", float("nan")),
        ("spot_radius_m", -1e-5),
        ("spot_radius_m", 0.0),
        ("spot_radius_m", float("inf")),
    ],
)
def test_invalid_laser_parameters_rejected(field, value):
    raw = _base()
    raw["laser"][field] = value
    with pytest.raises(UFDemoError) as ei:
        make_config(raw)
    assert ei.value.code == CONFIG_INVALID
    assert ei.value.field_path == f"laser.{field}"


@pytest.mark.parametrize("value", [0.0, -1e-6, float("nan")])
def test_invalid_grid_spacing_rejected(value):
    raw = _base()
    raw["grid"]["dx_m"] = value
    with pytest.raises(UFDemoError) as ei:
        make_config(raw)
    assert ei.value.code == CONFIG_INVALID


def test_energy_power_conflict_rejected():
    raw = _base()
    raw["laser"]["average_power_W"] = EP * 1000.0 * 2.0  # 与 pulse_energy_J 明显不一致
    with pytest.raises(UFDemoError) as ei:
        make_config(raw)
    assert ei.value.code == "ENERGY_CONFLICT"
    assert ei.value.field_path == "laser.pulse_energy_J"


def test_energy_power_consistent_is_accepted():
    raw = _base()
    del raw["laser"]["pulse_energy_J"]
    raw["laser"]["average_power_W"] = EP * 1000.0
    raw["laser"]["repetition_rate_Hz"] = 1000.0
    cfg = make_config(raw)
    assert cfg.laser.pulse_energy_J == pytest.approx(EP, rel=1e-12)


def test_focus_outside_domain_does_not_renormalize_energy():
    """焦点远离计算域：仍记录发射能量，但不制造去除，也不把能量归一化回域内。"""
    raw = _base()
    # 脉冲横向位置来自轨迹点（正入射下焦点横向坐标由路径给出）
    raw["path"]["segments"][0]["start_xyz_m"] = [1.0, 0.0, 0.0]
    raw["path"]["segments"][0]["end_xyz_m"] = [1.0, 0.0, 0.0]
    res = _solve(raw)
    assert res.status == "completed"
    assert res.events_processed == 1
    assert res.statistics["removal_volume_internal"] == 0.0
    ledger = res.diagnostics["fluence_ledger"]
    assert ledger["emitted_energy_internal"] == pytest.approx(EP, rel=1e-12)
    assert ledger["estimated_intercepted_energy_internal"] == 0.0


def test_domain_truncation_is_not_normalized():
    """光斑远大于计算域：截获能量小于发射能量，且域内能流未被放大。"""
    raw = _base()
    raw["laser"]["spot_radius_m"] = 1e-3  # 1 mm，远大于 80 um 计算域
    res = _solve(raw)
    ledger = res.diagnostics["fluence_ledger"]
    assert ledger["estimated_intercepted_energy_internal"] < ledger["emitted_energy_internal"]
    assert ledger["max_domain_truncated_fraction"] > 0.9
    assert "不得解释为热损失" in ledger["note"]


def test_missing_delta_material_refuses_depth_request():
    """CFRP 卡缺 δ：请求 reference_case 深度必须报错，不悄悄切换合成模式。"""
    card = load_material_card(ROOT / "data" / "materials" / "cfrp_t700_yb01_800nm.json")
    assert card.response.get("delta_internal") is None
    raw = _base()
    raw["material_id"] = card.id
    del raw["material_card_file"]
    cfg = RunConfig.from_dict(raw)
    rep = validate_run(cfg, card)
    assert not rep.ok
    assert any(e["code"] == MATERIAL_CAPABILITY_MISSING for e in rep.errors)
    with pytest.raises(UFDemoError) as ei:
        build_pulse_law(card)
    assert ei.value.code == RESPONSE_SEMANTICS_INVALID


@pytest.mark.parametrize(
    "semantics",
    ["mean_depth_per_effective_pulse", "cumulative_depth", "track_or_pass_depth", "volume_per_energy"],
)
def test_non_increment_semantics_rejected_even_with_matching_shapes(semantics):
    """累计/平均/轨道/体积语义即使数组形状吻合也不能进入事件核。"""
    import numpy as np

    # 语义闸门在构造期即拒绝（快速失败），也在 increment 入口再次校验
    with pytest.raises(UFDemoError) as ei:
        law = FixedThresholdLogLaw(threshold_internal=FTH, delta_internal=DELTA, output_semantics=semantics)
        law.increment(np.array([[FTH * 2.0]]), HistoryState(exposure_count=None))
    assert ei.value.code == RESPONSE_SEMANTICS_INVALID


def test_nan_fluence_terminates_run_with_nonfinite_code():
    import numpy as np

    law = FixedThresholdLogLaw(threshold_internal=FTH, delta_internal=DELTA)
    with pytest.raises(UFDemoError) as ei:
        law.increment(np.array([[float("nan")]]), HistoryState(exposure_count=None))
    assert ei.value.code == "NUMERIC_NONFINITE"


def test_oblique_incidence_rejected_at_config_layer():
    raw = _base()
    raw["laser"]["direction_unit"] = [0.0, 0.5, 0.8660254037844386]
    cfg = make_config(raw)
    rep = validate_run(cfg, load_material_card(raw["material_card_file"]))
    assert not rep.ok
    assert any(e["code"] == "GEOMETRY_UNSUPPORTED" for e in rep.errors)


def test_grouped_mode_red_lines_rejected_at_config_layer():
    """批次 I（T17）：分组批量的红线在**配置层**拦截，不能只靠运行时回退。

    红线三条（细则 9.1 末）：分相结构、历史耦合、「批量 + 动态角度」。
    注意 acceleration="numba" 本身是**合法**的局部核后端（T16），不再被拒；
    缺失 numba 时回退 NumPy 并给出警告（见 acelerators.numba_available）。
    """
    card = load_material_card(_base()["material_card_file"])

    # 1) acceleration=numba 现在是合法后端（不再 NOT_IMPLEMENTED）
    raw = _base()
    raw["solver"]["acceleration"] = "numba"
    rep = validate_run(make_config(raw), card)
    assert rep.ok, [e["code"] for e in rep.errors]

    # 2) 分组 × 分相结构 → CONFIG_INVALID
    raw = _base()
    raw["solver"].update(mode="grouped", structured_interface=True)
    rep = validate_run(make_config(raw), card)
    assert not rep.ok
    assert any(e["code"] == "CONFIG_INVALID" for e in rep.errors)

    # 3) 分组 × 历史耦合 → CONFIG_INVALID
    raw = _base()
    raw["solver"].update(mode="grouped", history_enabled=True)
    rep = validate_run(make_config(raw), card)
    assert not rep.ok
    assert any(e["code"] == "CONFIG_INVALID" for e in rep.errors)

    # 4) 分组 × 动态角度 → CONFIG_INVALID（细则 9.1 末：两增强不得同时启用）
    raw = _base()
    raw["solver"].update(mode="grouped", dynamic_angle=True)
    rep = validate_run(make_config(raw), card)
    assert not rep.ok
    assert any(e["code"] == "CONFIG_INVALID" for e in rep.errors)


def test_threshold_only_reports_null_not_zero():
    """threshold_only：体积与深度写 null，不提供，而不是数值零。"""
    raw = _base()
    raw["run_mode"] = "threshold_only"
    res = _solve(raw)
    assert res.status == "completed"
    assert res.removal_available is False
    st = res.statistics
    assert st["removal_volume_internal"] is None
    assert st["center_depth_internal"] is None
    assert st["max_depth_internal"] is None
    assert st["removal_available"] is False
    assert res.profiles == []


def test_synthetic_mode_requires_explicit_selection():
    raw = _base()
    raw["unit_system"] = "dimensionless"
    raw["reference_scales"] = {"L_ref_m": 1e-5, "F_ref_J_m2": 1e4, "delta_ref_m": 1e-7}
    raw["run_mode"] = "reference_case"  # 无量纲却声明参考模式
    cfg = make_config(raw)
    rep = validate_run(cfg, load_material_card(raw["material_card_file"]))
    assert not rep.ok
    assert any(e["code"] == CONFIG_INVALID for e in rep.errors)
