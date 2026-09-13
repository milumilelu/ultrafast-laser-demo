"""解析作者原始 XLSX → 结构化 CSV（U09）。

来源：Mendeley Data `10.17632/sdz48p9j5m.1`，文件
`CFRP_with_hydroxyapetites_ablation_results.xlsx`（CC BY 4.0）。

**本脚本产出的 CSV 保留三层区分，不得混同：**
1. **实验列** —— `Measured ablated v`（宽度）、`Measured depth`（深度）；
2. **效率列** —— `10^6 um^3/J` 的**平均值**（体积效率，**不是**局部深度）；
3. **条件列** —— Power / Energy / Fluence（由脚本按原文参数计算，非实测）。

单位（**逐列写明，不隐式换算**）：
- `measured_ablated_v_1e6um3` —— 原文列名如此，单位 10^6 μm³（去除体积）；
- `measured_depth_um` —— μm；
- `efficiency_1e6um3_per_J` —— 10^6 μm³/J；
- 换算：`1 (10^6 μm³/J) = 1e-12 m³/J`（1 μm³/μJ = 1e-12 m³/J）。

用法::

    python tools/cfrp_xlsx_to_csv.py <xlsx 路径>
"""

from __future__ import annotations

import csv
import hashlib
import sys
from pathlib import Path
from typing import Any

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_XLSX = Path("C:/tmp/cfrp_dl/CFRP_with_hydroxyapetites_ablation_results.xlsx")
OUT_CSV = ROOT / "data" / "measured" / "cfrp_efficiency_curves.csv"

#: 工作表的配方标签 → 与论文对应关系（**记录配方即可，不因牌号不匹配拒收**）。
RECIPE_LABELS: dict[str, str] = {
    "N1-1%": "CFRP + 1 wt% CuHAp",
    "N1-1%_NoCF": "resin-only (no carbon fibre) + 1 wt% CuHAp",
    "N1-5%": "CFRP + 5 wt% CuHAp",
    "N1-5%_NoCF": "resin-only (no carbon fibre) + 5 wt% CuHAp",
    "N2-5%": "CFRP + 5 wt% (CuHAp:CaHAp 75:25)",
    "N2-5%_NoCF": "resin-only (no carbon fibre) + 5 wt% (CuHAp:CaHAp 75:25)",
    "N3-5%": "CFRP + 5 wt% (CuHAp:CaHAp 50:50)",
    "N3-5%_NoCF": "resin-only (no carbon fibre) + 5 wt% (CuHAp:CaHAp 50:50)",
    "CF+Epoxy": "CFRP + neat epoxy (reference)",
    "Epoxy": "neat epoxy (reference)",
}

#: 摘要与正文的冲突（细则要求：保留正文值并**加标志**，**不得平均**）。
ABSTRACT_VS_TEXT_CONFLICT = {
    "abstract_value_1e6um3_per_J": 6.06,
    "note": ("摘要称混合添加剂效率峰值 6.06（10^6 μm³/J），与正文表格数值不一致；"
             "本表**保留正文数值**并标记冲突，**未做平均**。"),
}

#: 该批数据的固定条件（原文：Beam radius 3.85 μm、Pitch 0.6 μm、Total pulses 166667）。
FIXED_CONDITIONS = {
    "beam_radius_um": 3.85,
    "pitch_um": 0.6,
    "total_pulses": 166667,
    "wavelength_nm": 1030.0,   # 原文条件
    "pulse_duration_fs": 300.0,
}


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s == "" or s.lower() in ("null", "none"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _find_layout(ws: Any) -> dict[str, Any] | None:
    """**按表头文字定位**列与表头行 —— 不要按列号硬编码。

    为什么必须这样：各工作表版式**并不一致**。实测：
    * `N1-*`/`Epoxy` 表头在第 **3** 行、数据自第 **4** 行起，Power 在 **F** 列；
    * `N2-5%_NoCF` 表头在第 **3** 行，但 Power 在 **G** 列（整体右移一列）；
    * `N3-5%` 表头在第 **3** 行、Power 在 **F** 列。

    硬编码列号会：要么整表解析不出来（本项第一版就是），
    要么**把错的列读成深度/效率**（那是静默的错误数据，比解析失败更危险）。
    """
    header_row = None
    cols: dict[str, int] = {}
    for r in range(1, min(8, ws.max_row + 1)):
        found: dict[str, int] = {}
        for c in range(1, ws.max_column + 1):
            v = ws.cell(r, c).value
            if not isinstance(v, str):
                continue
            s = v.strip().lower()
            if "power" in s and "w" in s:
                found["power"] = c
            elif "energy" in s:
                found["energy"] = c
            elif "fluence" in s:
                found["fluence"] = c
            elif "average" in s:
                found["average"] = c
            elif "um^3/j" in s.replace(" ", ""):
                found["efficiency"] = c
        # 表头行必须同时有 Power/Energy/Fluence
        if {"power", "energy", "fluence"} <= set(found):
            header_row, cols = r, found
            break
    if header_row is None:
        return None

    # 「Measured ablated / depth」在**前两列**（表头在更上面一行，可能为空）
    ablated_col, depth_col = None, None
    for r in range(1, header_row + 1):
        for c in range(1, 4):
            v = ws.cell(r, c).value
            if isinstance(v, str):
                s = v.strip().lower()
                if "ablated" in s:
                    ablated_col = c
                elif "depth" in s:
                    depth_col = c
    return {
        "header_row": header_row,
        "data_start": header_row + 1,
        "power": cols["power"],
        "energy": cols["energy"],
        "fluence": cols["fluence"],
        "average": cols.get("average"),
        "efficiency": cols.get("efficiency"),
        "ablated": ablated_col or 2,
        "depth": depth_col or 3,
        "reps": None,   # 体积重复列在 average/efficiency 左侧，运行时推断
    }


def _infer_replicate_cols(ws: Any, lay: dict[str, Any], r: int) -> list[int]:
    """推断 4 个重复体积读数的列：取 Average 左侧最近 4 个有数值的列。"""
    avg = lay.get("average")
    if not avg:
        return []
    out: list[int] = []
    c = avg - 1
    while c >= 1 and len(out) < 4:
        if _num(ws.cell(r, c).value) is not None:
            out.append(c)
        c -= 1
    return sorted(out)


def parse_with_skipped(xlsx: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """解析全部工作表 → 逐（配方 × 功率级别）记录。"""
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        lay = _find_layout(ws)
        if lay is None:
            skipped.append(sheet)
            continue
        for r in range(lay["data_start"], ws.max_row + 1):
            f = _num(ws.cell(r, lay["power"]).value)
            g = _num(ws.cell(r, lay["energy"]).value)
            h = _num(ws.cell(r, lay["fluence"]).value)
            if f is None or g is None or h is None:
                continue
            rep_cols = _infer_replicate_cols(ws, lay, r)
            reps = [_num(ws.cell(r, c).value) for c in rep_cols]
            avg_txt = _num(ws.cell(r, lay["average"]).value) if lay.get("average") else None
            eff = _num(ws.cell(r, lay["efficiency"]).value) if lay.get("efficiency") else None
            no = _num(ws.cell(r, 1).value)
            ablated = _num(ws.cell(r, lay["ablated"]).value)
            depth = _num(ws.cell(r, lay["depth"]).value)
            if no is None and ablated is None and depth is None:
                continue
            reps_present = [v for v in reps if v is not None]
            case_id = f"D04-{sheet}-{int(no) if no is not None else r}"
            rows.append({
                "case_id": case_id,
                "source_id": "D04",
                "sheet": sheet,
                "recipe_label": RECIPE_LABELS.get(sheet, sheet),
                "level_no": int(no) if no is not None else None,
                # --- 条件列（由原文参数给出，非实测）---
                "power_W": f,
                "energy_uJ": g,
                "fluence_J_cm2": h,
                # --- 实验列 ---
                "measured_ablated_v_1e6um3": ablated,
                "measured_depth_um": depth,
                "replicate_count": len(reps_present),
                # --- 效率列（体积效率，**不是**局部深度）---
                "average_1e6um3": avg_txt,
                "efficiency_1e6um3_per_J": eff,
                "efficiency_m3_per_J": (eff * 1e-12) if eff is not None else None,
                # --- 元信息 ---
                "data_kind": "published_experimental_result",
                "output_semantics": "volume_per_energy",
                "column_class": "measured_columns+derived_conditions",
                "source_doi": "10.17632/sdz48p9j5m.1",
                "source_url": "https://data.mendeley.com/datasets/sdz48p9j5m/1",
                "source_locator": f"worksheet '{sheet}'",
                "extraction_method": "xlsx_parse_via_openpyxl",
                "extraction_date": "2026-09-13",
                "license": "CC BY 4.0",
                "unit_note": "1 (10^6 µm³/J) = 1e-12 m³/J；效率是体积效率，不可反推局部深度",
                "grade_confirmation_required": False,
                "quality_note": (
                    "FLAG: abstract-vs-text efficiency conflict preserved (no averaging). "
                    if sheet == "N2-5%" else ""
                ) + (
                    "FLAG: cutting used a SEPARATE 24 µm optical system; "
                    "must NOT be merged with the efficiency dataset."
                ),
            })
    return rows, skipped


def parse(xlsx: Path) -> list[dict[str, Any]]:
    """兼容旧签名（只返回行）。"""
    return parse_with_skipped(xlsx)[0]


def main() -> int:
    xlsx = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_XLSX
    if not xlsx.exists():
        print(f"缺少 XLSX：{xlsx}")
        return 2
    sha = hashlib.sha256(xlsx.read_bytes()).hexdigest()
    rows, skipped = parse_with_skipped(xlsx)
    if not rows:
        print("未解析出数据行")
        return 2

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"  源文件 sha256 = {sha}")
    print(f"  源文件大小   = {xlsx.stat().st_size} 字节")
    n_sheets = len({r["sheet"] for r in rows})
    print(f"  解析行数     = {len(rows)}（{n_sheets} / {n_sheets + len(skipped)} 个工作表）")
    if skipped:
        print(f"  ⚠️ 未解析出的工作表：{skipped}")
    n_exp = sum(1 for r in rows if r["measured_depth_um"] is not None)
    n_eff = sum(1 for r in rows if r["efficiency_1e6um3_per_J"] is not None)
    print(f"  含实验深度列 = {n_exp}；含效率列 = {n_eff}")
    print(f"  冲突标记行   = {sum(1 for r in rows if 'abstract' in r['quality_note'])}")
    print(f"输出：{OUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
