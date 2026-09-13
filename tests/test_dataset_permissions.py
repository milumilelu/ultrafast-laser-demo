"""U05：数据集注册与**权限门槛**回归。

本文件是 U05 的守门人。它守两件**方向相反**的事，缺一不可：

1. **放宽** —— 身份/工况完整度（牌号未知、脉宽未确认）**不得**把观测入口挡掉；
   否则真实数据永远进不来，正是 F06 的病根。
2. **守红线** —— 单位/语义/观测类型**不得**因为放宽而松掉：
   - 累计量 / 平均率 / 体积效率 → **永不**取得 increment_access；
   - 请求增量权限必须抛**既定错误码** `RESPONSE_SEMANTICS_INVALID`；
   - 单位冲突、语义缺失 → **硬错误**，不降级为警告。

所以「全部通过」本身不是目标 —— 本文件同时断言**该拦的确实拦住了**。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from ufdemo import datasets as DS
from ufdemo.config import ALL_SEMANTICS, SEMANTIC_EVENT_INCREMENT
from ufdemo.errors import CONFIG_INVALID, RESPONSE_SEMANTICS_INVALID, UFDemoError

ROOT = Path(__file__).resolve().parents[1]
MEASURED = ROOT / "data" / "measured"
MATERIALS = ROOT / "data" / "materials"

pytestmark = pytest.mark.u05


def _base_record(**over) -> dict:
    """一条最小可过的观测记录（端点语义、实测来源、无单位冲突）。"""
    rec = {
        "case_id": "T-1",
        "source_id": "D01",
        "data_kind": "published_experimental_result",
        "output_semantics": DS.SEMANTIC_SINGLE_PULSE_CRATER,
    }
    rec.update(over)
    return rec


# ---------------------------------------------------------------------------
# 放宽侧：身份/完整度不得阻塞观测
# ---------------------------------------------------------------------------


def test_unknown_grade_still_has_observation_access():
    """**牌号未知不阻塞观测** —— 这是 U05 的核心放宽。"""
    d = DS.evaluate(_base_record(grade=None))
    assert d.observation_access is True
    assert d.increment_access is False
    assert any("牌号" in r for r in d.risk_notes), "未知牌号必须进 risk_notes，而不是被隐藏"


def test_blank_grade_string_also_allowed():
    """空串与 null 等价处理（CSV 里空值读出来是空串，不是 None）。"""
    d = DS.evaluate(_base_record(grade=""))
    assert d.observation_access is True
    assert any("牌号" in r for r in d.risk_notes)


def test_unconfirmed_pulse_duration_is_soft_gate_only():
    """脉宽未确认 → 只降权限 + 提示，**不阻塞**观测。

    金刚石那批原文只给「设备最小脉宽 250 fs」，那不是实际脉宽；
    必须提示「不得据此反推峰值强度」，但数据仍可浏览。
    """
    d = DS.evaluate(_base_record(pulse_duration_fs=None,
                                 equipment_min_pulse_duration_fs="250"))
    assert d.observation_access is True
    assert "pulse_duration_fs" in d.missing_fields
    assert any("设备最小脉宽" in r and "不得" in r for r in d.risk_notes)


def test_missing_repetition_rate_is_soft_gate_only():
    d = DS.evaluate(_base_record(repetition_rate_Hz=None))
    assert d.observation_access is True
    assert any("重复频率" in r for r in d.risk_notes)


def test_digitized_extraction_is_flagged_not_blocked():
    """图为数字化读数 → 提示容差性质，但不阻塞（它仍是实测，只是精度更低）。"""
    d = DS.evaluate(_base_record(extraction_method="digitized_from_figure"))
    assert d.observation_access is True
    assert any("数字化" in r for r in d.risk_notes)


# ---------------------------------------------------------------------------
# 守红线侧：语义 / 单位 / 来源，必须硬拦
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sem", ["cumulative_depth", "mean_depth_per_effective_pulse",
                                 "volume_per_energy", "track_or_pass_depth",
                                 "threshold_only", "surface_roughness_endpoint",
                                 "through_hole_geometry_endpoint"])
def test_endpoint_and_aggregate_semantics_never_get_increment_access(sem):
    """所有非增量语义 → increment_access 恒为 False。"""
    d = DS.evaluate(_base_record(output_semantics=sem))
    assert d.observation_access is True
    assert d.increment_access is False


@pytest.mark.parametrize("sem", ["cumulative_depth", "mean_depth_per_effective_pulse",
                                 "volume_per_energy"])
def test_requesting_increment_access_raises_established_error_code(sem):
    """**把累计量当增量** → 必须抛**既定错误码** ``RESPONSE_SEMANTICS_INVALID``。

    不得新造码；不得降级为警告后放行。
    """
    with pytest.raises(UFDemoError) as ei:
        DS.assert_increment_access(_base_record(output_semantics=sem))
    assert ei.value.code == RESPONSE_SEMANTICS_INVALID


def test_dataset_may_not_claim_event_increment_semantics():
    """数据集**不得**声明 ``event_depth_increment``。

    那是曲线/响应核的语义；实测数据包若这么声明，等于把端点结果伪装成逐事件增量。
    """
    with pytest.raises(UFDemoError) as ei:
        DS.evaluate(_base_record(output_semantics=SEMANTIC_EVENT_INCREMENT))
    assert ei.value.code == CONFIG_INVALID


def test_missing_semantics_is_hard_error():
    """语义缺失 → **硬错误**，不降级。"""
    for bad in (None, "", "   "):
        with pytest.raises(UFDemoError) as ei:
            DS.evaluate(_base_record(output_semantics=bad))
        assert ei.value.code == CONFIG_INVALID


def test_unregistered_semantics_is_hard_error():
    """未登记的语义 → 硬错误（需显式登记，不静默放行）。"""
    with pytest.raises(UFDemoError) as ei:
        DS.evaluate(_base_record(output_semantics="totally_made_up_quantity"))
    assert ei.value.code == CONFIG_INVALID


def test_unit_conflict_is_hard_error():
    """成对单位不自洽 → **硬错误**，不降级为警告。"""
    with pytest.raises(UFDemoError) as ei:
        DS.evaluate(_base_record(depth_um=1.0, depth_m=1.0))  # 应 1e-6，差 6 个数量级
    assert ei.value.code == CONFIG_INVALID


def test_consistent_unit_pair_passes():
    """自洽的成对单位必须放行（不能把正常的也拦掉）。"""
    d = DS.evaluate(_base_record(depth_um=6.3, depth_m=6.3e-6))
    assert d.observation_access is True


def test_model_generated_rows_are_hard_error():
    """模拟量冒充实验量 → 硬错误。"""
    for kind in ("model_prediction", "synthetic_estimate", "formula_generated"):
        with pytest.raises(UFDemoError) as ei:
            DS.evaluate(_base_record(data_kind=kind))
        assert ei.value.code == CONFIG_INVALID


# ---------------------------------------------------------------------------
# 两个枚举是**两个轴**，不得合并
# ---------------------------------------------------------------------------


def test_observation_semantics_do_not_pollute_curve_enum():
    """``*_endpoint`` 观测语义**不得**出现在 ``config.ALL_SEMANTICS`` 里。

    为什么这条重要：``ALL_SEMANTICS`` 是**曲线/响应**的枚举，细则 4.3 明确
    「枚举不扩容」，且 ``tables.CURVE_ROUTES`` 必须对它逐项登记去向。
    若把观测语义混进去，要么测试失败，要么被迫给端点语义登记曲线去向——
    那等于默认它们可以进查表/事件核，是**红线倒退**。
    """
    for s in (DS.SEMANTIC_SINGLE_PULSE_CRATER, DS.SEMANTIC_SURFACE_ROUGHNESS,
              DS.SEMANTIC_THROUGH_HOLE_GEOMETRY):
        assert s not in ALL_SEMANTICS, f"{s} 不该出现在曲线语义枚举里"
    # 反向：曲线枚举里的每个非增量语义仍必须是可观测的
    for s in ALL_SEMANTICS:
        if s != SEMANTIC_EVENT_INCREMENT:
            assert s in DS.OBSERVATION_SEMANTICS


# ---------------------------------------------------------------------------
# 真实数据：65+4 全部可观测、零增量
# ---------------------------------------------------------------------------


def _measured_rows() -> list[dict]:
    import csv

    out: list[dict] = []
    for p in sorted(MEASURED.glob("*.csv")):
        out += list(csv.DictReader(p.read_text(encoding="utf-8-sig").splitlines()))
    return out


@pytest.mark.skipif(not (MEASURED / "manifest.json").exists(), reason="实测数据未导入")
def test_all_measured_records_are_observable_and_none_incremental():
    """**65+4 条实测记录全部可观测，且没有一条取得增量权限。**

    这条把 U05 的产品结论钉死：真实数据进得来（能观测），但**进不了主循环**
    （实测的是端点结果，不是逐事件增量）。要改变形貌必须走 U07 的
    ``TablePulseLaw`` / 标定核，而不是把端点数据改名混入。
    """
    rows = _measured_rows()
    assert len(rows) == 69, f"应为 65+4=69 条，实测 {len(rows)}"
    decisions = DS.evaluate_all(rows)  # 任一硬门槛未过会抛
    assert all(d.observation_access for d in decisions)
    assert sum(1 for d in decisions if d.increment_access) == 0
    s = DS.summarize(decisions)
    assert s["records"] == 69 and s["increment_access_true"] == 0


@pytest.mark.skipif(not (MEASURED / "registry.json").exists(), reason="注册表未生成")
def test_registry_matches_current_data():
    """注册表必须与当前数据一致（防止各自漂移）。"""
    stored = DS.load_registry(MEASURED)
    fresh = DS.build_registry(MEASURED)
    assert stored["datasets"] == fresh["datasets"]
    assert stored["files_sha256"] == fresh["files_sha256"]
    assert stored["summary"] == fresh["summary"]


# ---------------------------------------------------------------------------
# 既有红线必须原样保留（U05 风险最高处：放宽不得连带松掉 enforcement）
# ---------------------------------------------------------------------------


def test_material_cards_unchanged():
    """``data/materials/*.json`` 的 sha256 **未变** —— 旧卡不被覆盖。

    基线在 U05 开工前记录（11 张卡）。注册表是**新增维度**，
    不得顺手改写既有材料卡。
    """
    baseline_path = Path("C:/tmp/materials_sha_baseline.json")
    if not baseline_path.exists():
        pytest.skip("无基线快照（跨机运行）")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    now = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
           for p in sorted(MATERIALS.glob("*.json"))}
    assert set(now) == set(baseline), "材料卡集合发生变化"
    changed = [k for k in now if now[k] != baseline[k]]
    assert not changed, f"材料卡被改写：{changed}"


def test_curve_enum_unchanged():
    """曲线语义枚举**仍是 6 项**（细则 4.3「枚举不扩容」）。"""
    assert len(ALL_SEMANTICS) == 6, f"曲线语义枚举被改动：{ALL_SEMANTICS}"


def test_capability_modules_still_enforce():
    """既有能力入口的 enforcement **未被削弱**。

    抽查两处既有闸门：不仅要「正常输入通过」，更要**断言该拒的确实拒了** ——
    只测通过分支等于没测红线。
    """
    from ufdemo import response as R
    from ufdemo import thresholds as TH
    from ufdemo.config import SEMANTIC_THRESHOLD_ONLY

    # ① 响应语义闸门：非增量语义仍被拒（既定错误码）
    with pytest.raises(UFDemoError) as ei:
        R.assert_increment_semantics("cumulative_depth")
    assert ei.value.code == RESPONSE_SEMANTICS_INVALID

    # ② 阈值协议闸门：正常协议通过
    good = TH.ThresholdProtocol(
        output_semantics=SEMANTIC_THRESHOLD_ONLY,
        available=True,
        reason=None,
        observable_name="threshold_exceedance",
        fluence_basis="per_event_incident",
    )
    TH.assert_not_removal(good)  # 不抛

    # ③ 该拒的要拒：`used_for_depth=True` 必须被拒
    bad_depth = TH.ThresholdProtocol(
        output_semantics=SEMANTIC_THRESHOLD_ONLY,
        available=True,
        reason=None,
        observable_name="threshold_exceedance",
        fluence_basis="per_event_incident",
        used_for_depth=True,
    )
    with pytest.raises(UFDemoError) as ei2:
        TH.assert_not_removal(bad_depth)
    assert ei2.value.code == RESPONSE_SEMANTICS_INVALID

    # ④ 该拒的要拒：阈值协议声明成逐事件增量必须被拒
    bad_sem = TH.ThresholdProtocol(
        output_semantics=SEMANTIC_EVENT_INCREMENT,
        available=True,
        reason=None,
        observable_name="threshold_exceedance",
        fluence_basis="per_event_incident",
    )
    with pytest.raises(UFDemoError) as ei3:
        TH.assert_not_removal(bad_sem)
    assert ei3.value.code == RESPONSE_SEMANTICS_INVALID
