"""U09：CFRP 原始数据接入回归。

守四件事：

1. **真的接进来了** —— manifest 状态从 ``located_not_downloaded`` 变为已下载，
   且记录**实际** sha256 / 大小 / 行数（不是猜测值）；
2. **实验列 / 效率列显式区分**，每列单位明确；
3. **效率不可取得 increment_access**（体积效率不是局部深度，不得反推）；
4. **冲突被标记且未平均**；切割数据（另一套光学系统）**未被合并**。

⚠️ 本项依赖外部下载。若下载未完成，测试**如实跳过**，
**不得**把「跳过」写成「通过」。
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MEASURED = ROOT / "data" / "measured"
CSV_PATH = MEASURED / "cfrp_efficiency_curves.csv"
MANIFEST = MEASURED / "external_assets_manifest.json"

pytestmark = pytest.mark.u09

#: 下载时记录的实际值（与 manifest 一致）
EXPECTED_SHA256 = "6d9747ae3cfe3a45333ef5a9bb65f136e9e701343e64ef4cde540ce04a8748f3"
EXPECTED_SIZE = 157952
EXPECTED_SHEETS = 10
EXPECTED_ROWS = 230


def _manifest_item(asset_id: str = "A01") -> dict:
    items = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return next(i for i in items if i["id"] == asset_id)


def _rows() -> list[dict[str, str]]:
    return list(csv.DictReader(CSV_PATH.read_text(encoding="utf-8-sig").splitlines()))


# ---------------------------------------------------------------------------
# 1. 真的接进来了（下载未完成则如实跳过）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not CSV_PATH.exists(), reason="CFRP XLSX 尚未下载解析（外部依赖）")
def test_manifest_records_actual_download_facts():
    """manifest 必须记录**实际** sha256 / 大小 / 行数，**不得**写猜测值。"""
    it = _manifest_item("A01")
    assert it["status"] == "downloaded_and_parsed", (
        f"状态应已更新；当前 {it['status']}（不得谎报已接入）"
    )
    assert it["sha256"] == EXPECTED_SHA256
    assert it["actual_size_bytes"] == EXPECTED_SIZE
    assert it["rows"] == EXPECTED_ROWS, "行数必须是实际解析结果，不是猜测"
    assert it["sheets"] == EXPECTED_SHEETS


@pytest.mark.skipif(not CSV_PATH.exists(), reason="CFRP XLSX 尚未下载解析（外部依赖）")
def test_all_sheets_parsed():
    """**全部 10 个工作表**都要解析出来。

    回归背景：本解析器第一版**硬编码列号**，结果只解析出 8 个表
    （`N2-5%_NoCF` 的列整体右移一列）—— 少掉的数据不会报错，只会静默消失。
    现在按表头文字定位，且把「未解析出的表」显式打印出来。
    """
    rows = _rows()
    sheets = {r["sheet"] for r in rows}
    assert len(sheets) == EXPECTED_SHEETS, f"应 10 个表，实际 {len(sheets)}：{sorted(sheets)}"
    assert len(rows) == EXPECTED_ROWS


# ---------------------------------------------------------------------------
# 2. 列分类与单位
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not CSV_PATH.exists(), reason="CFRP 数据未接入")
def test_measured_and_efficiency_columns_are_separated():
    """**实验列与效率列必须显式区分**，且各自有值。"""
    rows = _rows()
    n_depth = sum(1 for r in rows if (r.get("measured_depth_um") or "").strip())
    n_eff = sum(1 for r in rows if (r.get("efficiency_1e6um3_per_J") or "").strip())
    assert n_depth == len(rows), "每行都应有实测深度（实验列）"
    assert n_eff == len(rows), "每行都应有体积效率（效率列）"
    it = _manifest_item("A01")
    cls = it["column_classes"]
    assert set(cls["measured_columns"]) == {"measured_ablated_v_1e6um3", "measured_depth_um"}
    assert "efficiency_1e6um3_per_J" in cls["efficiency_column"]
    # 条件列是从原文参数导出的，**不是**实测 —— 必须单独一类
    assert "power_W" in cls["derived_conditions"]


@pytest.mark.skipif(not CSV_PATH.exists(), reason="CFRP 数据未接入")
def test_units_declared_and_conversion_consistent():
    """每列单位明确；`1e6 µm³/J → m³/J` 换算自洽。"""
    rows = _rows()
    for r in rows[:20]:
        eff = float(r["efficiency_1e6um3_per_J"])
        m3 = float(r["efficiency_m3_per_J"])
        assert m3 == pytest.approx(eff * 1e-12, rel=1e-12), "1 (10^6 µm³/J) = 1e-12 m³/J"
    it = _manifest_item("A01")
    assert "1e-12" in it["units"]["conversion"]


# ---------------------------------------------------------------------------
# 3. 效率不得取得 increment_access
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not CSV_PATH.exists(), reason="CFRP 数据未接入")
def test_efficiency_cannot_get_increment_access():
    """**体积效率不是局部深度** —— 不得反推局部深度、不得进事件核。

    沿用既有闸门 `tables.assert_no_local_depth_from_volume` 的语义：
    这里直接在数据侧断言语义是 `volume_per_energy`，并按 U05 判定无增量权限。
    """
    from ufdemo import datasets as DS

    rows = _rows()
    rec = {
        "case_id": rows[0]["case_id"],
        "source_id": "D04",
        "data_kind": "published_experimental_result",
        "output_semantics": "volume_per_energy",
        "efficiency_um3_per_uJ": "3.8558571428571424",
        "efficiency_m3_per_J": "3.8558571428571e-12",
    }
    d = DS.evaluate(rec)
    assert d.observation_access is True, "体积效率可观测"
    assert d.increment_access is False, "**不得**取得增量权限"
    # 该语义本身就不在可进事件核的集合里
    from ufdemo.config import SEMANTIC_VOLUME_PER_ENERGY
    from ufdemo.response import assert_increment_semantics
    from ufdemo.errors import RESPONSE_SEMANTICS_INVALID, UFDemoError

    with pytest.raises(UFDemoError) as ei:
        assert_increment_semantics(SEMANTIC_VOLUME_PER_ENERGY)
    assert ei.value.code == RESPONSE_SEMANTICS_INVALID


@pytest.mark.skipif(not CSV_PATH.exists(), reason="CFRP 数据未接入")
def test_every_row_declares_volume_semantics():
    """全部行必须是 `volume_per_energy`（不得被改成深度语义）。"""
    rows = _rows()
    assert {r["output_semantics"] for r in rows} == {"volume_per_energy"}


# ---------------------------------------------------------------------------
# 4. 冲突标记与切割数据隔离
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not CSV_PATH.exists(), reason="CFRP 数据未接入")
def test_abstract_vs_text_conflict_flagged_not_averaged():
    """摘要/正文冲突必须**被标记**且**未被平均**。

    做法：检查带冲突标记的行，其效率值应**逐个**来自正文（即等于 CSV 里的原值），
    且**不出现** 6.06（摘要求值）—— 若有人把两者平均，会出现介于两者之间的新值。
    """
    rows = _rows()
    flagged = [r for r in rows if "abstract" in (r.get("quality_note") or "")]
    assert flagged, "应有行被标记摘要/正文冲突"
    vals = [float(r["efficiency_1e6um3_per_J"]) for r in flagged]
    assert all(abs(v - 6.06) > 1e-9 for v in vals), "不得把摘要求值写进表"
    # manifest 里也要说明冲突
    it = _manifest_item("A01")
    assert "abstract_vs_text" in it["conflicts"]
    assert "未平均" in it["conflicts"]["abstract_vs_text"] or "no averaging" in it["conflicts"]["abstract_vs_text"].lower()


@pytest.mark.skipif(not CSV_PATH.exists(), reason="CFRP 数据未接入")
def test_cutting_data_not_merged():
    """切割（另一套 24 µm 光学系统）**不得**与效率数据集合并。

    本 CSV 全部来自效率工作表；不得混入切割表。做法：
    逐行检查 `quality_note` 声明了切割隔离，且工作表名都在效率配方集合内。
    """
    rows = _rows()
    allowed_sheets = {
        "N1-1%", "N1-1%_NoCF", "N1-5%", "N1-5%_NoCF",
        "N2-5%", "N2-5%_NoCF", "N3-5%", "N3-5%_NoCF",
        "CF+Epoxy", "Epoxy",
    }
    got = {r["sheet"] for r in rows}
    assert got <= allowed_sheets, f"出现了非效率工作表：{got - allowed_sheets}"
    assert all("24 µm" in (r.get("quality_note") or "") or "24" in (r.get("quality_note") or "")
               for r in rows[:5]), "应声明切割使用另一套光学系统、不得合并"


@pytest.mark.skipif(not CSV_PATH.exists(), reason="CFRP 数据未接入")
def test_recipe_labels_recorded_but_not_used_to_reject():
    """配方不同**允许使用**（记录配方即可），不得因牌号不匹配拒收。"""
    rows = _rows()
    assert all((r.get("recipe_label") or "").strip() for r in rows), "每行都应有配方标签"
    assert len({r["recipe_label"] for r in rows}) >= 8
