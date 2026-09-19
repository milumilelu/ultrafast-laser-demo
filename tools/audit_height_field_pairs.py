#!/usr/bin/env python
"""Audit height-field inventory and explicit process metadata (P1-01).

This is deliberately conservative: it reports files that can be read and
metadata that is present, but never guesses a height-field ↔ design-row pair.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


MATERIAL_DIRS = {
    "zirconia": "zr02_pass_60",
    "sic": "sic_pass",
    "cfrp": "cfrp_pass",
    "alsic": "alsic_pass",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--height-root", type=Path, default=Path("docs/experiments/height_fields"))
    ap.add_argument("--design-root", type=Path, default=Path("docs/experiments/design_tables_pulse_fs"))
    ap.add_argument("--output", type=Path, default=Path("runs/data_audit/height_field_pairs.json"))
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    height_root = args.height_root if args.height_root.is_absolute() else root / args.height_root
    design_root = args.design_root if args.design_root.is_absolute() else root / args.design_root

    # Import after resolving the repository root so the script works directly
    # from PowerShell without an editable install.
    import sys
    sys.path.insert(0, str(root / "src"))
    from ufdemo.observations import load_height_field

    materials: dict[str, dict] = {}
    for material, folder in MATERIAL_DIRS.items():
        d = height_root / folder
        rows: list[dict] = []
        for path in sorted(d.glob("*.npz")) if d.exists() else []:
            try:
                obs = load_height_field(path)
                rows.append({
                    "file": str(path.relative_to(root)),
                    "sample_id": obs.meta.sample_id,
                    "pass_count": obs.meta.pass_count,
                    "shape": list(obs.meta.shape),
                    "dx_um": obs.meta.dx_um,
                    "dy_um": obs.meta.dy_um,
                    "valid_fraction": obs.meta.valid_fraction,
                    "index_meta": dict(obs.meta.index_meta),
                    "pair_status": "metadata_only",
                    "pair_reason": "没有把文件名或空间位置猜成设计行；需要人工/索引确认加工区对应关系。",
                })
            except Exception as exc:  # noqa: BLE001
                rows.append({"file": str(path.relative_to(root)), "pair_status": "unreadable", "pair_reason": f"{type(exc).__name__}: {exc}"})
        materials[material] = {
            "folder": str(d.relative_to(root)) if d.exists() else str(d),
            "height_field_count": len(rows),
            "readable_count": sum(r.get("pair_status") == "metadata_only" for r in rows),
            "unreadable_count": sum(r.get("pair_status") == "unreadable" for r in rows),
            "records": rows,
        }

    design_files: list[dict] = []
    for p in sorted(design_root.rglob("*.csv")) if design_root.exists() else []:
        try:
            with p.open("r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.reader(fh)
                header = next(reader, [])
                n = sum(1 for _ in reader)
            design_files.append({"file": str(p.relative_to(root)), "header": header, "row_count": n})
        except Exception as exc:  # noqa: BLE001
            design_files.append({"file": str(p.relative_to(root)), "error": f"{type(exc).__name__}: {exc}"})

    payload = {
        "schema": "ufdemo.height_field_pair_audit/1",
        "materials": materials,
        "design_files": design_files,
        "pairing_policy": "explicit_metadata_only",
        "note": "高度场是终态观测；没有证据时不相减构造逐遍增量，不把149个文件当独立时间序列。",
    }
    out = args.output if args.output.is_absolute() else root / args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(out), "materials": {k: {x: v[x] for x in ("height_field_count", "readable_count", "unreadable_count")} for k, v in materials.items()}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

