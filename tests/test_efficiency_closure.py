from __future__ import annotations

import copy

import numpy as np

from conftest import load_example, load_material_card, make_config, ROOT
from ufdemo.closures import ConstantEfficiencyClosure, TorchEfficiencyMLP, build_features, load_efficiency_closure
from ufdemo.solver import solve


def _base_raw() -> dict:
    raw = load_example("analytic_single_pulse.json")
    raw["path"]["segments"][0]["end_s"] = 0.002
    raw["solver"]["mode"] = "reference"
    return raw


def test_features_are_causal_and_finite() -> None:
    features = build_features(
        fluence=np.asarray([[2.0, 1.0]]),
        threshold=np.asarray([[1.0, 1.0]]),
        depth_m=np.zeros((1, 2)),
        exposure_count=np.zeros((1, 2)),
        illumination_count=np.zeros((1, 2)),
        r2_m2=np.asarray([[0.0, 1.0]]),
        spot_radius_m=1.0,
        rayleigh_range_m=2.0,
        pulse_duration_s=None,
        pass_index=1,
        event_dt_s=None,
    )
    assert features.shape == (2, 8)
    assert np.all(np.isfinite(features))


def test_eta_one_is_exact_baseline_and_eta_changes_recursive_state() -> None:
    raw = _base_raw()
    material = load_material_card(ROOT / raw["material_card_file"])
    cfg = make_config(raw)
    baseline = solve(cfg, material)
    identity = solve(cfg, material, efficiency_closure=ConstantEfficiencyClosure(1.0))
    half = solve(cfg, material, efficiency_closure=ConstantEfficiencyClosure(0.5))

    assert baseline.ok and identity.ok and half.ok
    np.testing.assert_array_equal(baseline.surface.depth, identity.surface.depth)
    # Unprocessed cells retain the initial maximum height; this guards the
    # global-extrema bookkeeping used by the beam geometry code.
    assert baseline.surface.height_max_m == float(np.max(baseline.surface.height))
    assert float(np.max(half.surface.depth)) < float(np.max(baseline.surface.depth))
    assert identity.metadata["enabled_features"]["efficiency_closure"] is True
    assert half.diagnostics["efficiency_closure"]["events"] > 0


def test_closure_disables_grouped_freeze() -> None:
    raw = _base_raw()
    raw["solver"]["mode"] = "grouped"
    cfg = make_config(raw)
    material = load_material_card(ROOT / raw["material_card_file"])
    result = solve(cfg, material, efficiency_closure=ConstantEfficiencyClosure(1.0))
    assert result.ok
    assert result.metadata["acceleration"]["effective_mode"] == "reference"
    assert any("逐事件效率闭合" in warning for warning in result.warnings)


def test_mlp_checkpoint_roundtrip_preserves_bounded_forward(tmp_path) -> None:
    model = TorchEfficiencyMLP(hidden_dim=4, n_hidden_layers=1, seed=3)
    x = np.zeros((3, 8), dtype=float)
    before = model(x)
    path = tmp_path / "closure.pt"
    model.save_checkpoint(str(path))
    restored = load_efficiency_closure(str(path))
    np.testing.assert_allclose(before, restored(x), rtol=0.0, atol=1e-7)
    assert restored.metadata()["kind"] == "bounded_efficiency_mlp"
