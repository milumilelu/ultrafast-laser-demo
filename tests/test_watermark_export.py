"""G09：水印贯穿导出与回放（批次 H / T14，细则 11.3 / 任务书 15.2）。

要证明的不是"字符串里有材料名"，而是**同一枚水印**同时出现在：

* ``metadata.json``（含 ``run_mode`` / ``unit_mode`` 与完整 watermark）；
* ``watermark.json``（单文件即可读出身份与模式）；
* ``statistics.csv`` 的 ``watermark.*`` 自描述行；
* 界面回放（``ui_service.read_existing_run``）读到的标签。

只要有一处各写一份，本文件的"逐字段一致"断言就会失败。
"""

from __future__ import annotations

import csv
import json

import pytest

from conftest import DATA_MATERIALS, ROOT, load_example, make_config
from ufdemo import ui_service as U
from ufdemo.config import RunConfig
from ufdemo.io import load_run, save_run
from ufdemo.materials import load_material_card
from ufdemo.solver import solve

pytestmark = pytest.mark.g09

SIC = "sic_4h_cface_1035nm_multishot"
SIC_CARD = f"data/materials/{SIC}.json"

WATERMARK_KEYS = (
    "material_id",
    "family",
    "grade",
    "evidence_status",
    "source_type",
    "card_version",
    "card_sha256",
    "physical_prediction_allowed",
    "identity_confirmed_by_user",
    "run_mode",
    "unit_mode",
    "physical_depth_export_allowed",
)


def _sic_threshold_raw(**overrides) -> dict:
    raw = {
        "schema_version": "1.0",
        "case_id": "g09_watermark",
        "label": "g09_watermark",
        "run_mode": "threshold_only",
        "unit_system": "SI",
        "material_id": SIC,
        "material_card_file": SIC_CARD,
        "seed": 1,
        "grid": {
            "nx": 21, "ny": 21, "dx_m": 2e-6, "dy_m": 2e-6,
            "center_x_m": 0.0, "center_y_m": 0.0, "origin": "cell_center",
            "initial_surface": "flat", "initial_height_m": 0.0,
        },
        "laser": {
            "wavelength_m": 1.035e-6, "pulse_duration_s": 3e-13, "pulse_energy_J": 1e-5,
            "repetition_rate_Hz": 200000.0, "spot_radius_m": 1e-5,
            "focus_xyz_m": [0.0, 0.0, 0.0], "direction_unit": [0.0, 0.0, 1.0],
            "power_measurement_location": "sample_surface",
            "parameter_sources": ["synthetic_definition"],
        },
        "path": {
            "t0_s": 0.0, "time_tolerance_s": 1e-12,
            "segments": [{
                "segment_id": 0, "pass_id": 0, "start_s": 0.0, "end_s": 2.5e-5,
                "start_xyz_m": [0.0, 0.0, 0.0], "end_xyz_m": [0.0, 0.0, 0.0],
                "speed_m_s": 0.0, "laser_on": True, "label": "point",
            }],
        },
        "solver": {
            "mode": "reference", "geometry_feedback": "fixed_geometry",
            "history_enabled": False, "tail_epsilon": 1e-8,
            "memory_budget_bytes": 1073741824, "budget_safety_factor": 1.5,
            "cancel_check_interval": 256, "acceleration": "off",
            "multiline_incubation": False, "structured_interface": False,
        },
        "output": {
            "snapshot_policy": "events", "snapshot_events": [0],
            "max_snapshots": 4, "max_snapshot_bytes": 134217728,
        },
    }
    for dotted, value in overrides.items():
        target = raw
        parts = dotted.split(".")
        for p in parts[:-1]:
            target = target[p]
        target[parts[-1]] = value
    return raw


@pytest.fixture(scope="module")
def saved(tmp_path_factory):
    out = tmp_path_factory.mktemp("g09_watermark")
    cfg = RunConfig.from_dict(_sic_threshold_raw(), base_dir=str(ROOT))
    card = load_material_card(ROOT / SIC_CARD)
    res = solve(cfg, card)
    res.run_id = "g09_watermark"
    save_run(res, out, project_root=ROOT)
    return out


def test_metadata_carries_run_mode_unit_mode_and_full_watermark(saved):
    md = json.loads((saved / "metadata.json").read_text(encoding="utf-8"))
    assert md["run_mode"] == "threshold_only"
    assert md["unit_mode"] == "SI"
    wm = md["watermark"]
    for k in WATERMARK_KEYS:
        assert k in wm, f"metadata.watermark 缺 {k}"
    assert wm["material_id"] == SIC
    assert wm["run_mode"] == "threshold_only"
    assert wm["geometry_feedback"] == "fixed_geometry"
    assert wm["acceleration"] == "off"


def test_watermark_json_exists_and_matches_metadata(saved):
    wm_file = json.loads((saved / "watermark.json").read_text(encoding="utf-8"))
    md = json.loads((saved / "metadata.json").read_text(encoding="utf-8"))
    assert wm_file, "watermark.json 不得为空"
    assert wm_file == md["watermark"], "watermark.json 与 metadata.watermark 必须逐字段一致"


def test_statistics_csv_has_self_describing_watermark_rows(saved):
    rows = {}
    with open(saved / "statistics.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows[r["metric"]] = r["value"]
    for k in WATERMARK_KEYS:
        assert f"watermark.{k}" in rows, f"statistics.csv 缺 watermark.{k} 行"
    assert rows["watermark.material_id"] == SIC
    assert rows["watermark.run_mode"] == "threshold_only"
    # 单独拿走 statistics.csv 也能读出身份：证据状态不能为空
    assert rows["watermark.evidence_status"] not in ("", "null", None)


def test_load_run_exposes_watermark(saved):
    loaded = load_run(saved)
    assert loaded.watermark, "load_run 必须回读 watermark"
    assert loaded.watermark["material_id"] == SIC
    assert loaded.watermark["run_mode"] == "threshold_only"


def test_replay_watermark_is_same_source_as_export(saved):
    """导出 = 回放：界面读到的标签必须与 watermark.json 逐字段一致。"""
    wm_file = json.loads((saved / "watermark.json").read_text(encoding="utf-8"))
    state = U.new_session(load_example("synthetic_demo_point.json"))
    frozen = U.read_existing_run(state, saved)
    for k in WATERMARK_KEYS:
        assert frozen.material_watermark.get(k) == wm_file.get(k), f"回放与导出在 {k} 上不一致"
    assert frozen.run_mode == wm_file["run_mode"]


def test_submit_watermark_equals_exported(tmp_path):
    """界面提交路径写出的标签，必须与落盘的 watermark.json 相同。"""
    raw = _sic_threshold_raw()
    state = U.new_session(raw)
    card = load_material_card(ROOT / SIC_CARD)
    frozen = U.submit(state, card, out_base=tmp_path / "runs", project_root=ROOT, label="g09wm")
    wm_file = json.loads(
        (tmp_path / "runs" / frozen.run_id / "watermark.json").read_text(encoding="utf-8")
    )
    for k in WATERMARK_KEYS:
        assert frozen.material_watermark.get(k) == wm_file.get(k), f"submit 与导出在 {k} 上不一致"
    assert frozen.material_watermark["run_mode"] == "threshold_only"


def test_synthetic_mode_watermark_flags_non_physical():
    """合成模式：标签必须写明非物理预测，且禁止物理深度导出。"""
    from conftest import load_example as _le
    from ufdemo.materials import load_material_card as _card

    raw = _le("synthetic_demo_point.json")
    cfg = RunConfig.from_dict(raw, base_dir=str(ROOT))
    card = _card(ROOT / raw["material_card_file"])
    wm = U.watermark_of(card, cfg.unit, run_mode=cfg.run_mode)
    assert wm["run_mode"] == "synthetic_demo"
    assert wm["physical_prediction_allowed"] is False
    assert wm["physical_depth_export_allowed"] is False
    label = U.format_watermark(wm)
    assert "非物理预测" in label
    U.assert_safe_wording(label, where="watermark.synthetic")
