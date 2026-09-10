"""G05：文献语义回归（执行细则 10 节、任务书 3.1/3.2 节、10 节 G05）。

覆盖：

* YSZ 式(9) ``N_eff = (pi/4)*(2*w0*f)/v``：反解得 ``278.9734276388 mm/s``（rel <= 1e-12）；
  错误换算 ``2*w0*f/N`` 得 ``355.2 mm/s``，断言两者**确实不同**；
* YSZ 条件一致性：``w0=16 um``、峰值 ``50.1 J/cm^2`` → ``201.464053689406 uJ``；
* SiC 式(6) ``Fth(1)=2.35 J/cm^2``、``N->inf`` 趋向 ``0.70 J/cm^2``、``k=0.0199``；
* 两套有效 N **不共用一个公式**（YSZ 有 pi/4、SiC 乘遍数 K）；
* 平均率与协议累计深度分别输出，且**都不能进入逐事件增量主循环**；
* 参考条件不匹配时拒绝（CONDITION_MISMATCH）。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from conftest import EXAMPLES, ROOT
from ufdemo import references as R
from ufdemo.config import SEMANTIC_CUMULATIVE, SEMANTIC_EVENT_INCREMENT, SEMANTIC_MEAN_RATE
from ufdemo.errors import CONDITION_MISMATCH, CONFIG_INVALID, RESPONSE_SEMANTICS_INVALID, UFDemoError
from ufdemo.materials import load_material_card
from ufdemo.response import assert_increment_semantics

pytestmark = pytest.mark.g05

YSZ_CARD = ROOT / "data" / "materials" / "zirconia_ysz_machining_effective_n3.json"
SIC_CARD = ROOT / "data" / "materials" / "sic_4h_cface_1035nm_multishot.json"

# 任务书 3.1 / G05 的目标值
YSZ_W0 = 16.0e-6
YSZ_F = 33300.0
YSZ_NEFF = 3.0
YSZ_SPEED_MM_S = 278.9734276388
NAIVE_SPEED_MM_S = 355.2

YSZ_PEAK_FLUENCE = 50.1e4          # 50.1 J/cm^2 -> J/m^2
YSZ_PULSE_ENERGY_UJ = 201.464053689406

SIC_K = 0.0199


def _rel(a: float, b: float) -> float:
    return abs(a - b) / abs(b)


# ---------------------------------------------------------------------------
# YSZ：有效 N 换算（式 9）
# ---------------------------------------------------------------------------


def test_ysz_speed_matches_task_plan_value():
    """G05 主断言：v = (pi/4)*(2*w0*f)/N_eff = 278.9734276388 mm/s。"""
    v = R.ysz_speed_for_effective_count(YSZ_W0, YSZ_F, YSZ_NEFF)
    assert _rel(v * 1e3, YSZ_SPEED_MM_S) <= 1e-12, f"got {v * 1e3!r} mm/s"


def test_ysz_effective_count_roundtrip():
    """正解与反解必须互逆，且 N_eff 的定义标识写对（不是 SiC 定义）。"""
    v = R.ysz_speed_for_effective_count(YSZ_W0, YSZ_F, YSZ_NEFF)
    eff = R.ysz_effective_count(YSZ_W0, YSZ_F, v)
    assert _rel(eff.value, YSZ_NEFF) <= 1e-12
    assert eff.definition == R.YSZ_EFFECTIVE_COUNT_DEFINITION
    assert eff.definition != R.SIC_EFFECTIVE_COUNT_DEFINITION
    assert "pi/4" in eff.formula


def test_naive_formula_is_actually_wrong():
    """2*w0*f/N 得 355.2 mm/s，与式(9) 相差约 27.3%；不得用于任何输出。"""
    naive = R.naive_speed_for_effective_count(YSZ_W0, YSZ_F, YSZ_NEFF)
    assert _rel(naive * 1e3, NAIVE_SPEED_MM_S) <= 1e-12
    correct = R.ysz_speed_for_effective_count(YSZ_W0, YSZ_F, YSZ_NEFF)
    assert abs(naive - correct) / correct > 0.2


def test_ysz_peak_fluence_to_pulse_energy():
    """条件一致性检查：E = F0*pi*w0^2/2 = 201.464053689406 uJ。"""
    e = R.gaussian_pulse_energy(YSZ_PEAK_FLUENCE, YSZ_W0)
    assert _rel(e * 1e6, YSZ_PULSE_ENERGY_UJ) <= 1e-12
    # 反向恒等
    assert _rel(R.gaussian_peak_fluence(e, YSZ_W0), YSZ_PEAK_FLUENCE) <= 1e-14


def test_ysz_reference_result_labels_and_no_depth():
    """YSZ 参考结果：不产生深度、不冒充实验复现、不声明可物理预测。"""
    case = R.load_reference_case(EXAMPLES / "ysz_reference_case.json")
    material = load_material_card(YSZ_CARD)
    res = R.ReferenceEvaluator.evaluate_case(case, material)
    assert res.event_kernel_allowed is False
    assert res.verified_by_formula is True
    assert res.verified_by_experiment is False
    assert res.output_semantics == R.SEMANTIC_THRESHOLD_ONLY
    assert res.condition_match == "matched_card_protocol"
    assert _rel(res.values["speed_mm_s"], YSZ_SPEED_MM_S) <= 1e-12
    assert _rel(res.values["pulse_energy_uJ"], YSZ_PULSE_ENERGY_UJ) <= 1e-12
    # 错误换算只作对照量存在
    assert _rel(res.values["naive_speed_mm_s"], NAIVE_SPEED_MM_S) <= 1e-12


# ---------------------------------------------------------------------------
# SiC：阈值函数与平均率（式 5/6/7）
# ---------------------------------------------------------------------------


def test_sic_threshold_endpoints_and_monotone():
    """Fth(1)=2.35、Fth(N->inf)->0.70 J/cm^2，k=0.0199，且随 N 单调不增。"""
    kw = dict(f1_J_m2=23500.0, f_inf_J_m2=7000.0, k_per_pulse=SIC_K)
    assert _rel(R.sic_threshold_fluence(1.0, **kw), 23500.0) <= 1e-15
    assert _rel(R.sic_threshold_fluence(1e9, **kw), 7000.0) <= 1e-9
    prev = math.inf
    for n in (1, 2, 5, 10, 50, 100, 720, 1e4):
        cur = R.sic_threshold_fluence(float(n), **kw)
        assert cur <= prev + 1e-9
        assert 7000.0 - 1e-6 <= cur <= 23500.0 + 1e-6
        prev = cur


def test_sic_threshold_card_parameters_are_used():
    """阈值参数取自材料卡 multi_response，而不是算例里硬编码。"""
    card = load_material_card(SIC_CARD)
    m = card.multi_response
    assert _rel(m["Fth1_internal"], 23500.0) <= 1e-15
    assert _rel(m["Fth_infinity_internal"], 7000.0) <= 1e-15
    assert _rel(m["k_inc_per_pulse"], SIC_K) <= 1e-15
    assert _rel(m["delta_eff_mean_m"], 22.4e-9) <= 1e-12


def test_sic_effective_count_uses_passes_not_ysz_factor():
    """SiC 式(5) 含遍数 K、**没有** pi/4 因子；与 YSZ 定义不可互换。"""
    w0, f, v = 19.0e-6, 200000.0, 0.2
    eff1 = R.sic_effective_count(w0, f, v, passes=1)
    eff2 = R.sic_effective_count(w0, f, v, passes=4)
    assert _rel(eff1.value, 2 * w0 * f / v) <= 1e-15
    assert _rel(eff2.value, 4 * eff1.value) <= 1e-15
    ysz = R.ysz_effective_count(w0, f, v)
    assert _rel(ysz.value, eff1.value * (math.pi / 4.0)) <= 1e-15
    assert eff1.definition == R.SIC_EFFECTIVE_COUNT_DEFINITION
    with pytest.raises(UFDemoError) as ei:
        R.sic_effective_count(w0, f, v, passes=0)
    assert ei.value.code == CONFIG_INVALID


def test_sic_evaluator_mean_and_cumulative_are_separate():
    """平均率与协议累计深度分别输出；累计 = 平均 × N_eff；不自动累加到网格。"""
    case = R.load_reference_case(EXAMPLES / "sic_reference_case.json")
    material = load_material_card(SIC_CARD)
    res = R.ReferenceEvaluator.evaluate_case(case, material)

    assert res.output_semantics == SEMANTIC_MEAN_RATE
    assert res.event_kernel_allowed is False
    assert res.verified_by_formula is True
    assert res.verified_by_experiment is False

    v = res.values
    # primary 行取卡中声明的协议有效 N = 720（不是 N->inf 探针）
    assert _rel(v["effective_count"], 720.0) <= 1e-12
    assert _rel(v["threshold_J_cm2"], 0.7000010082194416) <= 1e-9
    assert _rel(
        v["protocol_cumulative_depth_m"],
        v["mean_depth_per_effective_pulse_m"] * v["effective_count"],
    ) <= 1e-12
    # 完整扫描仍然保留，且 N=1 的阈值就是 2.35 J/cm^2
    sweep = v["sweep"]
    assert len(sweep) == 6
    assert _rel(sweep[0]["threshold_J_cm2"], 2.35) <= 1e-15
    assert _rel(sweep[-1]["threshold_J_cm2"], 0.7) <= 1e-9
    # 评估器没有触碰事件模型
    assert res.engineering_extension is not None


# ---------------------------------------------------------------------------
# 语义闸门：平均率/累计量不得进入逐事件核
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("semantics", [SEMANTIC_MEAN_RATE, SEMANTIC_CUMULATIVE, R.SEMANTIC_THRESHOLD_ONLY])
def test_mean_and_cumulative_rejected_by_event_kernel(semantics):
    """即使数组形状吻合，参考语义也不能通过事件核闸门。"""
    with pytest.raises(UFDemoError) as ei:
        assert_increment_semantics(semantics)
    assert ei.value.code == RESPONSE_SEMANTICS_INVALID


def test_event_increment_rejected_by_reference_gate():
    """反方向闸门：逐事件增量曲线不得冒充参考曲线。"""
    with pytest.raises(UFDemoError) as ei:
        R.assert_reference_only_semantics(SEMANTIC_EVENT_INCREMENT)
    assert ei.value.code == RESPONSE_SEMANTICS_INVALID
    assert R.assert_reference_only_semantics(SEMANTIC_MEAN_RATE) == SEMANTIC_MEAN_RATE


# ---------------------------------------------------------------------------
# 条件匹配与结构校验
# ---------------------------------------------------------------------------


def test_condition_mismatch_is_rejected():
    """参考条件超出卡内允许误差时拒绝，不自动切换模式。"""
    case = R.load_reference_case(EXAMPLES / "ysz_reference_case.json")
    case = dict(case)
    case["laser"] = dict(case["laser"])
    case["laser"]["wavelength_m"] = 1.5e-6  # 与 1030 nm 相差甚远
    material = load_material_card(YSZ_CARD)
    with pytest.raises(UFDemoError) as ei:
        R.ReferenceEvaluator.evaluate_case(case, material)
    assert ei.value.code == CONDITION_MISMATCH


def test_missing_laser_conditions_allows_paper_direct_n():
    """算例不给条件时允许直接用论文有效 N，但必须显式标注。"""
    case = R.load_reference_case(EXAMPLES / "ysz_reference_case.json")
    case = {k: v for k, v in case.items() if k != "laser"}
    material = load_material_card(YSZ_CARD)
    res = R.ReferenceEvaluator.evaluate_case(case, material)
    assert res.condition_match == "paper_direct_effective_n"
    assert any("论文有效 N" in w for w in res.warnings)


def test_case_structure_validation(tmp_path: Path):
    """缺少必需字段 / 文件不存在 / 非有限值都必须报错。"""
    with pytest.raises(UFDemoError) as ei:
        R.load_reference_case(EXAMPLES / "does_not_exist_reference_case.json")
    assert ei.value.code == CONFIG_INVALID

    # 缺 case_id
    bad = tmp_path / "bad_no_case_id.json"
    bad.write_text('{"reference_kind": "ysz_effective_n", "material_id": "x"}', encoding="utf-8")
    with pytest.raises(UFDemoError) as ei2:
        R.load_reference_case(bad)
    assert ei2.value.code == CONFIG_INVALID
    assert ei2.value.field_path == "case_id"

    # 未知 reference_kind
    bad2 = tmp_path / "bad_kind.json"
    bad2.write_text(
        '{"case_id": "x", "reference_kind": "nope", "material_id": "y"}', encoding="utf-8"
    )
    with pytest.raises(UFDemoError) as ei3:
        R.load_reference_case(bad2)
    assert ei3.value.code == CONFIG_INVALID
    assert ei3.value.field_path == "reference_kind"

    # 非有限值
    with pytest.raises(UFDemoError):
        R.sic_threshold_fluence(float("nan"), f1_J_m2=23500.0, f_inf_J_m2=7000.0, k_per_pulse=SIC_K)


def test_sic_evaluator_requires_material_card():
    """SiC 参数来自卡；不给卡必须拒绝，不能靠算例里的默认值。"""
    case = R.load_reference_case(EXAMPLES / "sic_reference_case.json")
    with pytest.raises(UFDemoError) as ei:
        R.ReferenceEvaluator.evaluate_case(case, None)
    assert ei.value.code == CONFIG_INVALID


# ---------------------------------------------------------------------------
# 端到端：reference 子命令写出标准运行目录，且不产生表面文件
# ---------------------------------------------------------------------------


def test_cli_reference_writes_run_dir_without_surface(tmp_path: Path):
    import json

    from ufdemo.__main__ import main
    from ufdemo.io import is_completed

    out = tmp_path / "g05_ysz"
    rc = main(["reference", str(EXAMPLES / "ysz_reference_case.json"), "--out", str(out)])
    assert rc == 0
    assert is_completed(out)

    files = {p.name for p in out.iterdir()}
    assert {"config.json", "material_snapshot.json", "metadata.json", "statistics.csv", "diagnostics.json"} <= files
    assert not any("surface" in f for f in files), "参考评估器不得写出形貌表面文件"

    diag = json.loads((out / "diagnostics.json").read_text(encoding="utf-8"))
    assert diag["diagnostics"]["event_model_touched"] is False
    assert diag["diagnostics"]["reference"]["event_kernel_allowed"] is False

    # 参考运行目录已存在时拒绝覆盖
    rc2 = main(["reference", str(EXAMPLES / "ysz_reference_case.json"), "--out", str(out)])
    assert rc2 == 1
