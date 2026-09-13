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
import json
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


def test_oblique_incidence_admission_and_range_limits():
    """批次 J（T18）：斜入射已开放，但**超范围即停**（不裁剪角度继续）。

    旧断言「斜入射一律拒绝」是 M0 时期的行为，已随批次 J 过时。
    现在：范围内（≤60°）放行；超范围（>60°）拒绝；``k_z<=0`` 违反方向约定拒绝。
    """
    card = load_material_card(_base()["material_card_file"])

    # 30°：允许（范围内）
    raw = _base()
    raw["laser"]["direction_unit"] = [0.0, 0.5, 0.8660254037844386]
    rep = validate_run(make_config(raw), card)
    assert rep.ok, [e["code"] for e in rep.errors]
    assert any("斜入射" in w for w in rep.warnings)

    # 70°：超出软件支持范围 → 拒绝
    raw = _base()
    raw["laser"]["direction_unit"] = [math.sin(math.radians(70.0)), 0.0, math.cos(math.radians(70.0))]
    rep = validate_run(make_config(raw), card)
    assert not rep.ok
    errs = [e for e in rep.errors if e["code"] == "GEOMETRY_UNSUPPORTED"]
    assert errs
    assert "60" in json.dumps(errs[0], ensure_ascii=False)

    # k_z <= 0：违反「光轴正向」约定 → 拒绝（并指出约定）
    raw = _base()
    raw["laser"]["direction_unit"] = [0.0, 0.0, -1.0]
    rep = validate_run(make_config(raw), card)
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


# ---------------------------------------------------------------------------
# F10：M² 与瑞利长度的一致性（**期望值独立推导**，不复用被测实现里的式子）
#
# 回归背景：`config.py` 曾写 `zr_expected = π w0² · M² / λ`，而物理关系是
# `z_R = π w0² / (M² λ)`（M² 越大，实际束腰处发散越快、z_R 越短）。
#
# 为什么长期没被发现：
#   ① 仓库里**所有示例的 `m2` 都是 `null`**；
#   ② **没有任何测试设置 `laser.m2`** → 这条一致性检查**零覆盖**；
#   ③ `M²=1` 时两式恒等，所以只测 M²=1 也发现不了。
#
# 因此本节期望值一律由标准关系独立算出，**绝不从被测实现里取**，
# 否则实现与期望同源、测试恒真。
# ---------------------------------------------------------------------------

_M2_W0 = 10e-6  # m
_M2_LAM = 1030e-9  # m
_M2_VAL = 2.0
# 独立公式：z_R = π w0² / (M² λ)
_ZR_FROM_M2 = math.pi * _M2_W0**2 / (_M2_VAL * _M2_LAM)  # ≈ 152.5045 µm


def _laser_raw(**over) -> dict:
    """以示例为底，钉住波长/束腰/zR/M²，再按需覆盖。"""
    raw = copy.deepcopy(load_example("ten_pulses.json"))
    raw["laser"].update(
        {
            "wavelength_m": _M2_LAM,
            "spot_radius_m": _M2_W0,
            "rayleigh_range_m": None,
            "m2": None,
        }
    )
    raw["laser"].update(over)
    return raw


def test_m2_relation_is_divisor_not_multiplier():
    """先把「正确值」与「乘以 M² 的错值」都钉死，防止公式被改回去还蒙混过关。"""
    assert _ZR_FROM_M2 == pytest.approx(152.5045e-6, rel=1e-6)
    wrong = math.pi * _M2_W0**2 * _M2_VAL / _M2_LAM
    assert wrong == pytest.approx(610.0180e-6, rel=1e-6)
    assert wrong / _ZR_FROM_M2 == pytest.approx(4.0, rel=1e-12)


def test_rayleigh_m2_consistent_pair_is_accepted():
    """正确的 (zR, M²) 成对输入必须通过。"""
    cfg = make_config(_laser_raw(rayleigh_range_m=_ZR_FROM_M2, m2=_M2_VAL))
    assert cfg.laser.rayleigh_range_m == pytest.approx(_ZR_FROM_M2, rel=1e-12)


def test_rayleigh_m2_conflicting_pair_is_rejected_and_reports_implied():
    """把「旧公式的产物」（正确 zR 的 4 倍）当输入 → 必须**拒绝**。

    这条同时钉住公式方向：若实现仍是「× M²」，它算出的隐含值恰好等于这个错输入，
    于是会**放行**，测试即失败。
    """
    wrong = _ZR_FROM_M2 * 4.0
    with pytest.raises(UFDemoError) as ei:
        make_config(_laser_raw(rayleigh_range_m=wrong, m2=_M2_VAL))
    err = ei.value
    assert err.code == CONFIG_INVALID
    assert err.actual["m2"] == _M2_VAL
    assert err.actual["implied_rayleigh_range_m"] == pytest.approx(_ZR_FROM_M2, rel=1e-9)


def test_only_rayleigh_range_is_kept_as_declared():
    """只给 zR（不给 M²）→ 原样保留。"""
    cfg = make_config(_laser_raw(rayleigh_range_m=2e-5))
    assert cfg.laser.rayleigh_range_m == pytest.approx(2e-5, rel=1e-12)


def test_only_m2_derives_rayleigh_range_instead_of_collimated():
    """只给 M² 时必须**导出** zR。

    旧行为是 zR 留空 → `beam.w_of_s(w0, s, zR=None)` 把空值解释为**准直不发散**
    并直接返回 w0，于是「填了 M²」看起来生效、实际求解器按理想准直算。
    这里同时断言两种结果不同，确保不再静默退化。
    """
    from ufdemo.beam import w_of_s

    cfg = make_config(_laser_raw(m2=_M2_VAL))
    assert cfg.laser.rayleigh_range_m == pytest.approx(_ZR_FROM_M2, rel=1e-9)

    zr = cfg.laser.rayleigh_range_m
    w_at_zr = w_of_s(_M2_W0, zr, zr)
    # 在 zR 处光斑应放大 √2 倍；若仍按准直处理则恒为 w0
    assert w_at_zr == pytest.approx(_M2_W0 * math.sqrt(2.0), rel=1e-12)
    assert abs(w_at_zr - _M2_W0) > 1e-7


def test_m2_below_one_is_rejected():
    """M² 是光束质量因子，物理上 ≥ 1；小于 1 必须显式拒绝而非放行。"""
    with pytest.raises(UFDemoError) as ei:
        make_config(_laser_raw(m2=0.5))
    assert ei.value.code == CONFIG_INVALID


def test_m2_without_wavelength_is_rejected():
    """只给 M² 但缺波长 → 无法导出 zR，必须明确报错，不得静默按准直处理。"""
    with pytest.raises(UFDemoError) as ei:
        make_config(_laser_raw(wavelength_m=None, m2=_M2_VAL))
    assert ei.value.code == CONFIG_INVALID
