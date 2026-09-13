"""U06：金刚石过程响应评估器回归。

守三类事：

1. **可复现** —— 指标与审计包实测对齐（分组五折深度 MAPE 52.42%）；
2. **诚实** —— 差结果必须**被输出**，且必须判「未达门槛」；
3. **边界** —— 它是过程响应、不是脉冲律；不产生形貌；不外推。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from ufdemo.errors import CONFIG_INVALID, UFDemoError
from ufdemo.evaluators import (
    DEFAULT_ALPHA,
    GROUPED_CV_SEED,
    INPUTS,
    NOT_a_pulse_law,
    OUTPUTS,
    ProcessEvaluator,
    QuadraticResponseSurface,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "measured" / "diamond_rsm_measured.csv"

pytestmark = pytest.mark.u06

# 审计包独立实测的参考值（本包须复现；允许浮点末位差异）
REF_HOLD_MAPE = {"width_um": 1.34, "depth_um": 3.26, "Ra_um": 6.09}
REF_GROUPED_DEPTH_MAPE = 52.42
REF_GROUPED_DEPTH_RMSE = 7.61
REF_HOLD_PRED = {"width_um": 46.0911, "depth_um": 20.0347, "Ra_um": 1.16696}


@pytest.fixture(scope="module")
def rows() -> list[dict[str, str]]:
    if not DATA.exists():
        pytest.skip("金刚石数据未导入")
    return list(csv.DictReader(DATA.read_text(encoding="utf-8-sig").splitlines()))


@pytest.fixture(scope="module")
def ev(rows) -> ProcessEvaluator:
    return ProcessEvaluator.from_rows(rows)


# ---------------------------------------------------------------------------
# 1. 结构：17 训练 + 1 留出 + 13 种工况
# ---------------------------------------------------------------------------


def test_row_split_is_17_plus_1(ev):
    assert ev.n_training_rows == 17
    assert len(ev.held_out_case_ids) == 1
    assert ev.held_out_case_ids[0] == "D01-18"


def test_distinct_conditions_is_13(ev):
    """17 行里只有 13 种不同工况（中心点重复 5 次）。"""
    assert ev.n_distinct_conditions == 13


# ---------------------------------------------------------------------------
# 2. 留出行**未参与拟合**
# ---------------------------------------------------------------------------


def test_holdout_row_not_used_in_fit(ev, rows):
    """去掉留出行后系数**逐位不变** —— 这是「留出」二字成立的硬证据。"""
    train_only = [r for r in rows if r["split_role"] == "calibration_candidate"]
    ev2 = ProcessEvaluator.from_rows(train_only)
    assert ev2.surface.coef_ is not None and ev.surface.coef_ is not None
    delta = abs(ev.surface.coef_ - ev2.surface.coef_).max()
    assert delta == 0.0, f"去掉留出行后系数变了 {delta}，说明留出行混进了拟合"


# ---------------------------------------------------------------------------
# 3. 复现审计指标
# ---------------------------------------------------------------------------


def test_holdout_predictions_match_audit(ev):
    """留出点预测须与审计包实测一致（相对差 ≤ 1e-6）。"""
    p = ev.predict({"power_W": 14.7, "scan_speed_m_s": 2.77, "passes": 117})
    for k, ref in REF_HOLD_PRED.items():
        got = p.outputs[k]
        assert abs(got - ref) / ref <= 1e-6, f"{k}: {got} vs 参考 {ref}"


def test_grouped_cv_reproduces_audit_numbers(rows):
    """分组五折的**差结果**必须能复现：深度 MAPE ≈ 52.42%、RMSE ≈ 7.61 μm。

    这条测试的用意是**钉住差结果**：如果将来有人为了让报告好看而
    改成随机划分、或把重复工况拆散，这两个数会显著变好 —— 测试就会失败。
    """
    import numpy as np

    from ufdemo.evaluators import design

    train = [r for r in rows if r["split_role"] == "calibration_candidate"]
    x = np.array([[float(r[k]) for k in INPUTS] for r in train])
    y = np.array([[float(r[k]) for k in OUTPUTS] for r in train])
    groups = np.array([r["condition_group"] for r in train])
    unique = np.unique(groups)
    rng = np.random.default_rng(GROUPED_CV_SEED)
    shuffled = unique.copy()
    rng.shuffle(shuffled)

    oof = np.full_like(y, np.nan)
    for vg in np.array_split(shuffled, 5):
        va = np.isin(groups, vg)
        tr = ~va
        assert not (set(groups[tr]) & set(groups[va])), "同一工况跨了训练/验证两侧"
        surf = QuadraticResponseSurface(alpha=DEFAULT_ALPHA).fit(x[tr], y[tr])
        oof[va] = design(x[va]) @ surf.coef_
    assert not np.isnan(oof).any()

    j = OUTPUTS.index("depth_um")
    d = oof[:, j] - y[:, j]
    mape = float(100.0 * np.mean(np.abs(d / y[:, j])))
    rmse = float(np.sqrt(np.mean(d**2)))
    assert mape == pytest.approx(REF_GROUPED_DEPTH_MAPE, abs=0.01), f"深度分组 MAPE 变了：{mape}"
    assert rmse == pytest.approx(REF_GROUPED_DEPTH_RMSE, abs=0.01), f"深度分组 RMSE 变了：{rmse}"


# ---------------------------------------------------------------------------
# 4. 门槛判定：必须判「未达」
# ---------------------------------------------------------------------------


def test_validation_verdict_is_not_passed(ev):
    """52.42% 远超自设门槛 10% → 必须判「未达预设门槛」。

    把它做成**代码判定**而不是靠人记得写一句 —— 否则报告很容易
    悄悄变成「已验证」。
    """
    v = ev.validation_verdict(REF_GROUPED_DEPTH_MAPE)
    assert v["passed"] is False
    assert "未达" in v["verdict"] and "已验证" in v["verdict"]


def test_verdict_would_pass_under_good_numbers(ev):
    """反向用例：若误差真的够小，判定应能通过（证明门槛不是恒假）。"""
    assert ev.validation_verdict(1.0)["passed"] is True


# ---------------------------------------------------------------------------
# 5. 边界：不是脉冲律、不产生形貌、不外推
# ---------------------------------------------------------------------------


def test_module_declares_not_a_pulse_law():
    assert NOT_a_pulse_law is True


def test_prediction_outputs_only_scalars(ev):
    """输出只有**标量三元组**：没有网格、没有高度场、没有数组。"""
    p = ev.predict({"power_W": 11.1, "scan_speed_m_s": 2.0, "passes": 100})
    assert set(p.outputs) == set(OUTPUTS)
    for k, v in p.outputs.items():
        assert isinstance(v, float), f"{k} 不是标量：{type(v)}"
    d = p.to_dict()
    assert d["NOT_a_pulse_law"] is True
    assert d["is_process_response_only"] is True
    # 不得出现任何形貌/网格字段
    for banned in ("height", "heightfield", "grid", "surface", "depth_map", "profile"):
        assert banned not in d, f"输出里出现了形貌相关字段 {banned!r}"


def test_extrapolation_is_rejected_by_default(ev):
    """超出采样箱 → 拒绝，不静默外推。"""
    with pytest.raises(UFDemoError) as ei:
        ev.predict({"power_W": 40.0, "scan_speed_m_s": 2.0, "passes": 100})
    assert ei.value.code == CONFIG_INVALID


def test_support_report_flags_outside_box(ev):
    s = ev.surface.check_support([[40.0, 2.0, 100.0]])
    assert s.inside_box is False
    assert "power_W" in s.outside
    # 「在箱内」不等于有支撑 —— 提示语必须带上这句
    assert "不是支撑保证" in s.note or "必要条件" in s.note


def test_missing_input_is_rejected(ev):
    with pytest.raises(UFDemoError) as ei:
        ev.predict({"power_W": 11.1, "passes": 100})  # 缺 scan_speed_m_s
    assert ei.value.code == CONFIG_INVALID


def test_bad_alpha_rejected():
    with pytest.raises(UFDemoError):
        QuadraticResponseSurface(alpha=0.0)
    with pytest.raises(UFDemoError):
        QuadraticResponseSurface(alpha=float("nan"))


# ---------------------------------------------------------------------------
# 6. 报告必须如实写出差结果
# ---------------------------------------------------------------------------


def test_report_states_grouped_cv_failure():
    """报告里必须出现分组五折的差数字，且明确说不得标「已验证」。

    只报留出 3.26% 而不报分组 52.42%，是选择性呈现 —— 这条测试防止它发生。
    """
    md = ROOT / "docs" / "reports" / "diamond_evaluator.md"
    if not md.exists():
        pytest.skip("报告尚未生成（先跑 tools/diamond_evaluator_report.py）")
    text = md.read_text(encoding="utf-8")
    assert "52.42" in text, "报告必须写明分组五折深度 MAPE"
    assert "7.61" in text, "报告必须写明分组五折深度 RMSE"
    assert "不得标注「已验证」" in text or "未达预设门槛" in text
    assert "不是逐事件去除律" in text or "NOT_a_pulse_law" in text
