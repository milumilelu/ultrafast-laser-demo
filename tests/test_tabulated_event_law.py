"""U07：查表逐事件核（``TabulatedEventLaw``）与语义闸门回归。

这是**唯一改动主循环**的任务，直接触碰全项目最核心的语义红线，
所以本文件按细则的顺序组织：

1. **先固化既有闸门的负例**（错语义 → 拒绝；错条件 → 拒绝；缺规则 → 拒绝）；
2. 再做**可判别算例**（查表核与对数律给出不同结果，且结果与 ``tables.lookup`` 逐点一致）；
3. 最后覆盖**边界与越界**（下界、上界、正好落在端点）。

红线：**任何**「为了接通」而放宽 ``output_semantics`` 或绕过闸门的做法都不得通过。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from ufdemo import tables as T
from ufdemo import ui_service as U
from ufdemo.config import SEMANTIC_EVENT_INCREMENT
from ufdemo.errors import CONDITION_MISMATCH, RESPONSE_SEMANTICS_INVALID, UFDemoError
from ufdemo.response import FixedThresholdLogLaw, TabulatedEventLaw, build_pulse_law

ROOT = Path(__file__).resolve().parents[1]
CURVES = ROOT / "data" / "curves"

pytestmark = pytest.mark.u07


# ---------------------------------------------------------------------------
# 0. 夹具：一条**测试专用**的最小曲线卡（不落盘、不进 data/curves）
# ---------------------------------------------------------------------------


def _card(**over) -> dict:
    cid = over.pop("curve_id", "u07_probe_curve")
    card = {
        "schema_version": "1.0",
        "curve_id": cid,
        "material_id": "u07_test_fixture",
        "material_identity": {"family": "mathematical_fixture", "grade": None},
        "x_quantity": {"name": "peak_fluence", "unit": "J/cm^2", "kind": "fluence"},
        "y_quantity": {"name": "removal_depth_per_pulse", "unit": "m", "kind": "depth"},
        "output_semantics": SEMANTIC_EVENT_INCREMENT,
        "depth_direction": "surface_normal",
        "fluence_basis": "incident_surface_peak",
        "fixed_conditions": {"repetition_rate_Hz": {"value": 1000.0, "rel_tol": 0.0}},
        "protocol": {"protocol_id": "u07_fixture"},
        "source_figure_or_table": "U07 测试专用（不落盘）",
        "valid_range": {"x": [2.0, 20.0], "note": "夹具"},
        "points_file": f"{cid}.points.csv",
        "duplicate_policy": "reject",
        "evidence_status": "unverified",
        "source_type": "analytic_test_definition",
        "entry_class": "fixture",
    }
    card.update(over)
    return card


@pytest.fixture
def probe_dir(tmp_path: Path) -> Path:
    """把一条曲线卡写进临时目录（走真实 ``load_curve`` 校验路径）。"""
    cid = "u07_probe_curve"
    card = _card(curve_id=cid)
    (tmp_path / f"{cid}.curve.json").write_text(
        json.dumps(card, ensure_ascii=False), encoding="utf-8")
    # y = 0.05 * x（线性），便于手算期望值
    lines = ["# u07 fixture", "x,y"]
    for x in (2.0, 5.0, 10.0, 20.0):
        lines.append(f"{x!r},{0.05 * x!r}")
    (tmp_path / f"{cid}.points.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def probe_curve(probe_dir: Path):
    return U.load_curve_card(probe_dir, "u07_probe_curve.curve.json")


# ---------------------------------------------------------------------------
# 1. 先固化**既有闸门**的负例行为
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sem", [
    "cumulative_depth", "mean_depth_per_effective_pulse", "volume_per_energy",
    "track_or_pass_depth", "threshold_only",
])
def test_gate_rejects_non_increment_semantics(tmp_path: Path, sem):
    """非 ``event_depth_increment`` 曲线**不得**构造查表核；错误码必须是既有的。"""
    cid = f"u07_bad_{sem}"
    # y 轴要与语义自洽，否则会在**加载期**先被拒（那是另一道更早的闸）——
    # 这里要测的是**构造期**的语义闸门，所以要让它能加载成功。
    if sem == "volume_per_energy":
        yq = {"name": "removal_volume_per_energy", "unit": "m^3/J", "kind": "volume"}
    elif sem == "threshold_only":
        yq = {"name": "threshold_fluence", "unit": "J/cm^2", "kind": "fluence"}
    else:
        yq = {"name": "removal_depth_per_pulse", "unit": "m", "kind": "depth"}
    card = _card(curve_id=cid, output_semantics=sem, y_quantity=yq)
    if sem == "volume_per_energy":
        # 体积曲线还不得声明 depth_direction（加载期另一道闸）——
        # 这说明防御是分层的：先拦「语义与纵轴不符」，再拦「语义与方向不符」。
        card.pop("depth_direction", None)
    (tmp_path / f"{cid}.curve.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    (tmp_path / f"{cid}.points.csv").write_text("x,y\n2.0,0.1\n20.0,1.0\n", encoding="utf-8")
    curve = U.load_curve_card(tmp_path, f"{cid}.curve.json")
    with pytest.raises(UFDemoError) as ei:
        TabulatedEventLaw(curve)
    assert ei.value.code == RESPONSE_SEMANTICS_INVALID, "必须是既定错误码，不得新造"
    assert ei.value.code != "TABLE_OUT_OF_RANGE"


def test_gate_rejects_condition_mismatch(probe_curve):
    """固定条件与运行条件不符 → ``CONDITION_MISMATCH``（复用既有比对逻辑）。"""
    with pytest.raises(UFDemoError) as ei:
        TabulatedEventLaw(probe_curve, laser={"repetition_rate_Hz": 200_000.0})
    assert ei.value.code == CONDITION_MISMATCH


def test_gate_allows_matching_condition(probe_curve):
    TabulatedEventLaw(probe_curve, laser={"repetition_rate_Hz": 1000.0})  # 不抛


def test_unknown_threshold_rule_mode_rejected(probe_curve):
    """未知的 threshold_rule.mode 必须拒绝。"""
    with pytest.raises(UFDemoError) as ei:
        TabulatedEventLaw(probe_curve, threshold_rule={"mode": "whatever"})
    assert ei.value.code == RESPONSE_SEMANTICS_INVALID


def test_zero_below_requires_reason(probe_curve):
    """``zero_below`` 必须**带理由**，否则「声明」会退化成随手填默认值。"""
    with pytest.raises(UFDemoError) as ei:
        TabulatedEventLaw(probe_curve, threshold_rule={"mode": "zero_below"})
    assert ei.value.code == RESPONSE_SEMANTICS_INVALID
    # 带理由则允许
    TabulatedEventLaw(probe_curve, threshold_rule={
        "mode": "zero_below", "reason": "该曲线附独立阈值律（测试）"})


# ---------------------------------------------------------------------------
# 2. 越界语义：不返回 0、不外推、不钳端点
# ---------------------------------------------------------------------------


def test_below_range_rejected_by_default(probe_curve):
    """**低于下界默认拒绝** —— 低于量测区间不等于无去除。"""
    law = TabulatedEventLaw(probe_curve)
    with pytest.raises(UFDemoError) as ei:
        law.increment(np.array([[1.0e4]]), None)
    assert ei.value.code == "TABLE_OUT_OF_RANGE"


def test_above_range_rejected_never_extrapolated(probe_curve):
    """**高于上界一律拒绝**，不外推、不钳到端点。"""
    law = TabulatedEventLaw(probe_curve)
    with pytest.raises(UFDemoError) as ei:
        law.increment(np.array([[1e10]]), None)
    assert ei.value.code == "TABLE_OUT_OF_RANGE"


def test_zero_below_mode_records_zero_only_when_declared(probe_curve):
    """显式声明 ``zero_below`` 时下界以下记 0；**上界以上仍然拒绝**。"""
    law = TabulatedEventLaw(probe_curve, threshold_rule={
        "mode": "zero_below", "reason": "测试用：假定下界以下无去除"})
    res = law.increment(np.array([[1.0e4, 1.0e5]]), None)
    v = np.asarray(res.values)
    assert v[0, 0] == 0.0            # 显式声明才记 0
    assert v[0, 1] == pytest.approx(0.05 * 10.0)
    with pytest.raises(UFDemoError):
        law.increment(np.array([[1e10]]), None)   # 上界仍拒绝


def test_endpoints_are_inclusive(probe_curve):
    """正好落在两端点应可查（闭区间）。"""
    law = TabulatedEventLaw(probe_curve)
    res = law.increment(np.array([[2.0e4, 2.0e5]]), None)
    v = np.asarray(res.values)
    assert v[0, 0] == pytest.approx(0.05 * 2.0)
    assert v[0, 1] == pytest.approx(0.05 * 20.0)


def test_history_enabled_rejected(probe_curve):
    """查表核没有历史耦合：开启历史必须显式拒绝，而不是静默忽略。"""
    from ufdemo.response import HistoryState

    law = TabulatedEventLaw(probe_curve)
    with pytest.raises(UFDemoError) as ei:
        law.increment(np.array([[1.0e5]]), HistoryState(exposure_count=np.zeros((1, 1), dtype=np.uint32)))
    assert ei.value.code == RESPONSE_SEMANTICS_INVALID


# ---------------------------------------------------------------------------
# 3. 可判别算例：与 tables.lookup **逐点一致**，且与对数律结果不同
# ---------------------------------------------------------------------------


def test_values_match_tables_lookup_pointwise(probe_curve):
    """查表核的每个取值都必须等于 ``tables.lookup``（同一插值实现）。"""
    law = TabulatedEventLaw(probe_curve)
    xs_si = [2.0e4, 3.7e4, 5.0e4, 7.3e4, 1.0e5, 1.39e5, 2.0e5]   # J/m²（内部 SI）
    res = law.increment(np.array([xs_si]), None)
    got = np.asarray(res.values)[0]
    ref = T.lookup(probe_curve, [v / 1e4 for v in xs_si], method="linear").values  # SI → J/cm²
    for a, b in zip(got, ref):
        assert a == pytest.approx(b, rel=1e-12)


def test_discriminating_case_differs_from_log_law(probe_curve):
    """**可判别算例**：查表核与对数律在同一能流下给出**不同**结果。

    若两者恰好相同，就证明不了「查表真的接进了主循环」——
    这条测试的意义正在于「结果确实被数据改变了」。
    """
    law = TabulatedEventLaw(probe_curve)
    # 阈值/去除尺度按曲线自身单位（J/cm^2）给，且查询点落在 [2, 20] 的有效区间内。
    log_law = FixedThresholdLogLaw(threshold_internal=1.0, delta_internal=1e-5)
    F = np.array([[1.0e5]])   # J/m²（内部 SI）
    tab = float(np.asarray(law.increment(F, None).values)[0, 0])
    log = float(np.asarray(log_law.increment(F, None).values)[0, 0])
    assert tab > 0.0 and log > 0.0
    assert abs(tab - log) > 1e-9, "查表核与对数律结果相同，无法证明查表已接入"


def test_increment_is_per_event_not_amortized(probe_curve):
    """**逐事件**推进：同一能流查两次各得一份增量，不是把累计量摊到 N 次。"""
    law = TabulatedEventLaw(probe_curve)
    a = float(np.asarray(law.increment(np.array([[1.0e5]]), None).values)[0, 0])
    b = float(np.asarray(law.increment(np.array([[1.0e5]]), None).values)[0, 0])
    assert a == pytest.approx(b), "两次同样输入应给同样增量（逐事件、无状态）"
    assert a == pytest.approx(0.05 * 10.0)


# ---------------------------------------------------------------------------
# 4. build_pulse_law 分派
# ---------------------------------------------------------------------------


def test_build_pulse_law_without_curve_is_unchanged():
    """不传 curve 时行为与改动前一致（仍是固定阈值对数律）。"""
    class Mat:
        response = {"threshold_internal": 1e4, "delta_internal": 1e-7, "kind": "log_fixed"}

    law = build_pulse_law(Mat())
    assert isinstance(law, FixedThresholdLogLaw)
    assert not isinstance(law, TabulatedEventLaw)


def test_build_pulse_law_dispatches_to_tabulated(probe_curve):
    """传了增量语义曲线 → 分派到查表核。"""
    class Mat:
        response = {}
        laser_conditions: dict = {}

    law = build_pulse_law(Mat(), curve=probe_curve)
    assert isinstance(law, TabulatedEventLaw)
    assert law.kind == "table_event"
