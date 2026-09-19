"""Behavior regressions for the P0 optimization pass (A01--A10)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from ufdemo.config import RunConfig
from ufdemo.materials import load_material_card
from ufdemo.response import FixedThresholdLogLaw, HistoryState
from ufdemo.solver import solve
from ufdemo.webcontract import demo_rect_payload
from ufdemo.metrics import RoiSpec, roi_statistics
from ufdemo.calibration import ExperimentRow, group_split


ROOT = Path(__file__).resolve().parents[1]


def test_incubation_uses_one_based_local_history() -> None:
    law = FixedThresholdLogLaw(
        threshold_internal=10.0,
        delta_internal=1.0,
        incubation={"Fth1_internal": 10.0, "S": 0.5},
    )
    fluence = np.array([10.1])
    values = [
        float(law.increment(fluence, HistoryState(np.array([n], dtype=np.uint32))).values[0])
        for n in range(4)
    ]
    # N=1,2,3,4: a lower threshold gives a strictly increasing response.
    assert values[0] < values[1] < values[2] < values[3]


def test_solver_incubation_first_event_uses_n_one() -> None:
    raw = json.loads((ROOT / "examples" / "ten_pulses.json").read_text(encoding="utf-8"))
    raw["material_card_file"] = "data/materials/zirconia_ysz_machining_effective_n3.json"
    raw["laser"].update(
        wavelength_m=1030e-9,
        pulse_duration_s=208e-15,
        pulse_energy_J=30e-6,
        spot_radius_m=0.874291154e-6,
        repetition_rate_Hz=1000.0,
    )
    raw["path"]["segments"][0]["end_s"] = 0.004
    raw["reference_conditions"] = {
        "protocol_id": "ysz_crown_machining_effective_n3",
        "fluence_basis": "incident_peak_fluence",
    }
    raw["solver"].update(mode="reference", geometry_feedback="fixed_geometry", window_radius_policy="tail_epsilon")
    result = solve(RunConfig.from_dict(raw), load_material_card(ROOT / raw["material_card_file"]))
    assert result.ok
    center = result.surface.nearest_index_x(0.0), result.surface.nearest_index_y(0.0)
    actual = float(result.surface.depth[center[1], center[0]])
    fluence = 2.0 * 30e-6 / (np.pi * (0.874291154e-6 ** 2))
    fth1, delta, exponent = 14830.0, 3.9e-7, 0.969 - 0.0029 * 1.0
    expected = sum(delta * np.log(fluence / (fth1 * (n ** (exponent - 1.0)))) for n in range(1, 5))
    assert actual == pytest.approx(expected, rel=2e-6)


def test_illumination_history_is_distinct_from_removal_count() -> None:
    raw = json.loads((ROOT / "examples" / "ten_pulses.json").read_text(encoding="utf-8"))
    raw["solver"]["mode"] = "reference"
    raw["solver"]["window_radius_policy"] = "tail_epsilon"
    result = solve(RunConfig.from_dict(raw), load_material_card(ROOT / raw["material_card_file"]))
    assert result.surface.illumination_count.max() == 10
    assert result.surface.exposure_count.max() == 10
    s = result.surface
    s.apply_increment(
        np.zeros_like(s.height),
        event_index=999,
        section=(0, s.grid.ny, 0, s.grid.nx),
        illumination_count=np.ones_like(s.illumination_count, dtype=np.uint32),
    )
    assert s.illumination_count.max() == 11
    assert s.exposure_count.max() == 10
    assert result.metadata["effective_configuration"]["derived"]["history_enabled_effective"] is False


def test_actual_demo_uses_shared_background_and_rectangle_roi(tmp_path: Path) -> None:
    payload = demo_rect_payload(
        {
            "processMode": "actual",
            "materialCardFile": "data/materials/zirconia_ysz_machining_effective_n3.json",
            "regionUm": 20.0,
            "hatchUm": 4.0,
            "passes": 1,
            "dxUm": 2.0,
            "pulseDurationFs": 208.0,
            "repetitionRateKHz": 33.3,
            "speedMmS": 279.0,
        },
        out_base=tmp_path,
        project_root=ROOT,
    )
    assert payload["status"] == "completed"
    assert payload["demo"]["processMode"] == "actual"
    assert payload["demo"]["powerRule"] == "P_post_objective/f"
    roi = next(x for x in payload["roiStats"] if x["name"] == "machining_region")
    assert roi["available"] is True
    assert roi["mean_depth_internal"] is not None
    assert payload["stats"]["mean_depth_internal"] != roi["mean_depth_internal"]


def test_rectangle_roi_uses_intersection_area_for_half_cell() -> None:
    class G:
        dx_m = dy_m = 1.0

    class S:
        grid = G()
        x = np.array([0.0, 1.0])
        y = np.array([0.0, 1.0])
        depth = np.array([[1.0, 3.0], [5.0, 7.0]])

    # Bounds cut both cells at half-cell positions in x.
    out = roi_statistics(S(), [RoiSpec(name="r", bounds_xy_m=(-0.25, 1.25, -0.5, 0.5))])[0]
    assert out.actual_area_internal == 1.5
    assert out.mean_depth_internal == (1.0 * 0.75 + 3.0 * 0.75) / 1.5


def test_trajectory_split_keeps_all_passes_on_one_side() -> None:
    rows = [
        ExperimentRow(str(i), 208.0, freq, 10.0, 4.0, n, float(n))
        for i, (freq, n) in enumerate(((20.0, 1), (20.0, 2), (20.0, 3),
                                        (30.0, 1), (30.0, 2), (30.0, 3)), start=1)
    ]
    train, holdout, info = group_split(rows, holdout_groups=1, seed=2, group_by_trajectory=True)
    assert info["grouping"] == "trajectory"
    assert not ({r.trajectory_key for r in train} & {r.trajectory_key for r in holdout})


def test_g02_subcell_response_integrates_before_log() -> None:
    raw = json.loads((ROOT / "examples" / "analytic_single_pulse.json").read_text(encoding="utf-8"))
    raw["solver"].update(mode="reference", window_radius_policy="tail_epsilon")
    raw["grid"].update(nx=25, ny=25, dx_m=1.0e-6, dy_m=1.0e-6)
    material = load_material_card(ROOT / raw["material_card_file"])
    raw["solver"]["subcell_order"] = 1
    centre = solve(RunConfig.from_dict(raw), material)
    raw["solver"]["subcell_order"] = 2
    subcell = solve(RunConfig.from_dict(raw), material)
    assert centre.ok and subcell.ok
    assert subcell.metadata["effective_configuration"]["config"]["solver"]["subcell_order"] == 2
    # The nonlinear threshold edge is intentionally not identical to the
    # centre rule; the test also guards that the explicit mode actually runs.
    assert not np.array_equal(centre.surface.depth, subcell.surface.depth)


def test_g02_grouped_mode_falls_back_explicitly() -> None:
    raw = json.loads((ROOT / "examples" / "analytic_single_pulse.json").read_text(encoding="utf-8"))
    raw["solver"].update(mode="grouped", subcell_order=2, window_radius_policy="tail_epsilon")
    raw["grid"].update(nx=15, ny=15, dx_m=1.0e-6, dy_m=1.0e-6)
    result = solve(RunConfig.from_dict(raw), load_material_card(ROOT / raw["material_card_file"]))
    assert result.ok
    assert result.metadata["acceleration"]["effective_mode"] == "reference"
    assert any("G-02" in warning for warning in result.warnings)
