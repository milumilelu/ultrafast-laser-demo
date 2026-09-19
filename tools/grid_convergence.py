#!/usr/bin/env python
"""Run a fixed-physics three-level grid convergence baseline (G-01).

The script never refits a material parameter between grids.  It changes only
the cell spacing while preserving the physical domain and event path, then
records domain and ROI observations, a half-cell origin shift, and wall time.
It is intentionally a small acceptance baseline, not a claim that every
composite or history-enabled production case is converged.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np


def _run_one(raw: dict, card_path: Path, *, dx_m: float, shift_x_m: float) -> dict:
    from ufdemo.config import RunConfig
    from ufdemo.materials import load_material_card
    from ufdemo.solver import solve

    cfg = json.loads(json.dumps(raw))
    g0 = cfg["grid"]
    width_m = float(g0["nx"]) * float(g0["dx_m"])
    height_m = float(g0["ny"]) * float(g0["dy_m"])
    cfg["grid"].update(
        nx=max(5, int(round(width_m / dx_m)) + 1),
        ny=max(5, int(round(height_m / dx_m)) + 1),
        dx_m=dx_m,
        dy_m=dx_m,
        center_x_m=float(shift_x_m),
    )
    cfg["solver"]["mode"] = "reference"
    cfg["solver"]["acceleration"] = "off"
    cfg["solver"]["window_radius_policy"] = "tail_epsilon"
    cfg["output"]["snapshot_policy"] = "none"
    t0 = time.perf_counter()
    result = solve(RunConfig.from_dict(cfg), load_material_card(card_path))
    elapsed = time.perf_counter() - t0
    roi = list(result.rois or [])
    st = dict(result.statistics or {})
    depth = np.asarray(result.surface.depth, dtype=float)
    return {
        "dx_um": dx_m * 1e6,
        "shift_x_um": shift_x_m * 1e6,
        "nx": int(result.surface.grid.nx),
        "ny": int(result.surface.grid.ny),
        "events": int(result.events_processed),
        "mean_depth_m": st.get("mean_depth_internal"),
        "max_depth_m": st.get("max_depth_internal"),
        "volume_m3": st.get("removal_volume_internal"),
        "roi": roi,
        "depth_l2_m": float(np.linalg.norm(depth.ravel())),
        "elapsed_s": elapsed,
        "status": result.status,
        "warnings": list(result.warnings),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path("examples/ten_pulses.json"))
    ap.add_argument("--output", type=Path, default=Path("runs/grid_convergence/g01.json"))
    ap.add_argument("--dx-um", nargs="+", type=float, default=[0.5, 0.25, 0.125])
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    config_path = (root / args.config).resolve() if not args.config.is_absolute() else args.config.resolve()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    card_path = Path(raw["material_card_file"])
    if not card_path.is_absolute():
        card_path = (root / card_path).resolve()
    sys.path.insert(0, str(root / "src"))

    grids: list[dict] = []
    for dx_um in sorted(set(float(x) for x in args.dx_um), reverse=True):
        dx_m = dx_um * 1e-6
        grids.append(_run_one(raw, card_path, dx_m=dx_m, shift_x_m=0.0))
    finest = grids[-1]
    for row in grids:
        den = max(abs(float(finest["mean_depth_m"] or 0.0)), 1e-30)
        row["mean_depth_rel_to_finest"] = abs(float(row["mean_depth_m"] or 0.0) - float(finest["mean_depth_m"] or 0.0)) / den
        denv = max(abs(float(finest["volume_m3"] or 0.0)), 1e-30)
        row["volume_rel_to_finest"] = abs(float(row["volume_m3"] or 0.0) - float(finest["volume_m3"] or 0.0)) / denv

    # A half-cell origin shift at the finest grid isolates grid placement
    # sensitivity from the ordinary refinement trend.
    shift = _run_one(raw, card_path, dx_m=float(args.dx_um[-1]) * 1e-6, shift_x_m=0.5 * float(args.dx_um[-1]) * 1e-6)
    base = finest
    shift["mean_depth_rel_to_unshifted"] = abs(float(shift["mean_depth_m"] or 0.0) - float(base["mean_depth_m"] or 0.0)) / max(abs(float(base["mean_depth_m"] or 0.0)), 1e-30)
    shift["volume_rel_to_unshifted"] = abs(float(shift["volume_m3"] or 0.0) - float(base["volume_m3"] or 0.0)) / max(abs(float(base["volume_m3"] or 0.0)), 1e-30)

    payload = {
        "schema": "ufdemo.g01.grid_convergence/1",
        "config": str(config_path),
        "material_card": str(card_path),
        "fixed_physics": {"refit": False, "solver_mode": "reference", "window": "tail_epsilon"},
        "grids": grids,
        "half_cell_shift": shift,
        "acceptance_hint": {"mean_depth_or_volume_target": 0.02, "macro_profile_target": 0.05},
        "note": "三级网格与半单元偏移基线；不代表复合材料、历史或神经闭合已经收敛。",
    }
    out = args.output if args.output.is_absolute() else root / args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(out), "grids": grids, "half_cell_shift": shift}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

