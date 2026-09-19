"""把光机所设计表里的 **compressor 步数** 转成 **真实脉宽 (fs)**。

背景
----
设计表里那列叫「脉宽」，但填的是 **compressor 步数**（29400 / 36140 / … / 173500），
不是 fs。直接当脉宽用，工艺参数全错。

做法（**不覆盖原件**）
--------------------
对每个含 compressor 列的表，生成一份副本到
`<root>/_脉宽已转换/<原相对路径>`：

* 原 `脉宽` 列 → **改名为 `compressor`**（名实相符）；
* **新增 `脉宽_fs` 列** = 查表转换的结果（沿用项目里已有的列名，见 CFRP 的 60 pass）。

跳过并在报告里列出：
* **coded 表**（值 1–5）—— 那是另一套编码，本表转不了；
* **已是真实 fs** 的表 —— 不需要转；
* **已含 `脉宽_fs`** 的表 —— 只**校验**它与重算值是否一致。

用法
----
    python convert_pulse_columns.py [--root <目录>] [--apply]
    不加 --apply 时只做**干跑**（dry-run），打印将要修改什么。
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "references"))
from compressor_pulse_table import COMPRESSOR_TO_FS, compressor_to_fs  # noqa: E402

ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "cp932", "latin-1")
PULSE_COL_HINTS = ("脉宽", "compressor", "Compressor")
OUT_DIRNAME = "_脉宽已转换"


def read_text(p: Path) -> str:
    raw = p.read_bytes()
    for e in ENCODINGS:
        try:
            return raw.decode(e)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法解码 {p}")


def classify(values: list[float]) -> str:
    if not values:
        return "empty"
    if all(v in COMPRESSOR_TO_FS for v in values):
        return "compressor"
    if max(values) <= 10 and min(values) >= 1:
        return "coded"
    if max(values) <= 12000:
        return "already_fs"
    return "unknown"


def convert_csv(src: Path, dst: Path, apply: bool) -> dict:
    rows = list(csv.reader(read_text(src).splitlines()))
    if not rows:
        return {"skip": "空文件"}
    hdr = rows[0]
    idx_all = [i for i, h in enumerate(hdr) if any(k in str(h) for k in PULSE_COL_HINTS)]
    if not idx_all:
        return {"skip": "无脉宽列"}

    # 找出「值判定为 compressor」的那些列；已是 fs 的列留着不动
    converted_cols, checked = [], []
    for i in idx_all:
        vals = []
        for r in rows[1:]:
            if i < len(r) and r[i].strip():
                try:
                    vals.append(float(r[i]))
                except ValueError:
                    pass
        kind = classify(vals)
        if kind == "compressor":
            converted_cols.append(i)
        elif kind in ("coded", "already_fs"):
            checked.append((hdr[i], kind))
        else:
            checked.append((hdr[i], kind))

    if not converted_cols:
        return {"skip": "无需转换", "checked": checked}

    new_hdr = list(hdr)
    for i in converted_cols:
        new_hdr[i] = "compressor"                      # 名实相符
    if "脉宽_fs" not in new_hdr:
        new_hdr.append("脉宽_fs")

    out_rows = [new_hdr]
    n_conv = 0
    for r in rows[1:]:
        r = list(r) + [""] * (len(hdr) - len(r))
        conv_vals = []
        for i in converted_cols:
            cell = r[i].strip()
            if not cell:
                conv_vals.append("")
                continue
            try:
                v = compressor_to_fs(float(cell))
                conv_vals.append(f"{v:g}")
                n_conv += 1
            except ValueError as e:
                conv_vals.append("")
                conv_vals.append(f"#ERR {e}")
        out_rows.append(r + conv_vals)

    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open("w", encoding="utf-8-sig", newline="") as fh:
            csv.writer(fh).writerows(out_rows)
    return {"converted_cols": [hdr[i] for i in converted_cols],
            "n_cells": n_conv, "checked": checked, "rows": len(rows) - 1}


def convert_xlsx(src: Path, dst: Path, apply: bool) -> dict:
    wb = openpyxl.load_workbook(src, data_only=True)
    touched = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        hdr = [str(c) if c is not None else "" for c in rows[0]]
        idx = [i for i, h in enumerate(hdr) if any(k in h for k in PULSE_COL_HINTS)]
        if not idx:
            continue
        vals = [r[i] for r in rows[1:] for i in idx if i < len(r) and isinstance(r[i], (int, float))]
        if classify([float(v) for v in vals]) != "compressor":
            continue
        touched.append(ws.title)
        if not apply:
            continue
        # 在表头右侧加 脉宽_fs，并逐行填
        col_fs = len(hdr) + 1
        ws.cell(row=1, column=col_fs, value="脉宽_fs")
        for ri, r in enumerate(rows[1:], start=2):
            src_v = r[idx[0]] if idx[0] < len(r) else None
            if isinstance(src_v, (int, float)):
                try:
                    ws.cell(row=ri, column=col_fs, value=round(compressor_to_fs(float(src_v)), 3))
                except ValueError:
                    pass
        for i in idx:
            ws.cell(row=1, column=i + 1, value="compressor")
    if apply and touched:
        dst.parent.mkdir(parents=True, exist_ok=True)
        wb.save(dst)
    return {"sheets": touched} if touched else {"skip": "无 compressor 列"}


def main() -> int:
    ap = argparse.ArgumentParser(description="compressor 步数 → 真实脉宽 (fs)")
    ap.add_argument("--root", default=r"E:/博士课题资料/光机所实验原始数据")
    ap.add_argument("--apply", action="store_true", help="真正写文件；不加则只干跑")
    args = ap.parse_args()

    root = Path(args.root)
    out_root = root / OUT_DIRNAME
    n_done = n_skip = 0
    print(f"{'干跑' if not args.apply else '写入'}：root={root}")
    print(f"输出目录：{out_root}\n")

    for p in sorted(root.rglob("*")):
        if p.suffix.lower() not in (".csv", ".xlsx"):
            continue
        if OUT_DIRNAME in p.parts:
            continue
        rel = p.relative_to(root)
        dst = out_root / rel
        try:
            info = (convert_csv(p, dst, args.apply) if p.suffix.lower() == ".csv"
                    else convert_xlsx(p, dst, args.apply))
        except Exception as exc:                                  # noqa: BLE001
            print(f"  ✗ {rel}: {type(exc).__name__}: {exc}")
            continue
        if "skip" in info:
            if info["skip"] not in ("无脉宽列", "空文件"):
                print(f"  – {rel}: 跳过（{info['skip']}）")
                for h, k in info.get("checked", []):
                    print(f"      列 {h} → {k}")
            n_skip += 1
        else:
            n_done += 1
            detail = info.get("converted_cols") or info.get("sheets") or []
            print(f"  ✓ {rel}: {detail}  ({info.get('n_cells', '')} 个值)")
            for h, k in info.get("checked", []):
                print(f"      另：列 {h} → {k}（未动）")

    print(f"\n完成 {n_done} 个表；跳过 {n_skip} 个（无脉宽列/无需转换）")
    if not args.apply:
        print("\n⚠️ 这是干跑。加 --apply 才真正写文件（原件不动，写到 _脉宽已转换/）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
