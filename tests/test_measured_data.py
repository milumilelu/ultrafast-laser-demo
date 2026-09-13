"""U04：实测数据可用性回归。

把 `tools/measured_data_report.py` 的检查**纳入 pytest**，
否则那套检查只算「跑过一次」，后续改动无人保护。

这里只做**数据可用性**验收（行数/哈希/单位/字段/语义红线），
**不做实验复现** —— 软件跑通 ≠ 材料验证。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MEASURED = ROOT / "data" / "measured"


def _load_report_module():
    """从路径加载 ``tools/measured_data_report.py``（``tools/`` 不是包）。"""
    path = ROOT / "tools" / "measured_data_report.py"
    spec = importlib.util.spec_from_file_location("measured_data_report", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def qa_rows():
    if not (MEASURED / "manifest.json").exists():
        pytest.skip("data/measured/ 未导入")
    return _load_report_module().check()


def test_measured_pack_all_checks_pass(qa_rows):
    """全部检查项通过；失败时把失败项原文列出来，便于定位。"""
    failed = [r for r in qa_rows if r["status"] != "通过"]
    assert not failed, "实测数据 QA 未通过：\n" + "\n".join(
        f"  [{r['group']}] {r['check']}：期望 {r['expected']}，实测 {r['measured']}"
        for r in failed
    )


def test_measured_pack_counts_match_claim(qa_rows):
    """计数口径：65 条激光观测 + 4 条非激光对照。

    65 是**已发表条件/结果记录数**，不是 65 个独立数据集、不是 65 张材料卡。
    这条断言把口径钉死，防止后续被悄悄改写。
    """
    by_check = {r["check"]: r for r in qa_rows}
    main = by_check.get("激光观测行数 = manifest.main_record_count")
    ctrl = by_check.get("非激光对照行数 = manifest.nonlaser_control_count")
    assert main and main["measured"] == "65", f"激光观测数应 65，实测 {main and main['measured']}"
    assert ctrl and ctrl["measured"] == "4", f"对照数应 4，实测 {ctrl and ctrl['measured']}"
    # 观测包必须是 69（U09 的效率曲线包**不计入** —— 两者「一行」含义不同）
    obs = by_check.get("观测包条目数 = 65+4（效率曲线包不计入）")
    assert obs and obs["measured"] == "69", (
        f"观测包应 69，实测 {obs and obs['measured']}（分类：{obs and obs['note']}）"
    )


def test_measured_pack_dd6_row14_anomaly_is_visible(qa_rows):
    """DD6 第 14 行的锥度不一致必须**显式保留**，不得静默修正原文。

    原文印 -1.809°，按进/出口直径与 2 mm 板厚复算是 -0.3774°。
    两者并存 + 标记 + 说明排除该原文值参与拟合，缺一不可。
    """
    keys = {r["check"] for r in qa_rows if r["status"] == "通过"}
    assert "D05-14 原文锥度与复算值并存且不相等" in keys
    assert "D05-14 不一致被显式标记" in keys
    assert "D05-14 quality_note 说明排除该原文锥度参与拟合" in keys


def test_measured_pack_has_no_model_generated_rows(qa_rows):
    """导入数据中不得存在公式采样/模型预测生成的「实测」行。"""
    keys = {r["check"]: r for r in qa_rows}
    assert keys["manifest.measurements_generated_by_model"]["measured"] == "False"
    assert keys["data_kind 中无 model/synthetic/formula 生成行"]["status"] == "通过"


def test_measured_pack_respects_semantic_red_line(qa_rows):
    """语义红线：导入数据不得出现 ``event_depth_increment``。

    累计深度 / 体积效率这类语义**不得改名混入逐事件主循环**。
    """
    row = next(r for r in qa_rows if r["check"].startswith("导入数据不含 event_depth_increment"))
    assert row["status"] == "通过", row["measured"]
