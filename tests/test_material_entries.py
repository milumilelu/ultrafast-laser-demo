"""七材料能力入口与拦截核验（批次 H / T14 交付边界）。

本文件只做一件事：**逐条实跑**入口表的「必须拦截」探针与「开放内容」依据，
证明拦截是**在执行层真实生效**的，而不是只在文档里声称。

口径（与细则第 7 节、任务书 13 节一致）：

* ``blocked``：红线。探针必须让指定错误码真的抛出来。
* ``opened``：至少一张入口卡确实具备该能力／允许该运行模式。
* ``deferred``：细则要求开放、但当前实现未支持的缺口。探针必须证明
  "现在确实打不开"，以防日后被静默改写成"已开放"。
"""

from __future__ import annotations

import pytest

from conftest import DATA_MATERIALS
from ufdemo.materials import (
    MATERIAL_ENTRIES,
    load_material_catalog,
    material_entry_rows,
    verify_entry_enforcements,
)

pytestmark = pytest.mark.g09

EXPECTED_FAMILIES = ("氧化锆", "铝基碳化硅", "CFRP", "高温合金", "微晶玻璃", "SiC", "金刚石")


@pytest.fixture(scope="module")
def catalog():
    return load_material_catalog(DATA_MATERIALS)


@pytest.fixture(scope="module")
def verdicts(catalog):
    return verify_entry_enforcements(catalog, DATA_MATERIALS)


def test_entries_cover_seven_families_once():
    families = [e.family for e in MATERIAL_ENTRIES]
    assert families == list(EXPECTED_FAMILIES)


def test_every_entry_card_exists_in_catalog(catalog):
    for entry in MATERIAL_ENTRIES:
        for mid in entry.entry_ids:
            assert mid in catalog, f"{entry.family} 的入口卡 {mid!r} 不在材料目录中"


def test_every_blocked_and_deferred_item_binds_a_probe():
    """不许出现"声称拦截/缺口但没有可执行证据"的条目。"""
    for entry in MATERIAL_ENTRIES:
        for b in entry.blocked:
            assert b.probe_key, f"{entry.family}/{b.item} 未绑定探针"
            assert b.enforcement, f"{entry.family}/{b.item} 未绑定生效位置"
        for d in entry.deferred:
            assert d.probe_key, f"{entry.family}/{d.item} 未绑定探针"
            assert d.reason, f"{entry.family}/{d.item} 未给出缺口原因"


def test_all_entry_probes_hold(catalog, verdicts):
    """核心断言：所有探针必须全部成立（拦截生效 / 开放成立 / 缺口确实打不开）。"""
    failed = [r for r in verdicts if not r["ok"]]
    assert not failed, "\n".join(f"{r['family']}/{r['kind']}/{r['item']}: {r['detail']}" for r in failed)


def test_blocked_probes_are_not_vacuous(catalog, verdicts):
    """每条红线都要有对应的探针结论；拦截条数不得为 0。"""
    blocked = [r for r in verdicts if r["kind"] == "blocked"]
    assert len(blocked) >= 7, "七族至少各有一条红线被实跑验证"
    assert all(r["detail"] for r in blocked)


def test_opened_items_are_bound_to_real_capabilities(catalog, verdicts):
    """开放项必须绑定到真实可用能力／运行模式，且理由串可核对。"""
    opened = [r for r in verdicts if r["kind"] == "opened"]
    assert opened
    for r in opened:
        assert r["ok"], f"{r['family']}/{r['item']} 开放依据未成立"
        assert r["enforcement"].startswith(("capability:", "run_mode:")), r["enforcement"]


def test_diamond_synthetic_is_deferred_not_claimed_open(catalog, verdicts):
    """金刚石的"合成形貌"必须记为缺口，而不得被声称为已开放。"""
    diamond = next(e for e in MATERIAL_ENTRIES if e.family == "金刚石")
    opened_items = {o.item for o in diamond.opened}
    assert "合成形貌" not in opened_items, "金刚石合成形貌不得出现在开放项中"
    assert any(d.item == "合成形貌" for d in diamond.deferred)
    for mid in diamond.entry_ids:
        assert not catalog[mid].capability("synthetic_structure").available


def test_deferred_gap_is_proven_by_probe(verdicts):
    row = next(r for r in verdicts if r["kind"] == "deferred")
    assert row["family"] == "金刚石" and row["item"] == "合成形貌"
    assert row["ok"] is True, "缺口探针必须证明「现在确实打不开」"


def test_sic_has_no_synthetic_claim(catalog):
    """SiC 不在细则第 7 节的「合成形貌」之列，能力表也不得给出该能力。"""
    sic = catalog["sic_4h_cface_1035nm_multishot"]
    assert not sic.capability("synthetic_structure").available


def test_material_entry_rows_are_machine_readable(catalog):
    rows = material_entry_rows(catalog, DATA_MATERIALS)
    assert rows
    required = {"family", "entry_ids", "kind", "item", "binding", "code", "verified", "detail"}
    for r in rows:
        assert required <= set(r.keys())
        assert r["kind"] in ("opened", "blocked", "deferred")
        assert r["verified"] is not None, f"{r['family']}/{r['item']} 未给出核验结论"


def test_material_entry_rows_cover_all_entries(catalog):
    rows = material_entry_rows(catalog, DATA_MATERIALS)
    families = {r["family"] for r in rows}
    assert families == set(EXPECTED_FAMILIES)


def test_no_dup_items_within_entry():
    for entry in MATERIAL_ENTRIES:
        items = [o.item for o in entry.opened] + [b.item for b in entry.blocked] + [
            d.item for d in entry.deferred
        ]
        assert len(items) == len(set(items)), f"{entry.family} 条目重复：{items}"
