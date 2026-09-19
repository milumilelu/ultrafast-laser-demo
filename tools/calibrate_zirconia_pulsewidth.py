"""Fit and materialize the pulse-duration-conditioned YSZ response model.

This consumes the audited wide pass table plus the pulse-duration design table,
keeps every condition (including non-monotonic curves), performs a grouped
leave-one-pulse-duration-out split, and writes a sidecar calibration card.  The
original literature card is never edited.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ufdemo.calibration import (  # noqa: E402
    ObservationSpec,
    PredictionSpec,
    calibrate_pulse_duration_response,
    save_pulse_duration_calibration,
)
from ufdemo.materials import load_material_card  # noqa: E402
from calibrate_zirconia_pass import load_rows  # noqa: E402

DEFAULT_SUMMARY = ROOT / "docs" / "experiments" / "laser_proc_2026-09-18" / "depth_vs_passes.csv"
DEFAULT_DESIGN = ROOT / "docs" / "experiments" / "design_tables_pulse_fs" / "氧化锆" / "氧化锆_pass_design.csv"
DEFAULT_CARD = ROOT / "data" / "materials" / "zirconia_ysz_machining_effective_n3.json"
DEFAULT_RESULT = ROOT / "runs" / "calibration" / "zirconia_pulsewidth_response.json"
DEFAULT_CARD_OUT = ROOT / "data" / "calibrations" / "zirconia_ysz_pulsewidth_calibrated_effective.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    ap.add_argument("--design", type=Path, default=DEFAULT_DESIGN)
    ap.add_argument("--card", type=Path, default=DEFAULT_CARD)
    ap.add_argument("--out-result", type=Path, default=DEFAULT_RESULT)
    ap.add_argument("--out-card", type=Path, default=DEFAULT_CARD_OUT)
    ap.add_argument("--window-um", type=float, default=40.0)
    ap.add_argument("--dx-um", type=float, default=2.0)
    ap.add_argument("--max-nfev", type=int, default=4)
    ap.add_argument("--holdout-tau-fs", type=float, default=None)
    ap.add_argument("--monotonic-only", action="store_true")
    args = ap.parse_args(argv)

    rows, audit = load_rows(args.summary, args.design, monotonic_only=args.monotonic_only)
    taus = sorted({float(r.pulse_duration_fs) for r in rows})
    if len(taus) < 2:
        raise SystemExit("至少需要两个脉宽档才能做留一脉宽验证")
    hold_tau = float(args.holdout_tau_fs) if args.holdout_tau_fs is not None else max(taus)
    hold = [r for r in rows if abs(r.pulse_duration_fs - hold_tau) < 1e-9]
    train = [r for r in rows if abs(r.pulse_duration_fs - hold_tau) >= 1e-9]
    if not hold or not train:
        raise SystemExit(f"留出脉宽 {hold_tau:g} fs 不存在，候选={taus}")

    spec = PredictionSpec(
        material_card_file=str(args.card), window_um=float(args.window_um), dx_um=float(args.dx_um),
        machining_region_um=float(args.window_um),
        observation=ObservationSpec(kind="full_region_mean", statistic="mean"),
    )
    result = calibrate_pulse_duration_response(
        train, holdout_rows=hold, spec=spec,
        material_id="zirconia_ysz_pulsewidth_calibrated_effective",
        model_id="ysz_pulsewidth_effective_v1",
        max_nfev=max(1, int(args.max_nfev)),
    )
    result.grid_validation = {
        "status": "solver_grid_declared",
        "fit_dx_um": float(args.dx_um),
        "fit_window_um": float(args.window_um),
        "note": "网格检查使用相同递推核；改变 dx 后需重新运行本工具或独立验证脚本，不自动宣称收敛。",
    }
    result_path = save_pulse_duration_calibration(result, args.out_result)

    raw = json.loads(args.card.read_text(encoding="utf-8"))
    raw["id"] = "zirconia_ysz_pulsewidth_calibrated_effective"
    raw["card_version"] = "1.0.0-calibrated-pulsewidth"
    raw["evidence_status"] = "unverified"
    raw["source_type"] = "user_supplied_table"
    raw["pulse_duration_models"] = [result.model_definition()]
    raw["validity_domain"] = {
        "scope": "calibrated_experiment_range",
        "pulse_duration_fs": list(result.pulse_duration_range_fs),
        "note": "实验条件化有效模型；不覆盖原始 208 fs 文献卡。",
    }
    raw.setdefault("provenance", {})["calibration_result"] = str(result_path)
    raw.setdefault("limitations", []).extend([
        "脉宽条件化参数来自同批实验终态递推拟合，属于工程有效闭合，不是独立单脉冲材料常数。",
        "当前域为实验覆盖的 223–4000 fs（或本次数据实际范围），域外不自动外推。",
    ])
    args.out_card.parent.mkdir(parents=True, exist_ok=True)
    args.out_card.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = {
        "rows": len(rows), "trainRows": len(train), "holdoutRows": len(hold),
        "pulseDurationsFs": taus, "holdoutPulseDurationFs": hold_tau,
        "nonMonotonicConditions": sum(1 for a in audit if not a.get("monotonic", True)),
        "result": str(result_path), "card": str(args.out_card),
        "metrics": result.to_dict()["metrics"], "model": result.model_definition(),
        "notes": list(result.notes),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

