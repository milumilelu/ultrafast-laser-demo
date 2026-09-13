"""实测数据可用性 QA（U04）。

对 `data/measured/` 里的审计数据包做**数据可用性**验收，**不做实验复现**。

检查项
------
1. 逐文件 sha256 与行数 —— 与 `data/measured/manifest.json` 逐一相等；
2. 计数口径 —— main=65、controls=4、唯一 `case_id`=65、族数=6；
3. 单位成对换算一致 —— `*_um × 1e-6 == *_m`、`J_cm2 × 1e4 == J_m2` 等，
   **不隐式换算**，只核对两份记录彼此自洽；
4. DD6 第 14 行锥度异常 —— `taper_reported_deg != taper_recomputed_deg`
   且不一致被显式标记、**原文值保留未被静默修正**；
5. `no_model_generated_measured_rows` —— 不存在由公式采样/模型预测生成的「实测」行；
6. 身份列非空 —— `case_id`/`source_id`/`data_kind`/`output_semantics`/`source_locator`；
7. 缺失物理量保持 `null` —— 不得用 0 或近似值补（抽查金刚石 `pulse_duration_fs`）；
8. **语义红线** —— 导入数据里不得出现 `event_depth_increment`
   （累计深度/体积效率等**不得**改名混入逐事件主循环）。

输出：`docs/reports/measured_data_qa.csv` 与 `.md`。
有失败项时退出码 1（可直接进 CI），且**如实列出全部失败项**，不掩盖。

用法::

    python tools/measured_data_report.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MEASURED = ROOT / "data" / "measured"
REPORTS = ROOT / "docs" / "reports"

IDENTITY_COLS = (
    "case_id",
    "source_id",
    "data_kind",
    "output_semantics",
    "source_locator",
)

# 身份列之外还要求非空的来源/方法列（manifest 声明的保留字段）
REQUIRED_NONEMPTY = ("source_doi", "source_url", "extraction_method", "extraction_date")

# 单位成对：(惯用列, SI 列, 换算系数, 说明)
UNIT_PAIRS: tuple[tuple[str, str, float, str], ...] = (
    ("depth_um", "depth_m", 1e-6, "1 µm = 1e-6 m"),
    ("width_um", "width_m", 1e-6, "1 µm = 1e-6 m"),
    ("Ra_um", "Ra_m", 1e-6, "1 µm = 1e-6 m"),
    ("Sa_um", "Sa_m", 1e-6, "1 µm = 1e-6 m"),
    ("diameter_um", "diameter_m", 1e-6, "1 µm = 1e-6 m"),
    ("peak_fluence_J_cm2", "peak_fluence_J_m2", 1e4, "1 J/cm² = 1e4 J/m²"),
    # 1 µm³/µJ = 1e-18 m³ / 1e-6 J = 1e-12 m³/J
    ("efficiency_um3_per_uJ", "efficiency_m3_per_J", 1e-12, "1 µm³/µJ = 1e-12 m³/J"),
)

REL_TOL = 1e-9


def _rows(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))


def _num(v: Any) -> float | None:
    """空串 / None → None；否则 float。**不把缺失当 0**。"""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("null", "none", "nan"):
        return None
    return float(s)


def _close(a: float, b: float) -> bool:
    scale = max(abs(a), abs(b))
    if scale == 0.0:
        return True
    return abs(a - b) / scale <= REL_TOL


class Row(dict):
    pass


def check() -> list[Row]:
    out: list[Row] = []

    def add(group: str, item: str, expected: str, measured: str, ok: bool, note: str = "") -> None:
        out.append(Row(group=group, check=item, expected=expected, measured=measured,
                       status="通过" if ok else "失败", note=note))

    manifest_path = MEASURED / "manifest.json"
    if not manifest_path.exists():
        add("manifest", "存在 manifest.json", "存在", "缺失", False)
        return out
    man = json.loads(manifest_path.read_text(encoding="utf-8"))
    add("manifest", "存在 manifest.json", "存在", "存在", True,
        f"schema={man.get('schema')} prepared_on={man.get('prepared_on')}")

    # --- 1. 逐文件 sha256 与行数 -------------------------------------------
    missing = [e["file"] for e in man["files"] if not (MEASURED / Path(e["file"]).name).exists()]
    add("完整性", "manifest 列出的文件都存在", "0 缺失",
        f"{len(missing)} 缺失" + (f"：{missing}" if missing else ""), not missing)

    sha_bad: list[str] = []
    row_bad: list[str] = []
    for e in man["files"]:
        name = Path(e["file"]).name
        p = MEASURED / name
        if not p.exists():
            continue
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        if got != e["sha256"]:
            sha_bad.append(name)
        n = len([ln for ln in p.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]) - 1
        if n != e["rows"]:
            row_bad.append(f"{name}({n}≠{e['rows']})")
    add("完整性", "逐文件 sha256 与 manifest 相等", "全部相等",
        "全部相等" if not sha_bad else f"不等：{sha_bad}", not sha_bad)
    add("完整性", "逐文件行数与 manifest 相等", "全部相等",
        "全部相等" if not row_bad else f"不等：{row_bad}", not row_bad)

    # --- 2. 计数口径 --------------------------------------------------------
    laser_files = ["diamond_rsm_measured.csv", "ceramics_dot_line_measured.csv",
                   "ceramics_laser_roughness_measured.csv", "sic_single_pulse_measured.csv",
                   "cfrp_efficiency_reported_maxima.csv", "dd6_drilling_measured.csv"]
    control_files = ["ceramics_nonlaser_controls.csv"]

    laser_rows: list[dict[str, str]] = []
    for f in laser_files:
        laser_rows += _rows(MEASURED / f)
    control_rows: list[dict[str, str]] = []
    for f in control_files:
        control_rows += _rows(MEASURED / f)

    n_main, n_ctrl = len(laser_rows), len(control_rows)
    add("计数", "激光观测行数 = manifest.main_record_count",
        str(man["main_record_count"]), str(n_main), n_main == man["main_record_count"])
    add("计数", "非激光对照行数 = manifest.nonlaser_control_count",
        str(man["nonlaser_control_count"]), str(n_ctrl),
        n_ctrl == man["nonlaser_control_count"])

    ids = [r["case_id"] for r in laser_rows]
    add("计数", "唯一 case_id 数 = 激光行数", str(n_main), str(len(set(ids))),
        len(set(ids)) == n_main)

    fams = sorted({r["material_family"] for r in laser_rows})
    add("计数", "材料族数（激光数据）", "6", str(len(fams)),
        len(fams) == 6, "、".join(fams))

    # jsonl 与 CSV 是同一数据的不同表示，行数须一致，且不得重复计数
    jl = MEASURED / "all_measured_cases.jsonl"
    if jl.exists():
        n_jl = len([ln for ln in jl.read_text(encoding="utf-8").splitlines() if ln.strip()])
        add("计数", "all_measured_cases.jsonl 行数 = 激光观测数（同一数据的另一表示，不重复计数）",
            str(n_main), str(n_jl), n_jl == n_main)

    # --- 3. 单位成对换算 ----------------------------------------------------
    pair_checked = 0
    pair_bad: list[str] = []
    for f in laser_files:
        for r in _rows(MEASURED / f):
            for conv_col, si_col, factor, _desc in UNIT_PAIRS:
                if conv_col not in r or si_col not in r:
                    continue
                a, b = _num(r[conv_col]), _num(r[si_col])
                if a is None or b is None:
                    continue
                pair_checked += 1
                if not _close(a * factor, b):
                    pair_bad.append(f"{r['case_id']}:{conv_col}×{factor:g}={a*factor:g} vs {si_col}={b:g}")
    add("单位", "成对单位换算自洽（不隐式换算，只核两份记录彼此自洽）",
        "全部自洽", f"核对 {pair_checked} 对，{len(pair_bad)} 处不符",
        not pair_bad, "；".join(pair_bad[:3]))

    # --- 4. DD6 第 14 行锥度异常 -------------------------------------------
    dd6 = _rows(MEASURED / "dd6_drilling_measured.csv")
    r14 = next((x for x in dd6 if x.get("case_id") == "D05-14"), None)
    if r14 is None:
        add("异常保留", "DD6 第 14 行存在", "D05-14", "缺失", False)
    else:
        rep, rec = _num(r14.get("taper_reported_deg")), _num(r14.get("taper_recomputed_deg"))
        add("异常保留", "D05-14 原文锥度与复算值并存且不相等",
            "两者均非空且不相等",
            f"reported={rep} recomputed={rec}",
            rep is not None and rec is not None and not _close(rep, rec),
            "原文值保留、未静默修正")
        add("异常保留", "D05-14 不一致被显式标记",
            "taper_consistent_with_diameters=False",
            str(r14.get("taper_consistent_with_diameters")),
            str(r14.get("taper_consistent_with_diameters")).strip().lower() == "false")
        qn = (r14.get("quality_note") or "")
        add("异常保留", "D05-14 quality_note 说明排除该原文锥度参与拟合",
            "含 FLAG / exclude", qn[:60] + ("…" if len(qn) > 60 else ""),
            "FLAG" in qn.upper() and "exclude" in qn.lower())

    # --- 5. 不得有模型生成行 -----------------------------------------------
    add("来源纯净", "manifest.measurements_generated_by_model",
        "False", str(man.get("measurements_generated_by_model")),
        man.get("measurements_generated_by_model") is False)
    add("来源纯净", "manifest.raw_author_files_included（作者原始文件不在包内）",
        "False", str(man.get("raw_author_files_included")),
        man.get("raw_author_files_included") is False)
    kinds = sorted({r["data_kind"] for r in laser_rows})
    bad_kind = [k for k in kinds if "model" in k.lower() or "synthetic" in k.lower()
                or "formula" in k.lower()]
    add("来源纯净", "data_kind 中无 model/synthetic/formula 生成行",
        "无", "无" if not bad_kind else str(bad_kind), not bad_kind, "、".join(kinds))

    # --- 6. 身份列非空 ------------------------------------------------------
    empty_ident: list[str] = []
    empty_req: list[str] = []
    for r in laser_rows + control_rows:
        for c in IDENTITY_COLS:
            if not (r.get(c) or "").strip():
                empty_ident.append(f"{r.get('case_id','?')}:{c}")
        for c in REQUIRED_NONEMPTY:
            if not (r.get(c) or "").strip():
                empty_req.append(f"{r.get('case_id','?')}:{c}")
    add("字段完整", "身份列全部非空（" + "/".join(IDENTITY_COLS) + "）",
        "0 空", f"{len(empty_ident)} 空", not empty_ident, "；".join(empty_ident[:3]))
    add("字段完整", "来源/方法列全部非空（" + "/".join(REQUIRED_NONEMPTY) + "）",
        "0 空", f"{len(empty_req)} 空", not empty_req, "；".join(empty_req[:3]))

    # --- 7. 缺失保持 null，不得填 0 ----------------------------------------
    dia = _rows(MEASURED / "diamond_rsm_measured.csv")
    n_null_pd = sum(1 for r in dia if _num(r.get("pulse_duration_fs")) is None)
    add("缺失语义", "金刚石 pulse_duration_fs 保持 null（原文只给设备最小脉宽，不得补值）",
        f"{len(dia)} 行全为 null", f"{n_null_pd}/{len(dia)} 为 null",
        n_null_pd == len(dia), "配套 equipment_min_pulse_duration_fs=250 fs 另列")
    zero_pd = [r["case_id"] for r in dia if _num(r.get("pulse_duration_fs")) == 0.0]
    add("缺失语义", "金刚石 pulse_duration_fs 无 0 值冒充（null≠0）",
        "0 行", f"{len(zero_pd)} 行", not zero_pd, "；".join(zero_pd[:3]))

    # --- 8. 语义红线 --------------------------------------------------------
    sem = sorted({r["output_semantics"] for r in laser_rows + control_rows})
    banned = [s for s in sem if s == "event_depth_increment"]
    add("语义红线", "导入数据不含 event_depth_increment（累计/体积等不得改名混入逐事件主循环）",
        "不含", "不含" if not banned else str(banned), not banned, "、".join(sem))

    return out


def write_reports(rows: list[Row]) -> tuple[Path, Path]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    csv_path = REPORTS / "measured_data_qa.csv"
    md_path = REPORTS / "measured_data_qa.md"

    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["group", "check", "expected", "measured", "status", "note"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    n_pass = sum(1 for r in rows if r["status"] == "通过")
    n_fail = len(rows) - n_pass
    L = [
        "# 实测数据可用性 QA（U04）",
        "",
        "- 对象：`data/measured/`（审计包 `ultrafast_audit_realdata_20260911.zip`，2026-09-11 构建）",
        "- 口径：**65 条已发表条件/结果记录 + 4 条非激光对照**；"
        "这是**观测记录数**，不是 65 个独立数据集、不是 65 张材料卡。",
        "  数据来源为论文表格/正文的**人工转录**（`manual_transcription_...`），"
        "**不含**作者仪器原始文件、**不含**公式采样或模型预测行。",
        "- 本检查是**数据可用性**验收，**不做实验复现**；软件跑通 ≠ 材料验证。",
        "",
        f"- 汇总：通过 {n_pass}｜失败 {n_fail}",
        "",
        "## 逐项",
        "",
        "| 组 | 检查项 | 期望 | 实测 | 状态 | 说明 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        L.append(f"| {r['group']} | {r['check']} | {r['expected']} | {r['measured']} | "
                 f"{r['status']} | {r['note']} |")
    if n_fail:
        L += ["", "## 失败项", ""]
        for r in rows:
            if r["status"] != "通过":
                L.append(f"- **{r['check']}**：期望 {r['expected']}，实测 {r['measured']}"
                         + (f"（{r['note']}）" if r["note"] else ""))
    md_path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return csv_path, md_path


def main() -> int:
    rows = check()
    csv_path, md_path = write_reports(rows)
    n_fail = sum(1 for r in rows if r["status"] != "通过")
    for r in rows:
        mark = "✓" if r["status"] == "通过" else "✗"
        print(f"  {mark} [{r['group']}] {r['check']}")
        if r["status"] != "通过":
            print(f"        期望 {r['expected']}｜实测 {r['measured']}"
                  + (f"｜{r['note']}" if r["note"] else ""))
    print(f"\n  通过 {len(rows) - n_fail}｜失败 {n_fail}")
    print(f"报告：{csv_path}\n      {md_path}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
