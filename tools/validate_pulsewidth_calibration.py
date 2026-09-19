"""Held-out and grid-resolution checks for a pulse-duration calibration sidecar."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from ufdemo.calibration import ObservationSpec, PredictionSpec, ExperimentRow, predict_mean_depth  # noqa: E402
from ufdemo.materials import load_material_card  # noqa: E402


def _rows(path: Path) -> list[ExperimentRow]:
    import csv
    out=[]
    with path.open(encoding="utf-8-sig", newline="") as f:
        for rec in csv.DictReader(f):
            out.append(ExperimentRow(
                sample_id=str(rec["sample_id"]), pulse_duration_fs=float(rec["pulse_duration_fs"]),
                repetition_rate_kHz=float(rec["repetition_rate_kHz"]),
                scan_speed_mm_s=float(rec["scan_speed_mm_s"]), hatch_spacing_um=float(rec["hatch_spacing_um"]),
                pass_count=int(rec["pass_count"]), mean_depth_um=float(rec["mean_depth_um"]),
            ))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", type=Path, default=ROOT / "data/calibrations/zirconia_ysz_pulsewidth_calibrated_effective.json")
    ap.add_argument("--long", type=Path, default=ROOT / "runs/calibration/zirconia_pulsewidth_long.csv")
    ap.add_argument("--model-id", default="ysz_pulsewidth_effective_v1")
    ap.add_argument("--window-um", type=float, default=40.0)
    ap.add_argument("--dx-um", type=float, default=2.0)
    ap.add_argument("--representatives", type=int, default=5)
    ap.add_argument("--out", type=Path, default=ROOT / "docs/reports/zirconia_pulsewidth_grid_validation.json")
    args = ap.parse_args(argv)
    card = load_material_card(args.card)
    rows = _rows(args.long)
    selected=[]
    for tau in sorted({r.pulse_duration_fs for r in rows}):
        selected.append(next(r for r in rows if r.pulse_duration_fs == tau))
    selected=selected[:max(1,int(args.representatives))]
    values=[]
    for row in selected:
        preds=[]
        for dx in (float(args.dx_um), float(args.dx_um)/2.0):
            spec=PredictionSpec(material_card_file=str(args.card),window_um=args.window_um,dx_um=dx,
                                machining_region_um=args.window_um,run_mode="synthetic_demo",
                                observation=ObservationSpec(kind="full_region_mean"),response_model_id=args.model_id)
            out=predict_mean_depth(row,spec=spec)
            preds.append({"dx_um":dx,"predicted_depth_um":out.get("predicted_depth_um"),"ok":out.get("ok")})
        a,b=preds
        rel=None
        if a["predicted_depth_um"] is not None and b["predicted_depth_um"] is not None:
            rel=abs(float(a["predicted_depth_um"])-float(b["predicted_depth_um"])) / max(abs(float(b["predicted_depth_um"])),1e-12)
        values.append({"sample_id":row.sample_id,"pulse_duration_fs":row.pulse_duration_fs,"measured_depth_um":row.mean_depth_um,"predictions":preds,"relative_grid_change":rel})
    rels=[v["relative_grid_change"] for v in values if v["relative_grid_change"] is not None]
    report={"schema":"ufdemo.pulse_duration_grid_validation/1","card":str(args.card),"modelId":args.model_id,"representatives":values,"maxRelativeGridChange":max(rels) if rels else None,"meanRelativeGridChange":sum(rels)/len(rels) if rels else None,"note":"dx/2 对照只验证当前窗口与代表工况的数值收敛，不是实验精度结论。"}
    args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(json.dumps(report,ensure_ascii=False,indent=2));return 0


if __name__ == "__main__":
    raise SystemExit(main())

