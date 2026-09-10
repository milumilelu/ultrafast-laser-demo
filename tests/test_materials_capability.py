"""材料目录、能力推导与卡准入回归（执行细则第 7 节、4.4 节）。

覆盖：

* 七种材料在目录中均有入口，能力由完整条件推导而非名称或参数个数；
* 缺 δ / 缺响应 → 深度能力禁用，阈值展示仍可用；
* 金刚石 400/700 fs 分卡，未确认脉宽的候选默认禁用；
* SiC 平均率语义为评估器专用，不得进入事件核；
* 原始 F01/F02 未被覆盖（哈希与任务书指纹一致）。
"""

from __future__ import annotations

import hashlib

import pytest

from conftest import DATA_MATERIALS, ROOT
from ufdemo.config import RunConfig, validate_run
from ufdemo.materials import (
    CAP_EVENT_INCREMENT,
    CAP_MEAN_RATE,
    CAP_REFERENCE_EVALUATOR,
    CAP_SYNTHETIC_STRUCTURE,
    CAP_THRESHOLD,
    MaterialSpec,
    capability_table,
    load_material_catalog,
)

EXPECTED_FINGERPRINTS = {
    "seven_materials_parameter_register.xlsx":
        "f0eb36ba926f48e3c2f180d4b8bf1142fcd63d378726746d1ba243d8a8726f9f",
    "material_card_templates.json":
        "9cb12bf43e15e918dc4decf34f9c927500400e675442469282f71dc6587c171a",
}


@pytest.fixture(scope="module")
def catalog():
    return load_material_catalog(DATA_MATERIALS)


def test_original_inputs_are_untouched():
    """迁移后原文件哈希不变（细则 4.4 第 6 项）。"""
    for name, expected in EXPECTED_FINGERPRINTS.items():
        p = ROOT.parent / name
        assert p.exists(), f"缺少原始输入 {name}"
        assert hashlib.sha256(p.read_bytes()).hexdigest() == expected


def test_catalog_covers_seven_families(catalog):
    families = {s.identity.get("family") for s in catalog.values()}
    for f in ("氧化锆", "铝基碳化硅", "CFRP", "高温合金", "微晶玻璃", "SiC", "金刚石"):
        assert f in families, f"缺少材料族 {f}"


def test_capabilities_derived_from_completeness(catalog):
    # YSZ 加工分支：字段完整 → 允许逐事件增量（有条件）
    ysz = catalog["zirconia_ysz_machining_effective_n3"]
    assert ysz.capability(CAP_EVENT_INCREMENT).available is True
    assert ysz.capability(CAP_EVENT_INCREMENT).conditional is True

    # CFRP 缺 δ → 深度禁用，阈值展示可用
    cfrp = catalog["cfrp_t700_yb01_800nm"]
    assert cfrp.capability(CAP_EVENT_INCREMENT).available is False
    assert "delta_m" in cfrp.capability(CAP_EVENT_INCREMENT).missing
    assert cfrp.capability(CAP_THRESHOLD).available is True

    # Inconel 缺 δ → 同上
    inc = catalog["inconel718_1030nm_n10"]
    assert inc.capability(CAP_EVENT_INCREMENT).available is False
    assert inc.capability(CAP_THRESHOLD).available is True

    # 铝基 SiC / 微晶玻璃：完整响应缺失 → 只开放合成结构
    for mid in ("alsic_sicp_aa2024_1030nm", "glass_ceramic_unbranded_1030nm"):
        assert catalog[mid].capability(CAP_EVENT_INCREMENT).available is False
        assert catalog[mid].capability(CAP_SYNTHETIC_STRUCTURE).available is True


def test_sic_average_rate_is_evaluator_only(catalog):
    sic = catalog["sic_4h_cface_1035nm_multishot"]
    assert sic.capability(CAP_MEAN_RATE).available is True
    assert sic.capability(CAP_REFERENCE_EVALUATOR).available is True
    # 平均率不是逐事件增量
    assert sic.capability(CAP_EVENT_INCREMENT).available is False
    assert "engineering_extension" in sic.capability(CAP_EVENT_INCREMENT).reason
    # 两个阈值观测量分别编号
    bids = {c["branch_id"] for c in sic.threshold_candidates}
    assert bids == {"sic4h_n1_modification", "sic4h_n1_structural_change"}


def test_diamond_pulse_durations_are_separated(catalog):
    a = catalog["diamond_scd_cvd_1030nm_400fs"]
    b = catalog["diamond_scd_cvd_1030nm_700fs"]
    assert a.response["threshold_internal"] == pytest.approx(8.2e4, rel=1e-12)
    assert b.response["threshold_internal"] == pytest.approx(12.9e4, rel=1e-12)
    c = catalog["diamond_scd_cvd_1030nm_pulsewidth_unconfirmed"]
    assert c.enabled_by_default is False
    assert "脉宽未核实" in c.blocked_reason


def test_unknown_grade_does_not_auto_match(catalog):
    """未知牌号不能凭名称自动匹配“最近”的卡。"""
    from ufdemo.errors import MATERIAL_CAPABILITY_MISSING, UFDemoError
    from ufdemo.materials import resolve_material

    cfg = RunConfig.from_dict({
        "schema_version": "1.0", "run_mode": "threshold_only", "unit_system": "SI",
        "material_id": "zirconia_some_unknown_grade",
        "grid": {"nx": 11, "ny": 11, "dx_m": 1e-6, "dy_m": 1e-6},
        "laser": {"pulse_energy_J": 1e-5, "repetition_rate_Hz": 1000.0, "spot_radius_m": 1e-5,
                  "focus_xyz_m": [0, 0, 0], "direction_unit": [0, 0, 1]},
        "path": {"segments": [{"segment_id": 0, "pass_id": 0, "start_s": 0.0, "end_s": 1e-3,
                               "start_xyz_m": [0, 0, 0], "end_xyz_m": [0, 0, 0], "laser_on": True}]},
    })
    with pytest.raises(UFDemoError) as ei:
        resolve_material(cfg, DATA_MATERIALS)
    assert ei.value.code == MATERIAL_CAPABILITY_MISSING


def test_capability_table_is_machine_readable(catalog):
    rows = capability_table(catalog)
    assert rows
    required = {"material_id", "family", "capability", "available", "reason", "missing"}
    assert required <= set(rows[0].keys())


def test_material_card_evidence_enum_is_execution_layer(catalog):
    for spec in catalog.values():
        assert spec.evidence_status in (
            "unverified", "literature_reported", "formula_checked",
            "experiment_reproduced", "independently_validated",
        )


def test_null_delta_is_preserved_not_zeroed():
    """缺失 δ 必须保持 null，不得被 0 或相近材料值替代。"""
    import json

    raw = json.loads((DATA_MATERIALS / "cfrp_t700_yb01_800nm.json").read_text(encoding="utf-8"))
    assert raw["response"]["delta_m"] is None
    assert raw["phases"][0]["delta_m"] is None
    spec = MaterialSpec.from_dict(raw)
    assert spec.response["delta_internal"] is None
