from __future__ import annotations

import json
from pathlib import Path

import pytest

from ufdemo.materials import MaterialSpec, resolve_pulse_duration_response
from ufdemo.response import build_pulse_law


def _card() -> MaterialSpec:
    return MaterialSpec.from_dict({
        "id": "fixture_pulsewidth",
        "evidence_status": "unverified",
        "source_type": "analytic_test_definition",
        "fixture_only": True,
        "response": {
            "kind": "log_fixed", "output_semantics": "event_depth_increment",
            "fluence_basis": "incident_peak_fluence", "depth_direction": "surface_normal",
            "threshold_J_m2": 100.0, "delta_m": 1e-6,
        },
        "validity_domain": {"scope": "analytic_all"},
        "pulse_duration_models": [{
            "id": "fixture_v1", "kind": "power_law_response",
            "reference_pulse_duration_fs": 100.0,
            "reference_response": {"threshold_J_m2": 100.0, "delta_m": 1e-6},
            "exponents": {"threshold": 0.5, "delta": -0.5},
            "validity_domain": {"scope": "fixture", "pulse_duration_fs": [100.0, 400.0]},
        }],
    })


def test_pulse_duration_response_resolves_and_preserves_base():
    m = _card()
    base = dict(m.response)
    response, meta = resolve_pulse_duration_response(m, 200e-15, model_id="fixture_v1")
    assert response["threshold_J_m2"] == pytest.approx(100.0 * 2**0.5)
    assert response["delta_m"] == pytest.approx(1e-6 / 2**0.5)
    assert meta["trust_state"] == "calibrated_interpolation"
    assert dict(m.response) == base


def test_pulse_duration_response_rejects_domain_escape():
    with pytest.raises(Exception) as exc:
        resolve_pulse_duration_response(_card(), 500e-15, model_id="fixture_v1")
    assert "超出校准模型有效域" in str(exc.value)


def test_response_law_carries_model_provenance():
    law = build_pulse_law(
        _card(), laser={"pulse_duration_s": 200e-15, "wavelength_m": 1.03e-6,
                       "repetition_rate_Hz": 10_000.0}, response_model_id="fixture_v1")
    assert law.response_model["model_id"] == "fixture_v1"
    assert law.threshold_internal == pytest.approx(100.0 * 2**0.5)


def test_real_calibrated_card_is_separate_from_literature_card():
    root = Path(__file__).resolve().parents[1]
    calibrated = root / "data/calibrations/zirconia_ysz_pulsewidth_calibrated_effective.json"
    literature = root / "data/materials/zirconia_ysz_machining_effective_n3.json"
    assert calibrated.exists()
    assert literature.read_bytes() != calibrated.read_bytes()
    m = __import__("ufdemo.materials", fromlist=["load_material_card"]).load_material_card(calibrated)
    assert m.pulse_duration_model("ysz_pulsewidth_effective_v1") is not None
    response, meta = resolve_pulse_duration_response(m, 500e-15, model_id="ysz_pulsewidth_effective_v1")
    assert response["threshold_J_m2"] > 0 and response["delta_m"] > 0
    assert meta["pulse_duration_fs"] == pytest.approx(500.0)

