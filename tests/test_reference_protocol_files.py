"""参考协议外置（ADR-0021）：材料属性 / 源文献装置条件 / 本机设备三层不得串位。

背景：光束参数（λ/τ/f/w0）是**设备量**，不是材料属性。核函数
``a = δ·ln(F/F_th)`` 是局域能流定律，与光斑无关 —— ``solver.py`` 与 ``response.py``
里**零** w0 引用，卡里的 w0 从不进入物理计算，只作条件门禁。把它留在材料卡里会被
误读成材料参数，而且同一台设备的条件要在多张卡里各抄一遍。

本文件守住三件事：

1. **结构**：材料卡里不得再出现光束条件字段；协议必须是独立文件；
2. **可解析**：每张卡的 ``protocol_id`` 都能解析到协议文件，且装配后形状与分离前一致；
3. **如实报错**：悬挂引用 / protocol_id 不一致 / 缺 ``protocol_file`` ⇒ 明确错误码与
   字段路径，**不得**静默返回空协议或降级执行。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import DATA_MATERIALS, ROOT

from ufdemo.errors import CONFIG_INVALID, UFDemoError
from ufdemo.materials import load_material_card, resolve_reference_protocol

PROTOCOL_DIR = ROOT / "data" / "protocols"

#: 光束条件的字段名 —— 它们只允许出现在协议文件里，不允许出现在材料卡里
BEAM_FIELDS = ("required_laser", "required_history", "spot_radius_m")


def _cards() -> list[Path]:
    return sorted(DATA_MATERIALS.glob("*.json"))


def _protocol_ids() -> set[str]:
    return {p.stem for p in PROTOCOL_DIR.glob("*.json")}


# ---------------------------------------------------------------------------
# 1. 结构：材料卡里不得再有光束条件
# ---------------------------------------------------------------------------


def test_material_cards_carry_no_beam_parameters():
    """材料卡的 ``reference_protocol`` 只能是引用；光束条件不得内联。"""
    offenders = {}
    for p in _cards():
        raw = json.loads(p.read_text(encoding="utf-8"))
        rp = raw.get("reference_protocol") or {}
        inline = [k for k in ("required_laser", "required_history") if k in rp]
        if inline:
            offenders[p.name] = inline
    assert not offenders, (
        "材料卡里仍内联着光束条件（应迁到 data/protocols/）："
        + repr(offenders)
        + "。参考 docs/reports/material_card_schema_change_adr0021.md。"
    )


def test_material_card_source_text_has_no_beam_field_keys():
    """**源码级**复查：卡文件里不得出现 ``"required_laser":`` 这种 JSON 字段。

    只看解析后的结构会漏掉「键写错层级」这类情况；这里连文本一起查。
    正文里的说明文字（如 migration_note 提到 required_laser）不算 —— 必须带引号加冒号。
    """
    offenders = []
    for p in _cards():
        text = p.read_text(encoding="utf-8")
        for field in BEAM_FIELDS:
            if f'"{field}":' in text:
                offenders.append((p.name, field))
    assert not offenders, f"材料卡文本里出现光束条件字段：{offenders}"


def test_peak_fluence_moved_into_protocol():
    """``validity_domain.peak_fluence_J_m2`` 同为光束量，已随协议迁出。"""
    offenders = [p.name for p in _cards()
                 if "peak_fluence_J_m2" in (json.loads(p.read_text(encoding="utf-8"))
                                            .get("validity_domain") or {})]
    assert not offenders, f"仍有卡把峰值能流挂在 validity_domain 下：{offenders}"


# ---------------------------------------------------------------------------
# 2. 可解析：引用闭合、装配形状一致
# ---------------------------------------------------------------------------


def test_every_reference_resolves_to_a_matching_protocol_file():
    """每张卡的 protocol_id 都必须有对应协议文件，且文件内 protocol_id 一致。"""
    missing, mismatched = [], []
    for p in _cards():
        rp = json.loads(p.read_text(encoding="utf-8")).get("reference_protocol") or {}
        pid = rp.get("protocol_id")
        if not pid:
            continue
        f = PROTOCOL_DIR / f"{pid}.json"
        if not f.exists():
            missing.append((p.name, pid))
            continue
        if json.loads(f.read_text(encoding="utf-8")).get("protocol_id") != pid:
            mismatched.append((p.name, pid))
    assert not missing, f"卡片引用了不存在的协议：{missing}"
    assert not mismatched, f"协议文件与卡内 protocol_id 不一致：{mismatched}"


def test_no_orphan_protocol_files():
    """协议库里不应有无人引用的文件（死数据会让人误以为它在生效）。"""
    used = set()
    for p in _cards():
        pid = (json.loads(p.read_text(encoding="utf-8"))
               .get("reference_protocol") or {}).get("protocol_id")
        if pid:
            used.add(pid)
    orphans = sorted(_protocol_ids() - used)
    assert not orphans, f"无人引用的协议文件：{orphans}"


def test_loader_assembles_same_shape_as_before_separation():
    """装配后的 ``reference_protocol`` 必须保留分离前的键（形状只做超集扩展）。"""
    spec = load_material_card(DATA_MATERIALS / "zirconia_ysz_machining_effective_n3.json")
    rp = spec.reference_protocol
    for key in ("protocol_id", "required_laser", "required_history", "protocol_note"):
        assert key in rp, f"装配后缺少分离前就有的键：{key}"
    assert sorted(rp["required_laser"]) == [
        "pulse_duration_s", "repetition_rate_Hz", "spot_radius_m", "wavelength_m"]
    assert rp["required_laser"]["spot_radius_m"]["value"] == pytest.approx(1.6e-05)
    # 新增：来源可追溯 + 峰值能流随协议
    assert rp["protocol_file"] == "data/protocols/ysz_crown_machining_effective_n3.json"
    assert rp["reference_peak_fluence_J_m2"] == pytest.approx(501000.0)


def test_protocols_match_generator_literals():
    """协议文件必须与生成器字面量一致 —— 手改产物而不改生成器会在这里暴露。"""
    import importlib.util

    mig_path = ROOT / "tools" / "migrate_materials.py"
    spec = importlib.util.spec_from_file_location("_mig_for_test", mig_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    drift = []
    for pid, proto in mod.PROTOCOLS.items():
        f = PROTOCOL_DIR / f"{pid}.json"
        if not f.exists():
            drift.append((pid, "缺文件"))
        elif json.loads(f.read_text(encoding="utf-8")) != proto:
            drift.append((pid, "内容与生成器不一致"))
    assert not drift, f"协议库与生成器不同步：{drift}"


def test_unconfirmed_condition_stays_null():
    """未核实的条件必须保持 null —— 缺数据不得用相近值补（红线 2）。"""
    f = PROTOCOL_DIR / "diamond_scd_1030nm_candidate.json"
    proto = json.loads(f.read_text(encoding="utf-8"))
    assert proto["required_laser"]["pulse_duration_s"]["value"] is None
    assert "不启用" in (proto.get("protocol_note") or "")


# ---------------------------------------------------------------------------
# 3. 如实报错：坏引用不得静默通过
# ---------------------------------------------------------------------------


def _card_with(reference_protocol: dict) -> dict:
    base = json.loads(
        (DATA_MATERIALS / "zirconia_ysz_machining_effective_n3.json").read_text(encoding="utf-8"))
    base["reference_protocol"] = reference_protocol
    return base


def test_missing_protocol_file_is_reported(tmp_path):
    with pytest.raises(UFDemoError) as ei:
        resolve_reference_protocol(
            _card_with({"protocol_id": "nope", "protocol_file": "data/protocols/nope.json"}),
            base=tmp_path)
    assert ei.value.code == CONFIG_INVALID
    assert ei.value.field_path == "reference_protocol.protocol_file"
    assert "nope" in json.dumps(ei.value.actual, ensure_ascii=False)


def test_protocol_id_mismatch_is_reported():
    with pytest.raises(UFDemoError) as ei:
        resolve_reference_protocol(
            _card_with({
                "protocol_id": "diamond_scd_1030nm_candidate",   # 与文件名指向的协议不符
                "protocol_file": "data/protocols/ysz_crown_machining_effective_n3.json",
            }))
    assert ei.value.code == CONFIG_INVALID
    assert ei.value.field_path == "reference_protocol.protocol_id"


def test_protocol_id_without_file_is_reported():
    """只有 protocol_id、没有 protocol_file ⇒ 报错，不得当作「没有协议」放行。"""
    with pytest.raises(UFDemoError) as ei:
        resolve_reference_protocol(_card_with({"protocol_id": "ysz_crown_machining_effective_n3"}))
    assert ei.value.code == CONFIG_INVALID
    assert ei.value.field_path == "reference_protocol.protocol_file"


def test_inline_protocol_still_accepted_for_fixtures():
    """人工 fixture 仍可直接内联完整协议（向后兼容，不是"必须走文件"）。"""
    inline = {"protocol_id": "fixture", "required_laser": {"wavelength_m": {"value": 1e-6, "rel_tol": 0.1}}}
    assert resolve_reference_protocol({"reference_protocol": inline}) == inline


def test_card_without_protocol_yields_empty_dict():
    assert resolve_reference_protocol({"reference_protocol": {}}) == {}
