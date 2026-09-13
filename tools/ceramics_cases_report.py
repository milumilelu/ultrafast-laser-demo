"""陶瓷/玻璃文献算例回放报告（U08）。

产出 ``docs/reports/ceramics_cases.{csv,md}``：4 组算例的实测值、条件、来源定位，
外加**措辞守卫**与**边界声明**。

不产生三维形貌；不把等效脉冲数写成脉冲数；不做「累计 ÷ N」。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ufdemo.cases import (  # noqa: E402
    EQUIVALENT_PULSE_LABEL,
    RECONSTRUCTION_LABEL,
    CaseReplay,
    assert_cases_are_replay_only,
)

MEASURED = ROOT / "data" / "measured"
REPORTS = ROOT / "docs" / "reports"


def main() -> int:
    replay = CaseReplay.from_dir(MEASURED)
    if not replay.cases:
        print(f"缺少 {MEASURED / 'ceramics_dot_line_measured.csv'}")
        return 2
    assert_cases_are_replay_only(replay.cases)

    REPORTS.mkdir(parents=True, exist_ok=True)
    csv_path = REPORTS / "ceramics_cases.csv"
    md_path = REPORTS / "ceramics_cases.md"

    rows = replay.summary_rows()
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    L = [
        "# 陶瓷 / 玻璃文献算例回放（U08）",
        "",
        f"- 数据：`data/measured/{replay.source_file}`（来源 D02，doi `10.3390/ma15103614`）",
        "- 条件：1030 nm、290 fs、10 kHz（原文 Table 1 / Table 2）",
        f"- 共 **{len(replay.cases)}** 组：两类材料 × （点坑 / 交叉线）",
        "",
        "> 本报告是**已知条件回放**：展示论文表格中的**实测条件与实测结果**，"
        "**不经过模型**。它不是任意工况的预测。",
        "",
        "## 1. 算例总表",
        "",
        "| 算例 | 材料 | 图形 | 峰值能流 (J/cm²) | 计数 | 深度 (μm) | 线宽 (μm) | 来源定位 |",
        "|---|---|---|---:|---|---:|---:|---|",
    ]
    for r in rows:
        cnt = (f"{r['equivalent_pulse_count']:g}（等效）" if r["equivalent_pulse_count"] != ""
               else (f"{r['pulse_count']:g}" if r["pulse_count"] != "" else "—"))
        L.append(f"| {r['case_id']} | {r['material']} | {r['pattern']} | "
                 f"{r['peak_fluence_J_cm2']:g} | {cnt} | {r['depth_um']:g} | "
                 f"{r['line_width_um'] if r['line_width_um'] != '' else '—'} | "
                 f"{r['source_locator']} |")

    L += [
        "",
        "## 2. ⚠️ 三条必须保留的边界声明",
        "",
        f"1. **等效脉冲数不是真实脉冲数**：交叉线的 `equivalent_pulse_count` 是按协议折算的"
        f"量（展示标签：`{EQUIVALENT_PULSE_LABEL}`），**不是**真实事件序列。"
        "**不得**把该工况的累计深度除以 N 后声明成「可用于任意扫描的脉冲核」"
        "——那会把一个已知条件的结果伪装成通用响应。",
        f"2. **没有实测三维形貌**：本批数据只有端点（坑深、线宽、直径），"
        f"因此只呈现散点与误差；任何由端点重建的图层**必须**标注「{RECONSTRUCTION_LABEL}」，"
        "且**不得**反向用于训练模型或计入实验数据。",
        "3. **回放 ≠ 预测**：算例来自论文表格，不经过本仓库的模型；"
        "模型对照若叠加，须与实测点分开呈现。",
        "",
        "## 3. 权限（与 U05 注册表一致）",
        "",
        "| 算例 | observation_access | increment_access |",
        "|---|---|---|",
    ]
    for r in rows:
        L.append(f"| {r['case_id']} | {r['observation_access']} | {r['increment_access']} |")
    L += [
        "",
        "全部算例 `increment_access = False`：它们报告的是**端点几何**，"
        "不是逐事件增量。要进主循环须走 U07 的 `TabulatedEventLaw` + 增量语义曲线。",
        "",
        "## 4. 复现",
        "",
        "```bash",
        "python tools/ceramics_cases_report.py",
        "```",
    ]
    md_path.write_text("\n".join(L) + "\n", encoding="utf-8")

    print(f"  算例 {len(replay.cases)} 组；全部 increment_access=False ✓")
    for r in rows:
        print(f"    {r['case_id']:18} {r['pattern']:13} d={r['depth_um']:g} μm  {r['source_locator']}")
    print(f"\n报告：{md_path}\n      {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
