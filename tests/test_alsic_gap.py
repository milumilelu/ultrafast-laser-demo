"""U10：AlSiC 缺口的**守门人**测试。

本项**未取得数据**，因此测试的职责不是「验证数据」，而是
**防止缺口被偷偷填上**：

1. 数据包里不得出现**纳秒**体制记录被当作飞秒；
2. 不得用**相近材料**（纯 AA2024 / 烧结 SiC）冒充 SiCp/AA2024 的复合响应；
3. 缺口必须**如实记录**在 manifest 里（含原因与排除项），不得含糊过去；
4. 材料卡的 `delta_m` 保持 `null`（**不得**凭空补一个深度能力）。

一旦将来真的取得了 AlSiC 飞秒数值，本文件里标 `xfail` 的那条会转为 XPASS，
提示「缺口已补 → 请更新本测试」。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MEASURED = ROOT / "data" / "measured"
MANIFEST = MEASURED / "external_assets_manifest.json"
ALSIC_CARD = ROOT / "data" / "materials" / "alsic_sicp_aa2024_1030nm.json"

pytestmark = pytest.mark.u10

ALSIC_MARKERS = ("alsic", "铝基碳化硅", "sicp/al", "sicp_")
NS_MARKERS = ("nanosecond", "纳秒", "ns_regime")
PROXY_MARKERS = ("aa2024", "aluminum alloy", "纯铝", "sintered sic")


def _all_rows() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for p in sorted(MEASURED.glob("*.csv")):
        for r in csv.DictReader(p.read_text(encoding="utf-8-sig").splitlines()):
            r["_file"] = p.name
            out.append(r)
    return out


def _a02() -> dict:
    items = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return next(i for i in items if i["id"] == "A02")


# ---------------------------------------------------------------------------
# 1. 缺口必须如实记录
# ---------------------------------------------------------------------------


def test_manifest_records_alsic_as_gap_with_reason():
    """缺口必须记录**原因**与**排除项**，不能只写一句「未取得」。"""
    a = _a02()
    assert a.get("is_gap") is True
    assert "gap" in a["status"]
    reason = a.get("gap_reason", "")
    assert reason.strip(), "必须有缺口原因"
    # 原因要说明「不是牌号问题，而是拿不到数值」
    assert "牌号" in reason and ("数值" in reason or "全文" in reason)
    excluded = a.get("explicitly_excluded") or {}
    assert "nanosecond_alsic" in excluded, "必须显式排除纳秒数据"
    assert "near_material_substitution" in excluded, "必须显式排除相近材料顶替"
    assert a.get("search_evidence"), "应留有本轮检索证据（可审计）"


def test_status_not_falsely_claimed_as_ingested():
    """**不得**谎报已接入。"""
    a = _a02()
    assert "downloaded" not in a["status"], "未取得就不得写成已下载"
    assert a.get("rows") is None, "未知行数必须保持 null，不得写猜测值"


# ---------------------------------------------------------------------------
# 2. 红线：不得混入纳秒 / 不得用相近材料冒充
# ---------------------------------------------------------------------------


def test_no_nanosecond_rows_in_femtosecond_pack():
    """数据包内**不得**出现纳秒体制记录。

    检索中出现的 39/99 μm 结果来自纳秒论文 —— 细则明列**不得**混入。
    """
    bad = []
    for r in _all_rows():
        blob = " ".join(str(v or "") for v in r.values()).lower()
        if any(m in blob for m in NS_MARKERS):
            bad.append((r["_file"], r.get("case_id")))
    assert not bad, f"发现疑似纳秒体制记录：{bad[:5]}"


def test_no_proxy_material_masquerading_as_composite():
    """**不得**用纯 AA2024 / 烧结 SiC 的数据冒充 SiCp/AA2024。

    这两者**可以**作为基体参考（若显式标注），但不得计入 AlSiC 记录。
    """
    masquerade = []
    for r in _all_rows():
        blob = " ".join(str(v or "") for v in r.values()).lower()
        is_alsic = any(m in blob for m in ALSIC_MARKERS)
        is_proxy = any(m in blob for m in PROXY_MARKERS)
        # 含代理材料标记、且**声称**是复合材料、但**没有** AlSiC 标记 → 可疑
        if is_proxy and "composite" in blob and not is_alsic:
            masquerade.append((r["_file"], r.get("case_id")))
    assert not masquerade, f"发现疑似相近材料冒充：{masquerade[:5]}"


# ---------------------------------------------------------------------------
# 3. 材料卡保持原样（不得凭空补深度能力）
# ---------------------------------------------------------------------------


def test_alsic_card_keeps_null_delta():
    """材料卡的 `response.delta_m` 必须保持 `null`。

    没有实测深度曲线就没有 δ —— **不得**由阈值反推、不得用相近材料补
    （这条与 `response.build_pulse_law` 的既有报错行为一致）。
    """
    if not ALSIC_CARD.exists():
        pytest.skip("AlSiC 材料卡不存在")
    card = json.loads(ALSIC_CARD.read_text(encoding="utf-8"))
    resp = card.get("response") or {}
    assert resp.get("delta_m") is None, (
        f"AlSiC 的 delta_m 应为 null（无实测深度曲线）；实际 {resp.get('delta_m')!r}"
    )


# ---------------------------------------------------------------------------
# 4. 将来补上时，让它显式失败以提醒更新
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="U10 未完成：AlSiC 飞秒数值尚未取得。取得后本测试会 XPASS，届时请更新它。",
    strict=False,
)
def test_alsic_gap_still_open():
    """缺口**当前**应为「未取得 AlSiC 记录」。

    这条用 `xfail` 表达「已知未完成」。若将来真的接入了 AlSiC 数据，
    它会变成 XPASS —— 那不是失败，而是**提醒**：该把 U10 标记为已完成了。
    """
    rows = [r for r in _all_rows()
            if any(m in " ".join(str(v or "") for v in r.values()).lower()
                   for m in ALSIC_MARKERS)]
    assert rows, "当前应为「无 AlSiC 记录」（缺口）——出现了记录说明缺口已补"


def test_report_states_gap_and_exclusions():
    """缺口报告必须写明「未完成」与排除项。"""
    md = ROOT / "docs" / "reports" / "alsic_gap.md"
    if not md.exists():
        pytest.skip("缺口报告尚未生成（先跑 tools/alsic_gap_report.py）")
    text = md.read_text(encoding="utf-8")
    assert "未完成" in text or "未取得" in text
    assert "纳秒" in text, "必须写明不得混入纳秒数据"
    assert "牌号" in text, "必须写明这不是牌号不匹配的问题"
