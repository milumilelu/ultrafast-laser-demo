"""Build and optionally fit the real zirconia pass scalar data.

The pass height fields are already reduced by the repository's Keyence
pipeline.  This tool deliberately consumes only the audited endpoint summary
(``depth_vs_passes.csv``) and joins it to the converted design table by the
four process variables.  It never pairs rows by file number or by the order
of connected components.

The local pass experiment is outside the literature protocol embedded in the
YSZ card (the card is 208 fs, while this batch contains 223--4000 fs).  The
optional fit therefore runs with ``run_mode=synthetic_demo`` and is reported
as an engineering-effective scalar closure calibration.  It does not mutate
the material card or claim independent material constants.

Examples (from the repository root)::

    PYTHONPATH=src python tools/calibrate_zirconia_pass.py --write-long
    PYTHONPATH=src python tools/calibrate_zirconia_pass.py --run --max-groups 1

The default ``--max-groups 1`` is intentionally small: a 200 x 200 um
reference solve contains tens of thousands of pulse events.  Increase it only
after checking runtime and the grouped hold-out report.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ufdemo.calibration import (  # noqa: E402
    ExperimentRow,
    ObservationSpec,
    PredictionSpec,
    calibrate_gain,
    save_calibration,
)

DEFAULT_SUMMARY = ROOT / "docs" / "experiments" / "laser_proc_2026-09-18" / "depth_vs_passes.csv"
DEFAULT_DESIGN = ROOT / "docs" / "experiments" / "design_tables_pulse_fs" / "氧化锆" / "氧化锆_pass_design.csv"
DEFAULT_CARD = ROOT / "data" / "materials" / "zirconia_ysz_machining_effective_n3.json"
DEFAULT_LONG = ROOT / "runs" / "calibration" / "zirconia_pass_scalar_long.csv"
DEFAULT_RESULT = ROOT / "runs" / "calibration" / "zirconia_pass_gain.json"


def _float(value: Any) -> float:
    return float(str(value).strip())


def _key(tau: float, freq: float, hatch_mm: float, speed: float) -> tuple[float, ...]:
    return (round(float(tau), 6), round(float(freq), 6), round(float(hatch_mm), 9), round(float(speed), 6))


def load_rows(summary_path: Path, design_path: Path, *, monotonic_only: bool = False,
              min_n1_um: float = 0.0) -> tuple[list[ExperimentRow], list[dict[str, Any]]]:
    """Join 15 summary curves to their four design rows without positional guessing."""
    with summary_path.open("r", encoding="utf-8-sig", newline="") as f:
        summary = list(csv.DictReader(f))
    with design_path.open("r", encoding="utf-8-sig", newline="") as f:
        design = list(csv.DictReader(f))

    by_key: dict[tuple[float, ...], list[dict[str, Any]]] = {}
    for d in design:
        k = _key(_float(d["脉宽_fs"]), _float(d["频率"]), _float(d["线间距"]), _float(d["速度"]))
        by_key.setdefault(k, []).append(d)

    rows: list[ExperimentRow] = []
    audit: list[dict[str, Any]] = []
    for s in summary:
        k = _key(_float(s["tau_fs"]), _float(s["f_kHz"]), _float(s["hatch_mm"]), _float(s["v_mm_s"]))
        candidates = by_key.get(k, [])
        by_pass = {int(_float(d["次数"])): d for d in candidates}
        missing = sorted(set(range(1, 5)) - set(by_pass))
        duplicates = sorted(p for p in by_pass if sum(int(_float(d["次数"])) == p for d in candidates) > 1)
        if len(candidates) != 4 or missing or duplicates:
            raise ValueError(f"design join is not one row per pass for key={k}: n={len(candidates)}, missing={missing}, duplicates={duplicates}")
        monotonic = str(s.get("monotonic", "")).strip().lower() in {"true", "1", "yes"}
        n1 = _float(s["N1"])
        selected = (not monotonic_only or monotonic) and n1 >= float(min_n1_um)
        audit.append({
            "key": k,
            "monotonic": monotonic,
            "n1_um": n1,
            "selected": selected,
            "passes": [_float(s[f"N{i}"]) for i in range(1, 5)],
        })
        if not selected:
            continue
        for n in range(1, 5):
            d = by_pass[n]
            rows.append(ExperimentRow(
                sample_id=f"zr-pass-{int(_float(d['加工顺序'])):03d}",
                pulse_duration_fs=_float(d["脉宽_fs"]),
                repetition_rate_kHz=_float(d["频率"]),
                scan_speed_mm_s=_float(d["速度"]),
                hatch_spacing_um=_float(d["线间距"]) * 1000.0,
                pass_count=n,
                mean_depth_um=_float(s[f"N{n}"]),
                source_row=int(_float(d["加工顺序"])) + 1,
                extras={"center_x": d.get("中心X"), "center_y": d.get("中心Y"),
                        "summary_monotonic": monotonic, "source_file": str(summary_path),
                        "observation_definition": "audited_depth_vs_passes_mean_um"},
            ))
    return rows, audit


def write_long(rows: list[ExperimentRow], audit: list[dict[str, Any]], path: Path, *, summary_path: Path, design_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["sample_id", "pulse_duration_fs", "repetition_rate_kHz", "hatch_spacing_um", "pass_count", "scan_speed_mm_s", "mean_depth_um", "center_x", "center_y", "summary_monotonic", "source_file", "observation_definition", "source_row"]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({
                "sample_id": r.sample_id,
                "pulse_duration_fs": r.pulse_duration_fs,
                "repetition_rate_kHz": r.repetition_rate_kHz,
                "hatch_spacing_um": r.hatch_spacing_um,
                "pass_count": r.pass_count,
                "scan_speed_mm_s": r.scan_speed_mm_s,
                "mean_depth_um": r.mean_depth_um,
                "center_x": r.extras.get("center_x"),
                "center_y": r.extras.get("center_y"),
                "summary_monotonic": r.extras.get("summary_monotonic"),
                "source_file": r.extras.get("source_file"),
                "observation_definition": r.extras.get("observation_definition"),
                "source_row": r.source_row,
            })
    audit_path = path.with_suffix(".audit.json")
    audit_path.write_text(json.dumps({"summary": str(summary_path), "design": str(design_path), "curves": audit}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _process_key(r: ExperimentRow) -> tuple[float, ...]:
    # Pass count is deliberately excluded: N=1..4 is one process-condition group.
    return (round(r.pulse_duration_fs, 6), round(r.repetition_rate_kHz, 6), round(r.hatch_spacing_um, 6), round(r.scan_speed_mm_s, 6))


def split_process_groups(rows: list[ExperimentRow], holdout_groups: int, seed: int) -> tuple[list[ExperimentRow], list[ExperimentRow], dict[str, Any]]:
    by: dict[tuple[float, ...], list[ExperimentRow]] = {}
    for r in rows:
        by.setdefault(_process_key(r), []).append(r)
    keys = sorted(by)
    if holdout_groups < 0 or holdout_groups >= len(keys):
        raise ValueError(f"holdout_groups must be in [0,{len(keys)-1}]")
    rng = random.Random(seed)
    shuffled = list(keys)
    rng.shuffle(shuffled)
    hold = set(shuffled[:holdout_groups])
    train = [r for k in keys if k not in hold for r in by[k]]
    test = [r for k in keys if k in hold for r in by[k]]
    return train, test, {"n_process_groups": len(keys), "holdout_groups": [list(k) for k in sorted(hold)], "leakage_groups": sorted(set(_process_key(r) for r in train) & set(_process_key(r) for r in test)), "seed": seed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    ap.add_argument("--design", type=Path, default=DEFAULT_DESIGN)
    ap.add_argument("--card", type=Path, default=DEFAULT_CARD)
    ap.add_argument("--out-long", type=Path, default=DEFAULT_LONG)
    ap.add_argument("--out-result", type=Path, default=DEFAULT_RESULT)
    ap.add_argument("--write-long", action="store_true")
    ap.add_argument("--run", action="store_true", help="run the recursive scalar gain calibration")
    ap.add_argument("--max-groups", type=int, default=1, help="small process-condition subset; 0 means all")
    ap.add_argument("--holdout-groups", type=int, default=0)
    ap.add_argument("--seed", type=int, default=20260919)
    ap.add_argument("--dx-um", type=float, default=2.0)
    ap.add_argument("--window-um", type=float, default=200.0)
    ap.add_argument(
        "--machining-region-um", type=float, default=None,
        help="实际蛇形加工区边长；默认等于 window_um",
    )
    ap.add_argument(
        "--observation-kind", choices=("full_region_mean", "center_window_mean"),
        default="full_region_mean",
    )
    ap.add_argument("--observation-window-um", type=float, default=None)
    ap.add_argument("--observation-statistic", choices=("mean", "median"), default="mean")
    ap.add_argument("--min-n1-um", type=float, default=0.0)
    ap.add_argument("--monotonic-only", action="store_true",
                    help="仅拟合单调曲线；默认保留全部已审计条件并记录非单调标记")
    ap.add_argument("--include-nonmonotonic", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    rows, audit = load_rows(args.summary, args.design, monotonic_only=args.monotonic_only, min_n1_um=args.min_n1_um)
    # Keep a deterministic prefix by process condition for the runnable subset.
    groups = sorted({_process_key(r) for r in rows})
    if args.max_groups > 0:
        keep = set(groups[:args.max_groups])
        rows = [r for r in rows if _process_key(r) in keep]
    if args.write_long or args.run:
        write_long(rows, audit, args.out_long, summary_path=args.summary, design_path=args.design)
    report: dict[str, Any] = {"rows": len(rows), "process_groups": len({_process_key(r) for r in rows}), "long_csv": str(args.out_long), "audit_json": str(args.out_long.with_suffix('.audit.json')), "notes": ["depth labels come from audited depth_vs_passes.csv; design joined by four process variables; N=1..4 remain terminal endpoints", "all audited curves are retained by default; nonmonotonic is a reported observation flag", "this batch is outside the card's 208 fs literature protocol; run_mode=synthetic_demo is explicit and the result is engineering-effective"]}
    if not args.run:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        raise ValueError("no rows selected")
    train, hold, split = split_process_groups(rows, args.holdout_groups, args.seed)
    region = args.machining_region_um
    center_window = None
    if args.observation_kind == "center_window_mean":
        if args.observation_window_um is None:
            raise ValueError("center_window_mean 需要 --observation-window-um")
        center_window = (float(args.observation_window_um), float(args.observation_window_um))
    spec = PredictionSpec(
        material_card_file=str(args.card), run_mode="synthetic_demo",
        window_um=args.window_um, dx_um=args.dx_um,
        machining_region_um=region,
        observation=ObservationSpec(
            kind=args.observation_kind,
            center_window_um=center_window,
            statistic=args.observation_statistic,
        ),
    )
    result = calibrate_gain(train, spec=spec, holdout_rows=hold, bounds=(0.05, 20.0), residual_scale_um=5.0, material_id="zirconia_ysz_pass_experimental_effective")
    save_calibration(result, args.out_result)
    report.update({"split": split, "result_json": str(args.out_result), "metrics": result.metrics(), "gain": result.gain, "hit_bound": result.hit_bound})
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
