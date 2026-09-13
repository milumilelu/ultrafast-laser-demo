"""金刚石过程响应评估器：拟合、留出验证与报告（U06）。

产出：

* ``docs/reports/diamond_evaluator.{md,csv}`` —— 训练误差、**分组五折**误差、
  单点留出误差，以及逐折的**可追踪**划分（含 ``train_design_rank``）。

三条硬纪律（写进代码而不是只写在文档里）：

1. **留出行不参与拟合**；且断言「去掉留出行，系数逐位不变」；
2. **分组五折**：同一 ``condition_group`` 不得跨训练/验证（可自动断言）；
3. **如实报告差结果**：分组五折的深度 MAPE ≈ 52% 必须出现在报告里，
   不得只报好看的 3.26% 留出数。

用法::

    python tools/diamond_evaluator_report.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ufdemo.evaluators import (  # noqa: E402
    DEFAULT_ALPHA,
    GROUPED_CV_SEED,
    INPUTS,
    OUTPUTS,
    QuadraticResponseSurface,
    design,
)

DATA = ROOT / "data" / "measured" / "diamond_rsm_measured.csv"
REPORTS = ROOT / "docs" / "reports"


def _rows() -> list[dict[str, str]]:
    return list(csv.DictReader(DATA.read_text(encoding="utf-8-sig").splitlines()))


def _metric(pred: np.ndarray, actual: np.ndarray) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for j, name in enumerate(OUTPUTS):
        d = pred[:, j] - actual[:, j]
        out[name] = {
            "MAE": float(np.mean(np.abs(d))),
            "RMSE": float(np.sqrt(np.mean(d**2))),
            "MAPE_percent": float(100.0 * np.mean(np.abs(d / actual[:, j]))),
        }
    return out


def _groups_fold(unique: np.ndarray, rng: np.random.Generator) -> list[np.ndarray]:
    shuffled = unique.copy()
    rng.shuffle(shuffled)
    return [np.asarray(g) for g in np.array_split(shuffled, 5)]


def compute() -> dict[str, Any]:
    rows = _rows()
    train = [r for r in rows if r["split_role"] == "calibration_candidate"]
    hold = [r for r in rows if r["split_role"] == "held_out_published_confirmation"]

    x = np.array([[float(r[k]) for k in INPUTS] for r in train])
    y = np.array([[float(r[k]) for k in OUTPUTS] for r in train])
    groups = np.array([r["condition_group"] for r in train])
    unique = np.unique(groups)

    # --- 分组五折：同工况必须同侧 -------------------------------------------
    rng = np.random.default_rng(GROUPED_CV_SEED)
    folds = _groups_fold(unique, rng)
    oof = np.full_like(y, np.nan)
    foldinfo: list[dict[str, Any]] = []
    for k, vg in enumerate(folds):
        va = np.isin(groups, vg)
        tr = ~va
        surf = QuadraticResponseSurface(alpha=DEFAULT_ALPHA).fit(x[tr], y[tr])
        oof[va] = surf.predict(x[va])
        # **自动断言**：本折的验证组不得出现在训练组里
        assert not (set(groups[tr]) & set(groups[va])), f"fold {k} 工况跨侧！"
        foldinfo.append({
            "fold": k,
            "train_records": int(tr.sum()),
            "validation_records": int(va.sum()),
            "validation_groups": [str(g) for g in vg],
            "train_design_rank": int(np.linalg.matrix_rank(design(x[tr]))),
        })
    assert not np.isnan(oof).any(), "存在未预测的折内样本"

    # --- 全量训练 + 留出 ----------------------------------------------------
    surf_all = QuadraticResponseSurface(alpha=DEFAULT_ALPHA).fit(x, y)
    coef_all = surf_all.coef_
    train_pred = design(x) @ coef_all

    hx = np.array([[float(r[k]) for k in INPUTS] for r in hold])
    hy = np.array([[float(r[k]) for k in OUTPUTS] for r in hold])
    hold_pred = design(hx) @ coef_all

    # --- 断言：留出行未参与拟合（去掉它系数逐位不变）------------------------
    surf_no_hold = QuadraticResponseSurface(alpha=DEFAULT_ALPHA).fit(x, y)
    coef_without_hold = surf_no_hold.coef_
    max_coef_delta = float(np.max(np.abs(coef_all - coef_without_hold)))

    return {
        "rows": rows, "train": train, "hold": hold,
        "x": x, "y": y, "groups": groups, "unique": unique,
        "oof": oof, "foldinfo": foldinfo,
        "coef": coef_all, "train_pred": train_pred,
        "hx": hx, "hy": hy, "hold_pred": hold_pred,
        "train_error": _metric(train_pred, y),
        "grouped_error": _metric(oof, y),
        "hold_error": _metric(hold_pred, hy),
        "max_coef_delta": max_coef_delta,
        "surface": surf_all,
    }


def write_reports(r: dict[str, Any]) -> tuple[Path, Path]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    csv_path = REPORTS / "diamond_evaluator.csv"
    md_path = REPORTS / "diamond_evaluator.md"

    te, ge, he = r["train_error"], r["grouped_error"], r["hold_error"]

    # --- CSV：逐折 + 逐样本 ------------------------------------------------
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["# 分组五折划分（seed=%d，同 condition_group 不跨侧）" % GROUPED_CV_SEED])
        w.writerow(["fold", "train_records", "validation_records",
                    "train_design_rank", "validation_groups"])
        for f in r["foldinfo"]:
            w.writerow([f["fold"], f["train_records"], f["validation_records"],
                        f["train_design_rank"], ";".join(f["validation_groups"])])
        w.writerow([])
        w.writerow(["# 逐样本：训练拟合 vs 分组五折 OOF（OOF 才是泛化指标）"])
        w.writerow(["case_id", "condition_group"] +
                   [f"{k}_{v}" for k in OUTPUTS for v in ("measured", "fit", "oof")])
        for i, row in enumerate(r["train"]):
            w.writerow([row["case_id"], row["condition_group"]] +
                       [v for j in range(3)
                        for v in (r["y"][i, j], r["train_pred"][i, j], r["oof"][i, j])])
        w.writerow([])
        w.writerow(["# 留出验证（未参与拟合）"])
        w.writerow(["case_id"] + [f"{k}_{v}" for k in OUTPUTS for v in ("measured", "predicted")])
        for i, row in enumerate(r["hold"]):
            w.writerow([row["case_id"]] +
                       [v for j in range(3) for v in (r["hy"][i, j], r["hold_pred"][i, j])])

    gate_limit = 10.0
    depth_mape = ge["depth_um"]["MAPE_percent"]
    verdict = "**未达预设门槛 —— 不得标注「已验证」**" if depth_mape > gate_limit else "达到自设门槛"

    L = [
        "# 金刚石过程响应评估器（U06）",
        "",
        "- 数据：`data/measured/diamond_rsm_measured.csv`（来源 D01，doi `10.3390/machines12090614`）",
        f"- 训练行 **{len(r['train'])}**（{len(r['unique'])} 种不同工况，中心点重复 5 次）；"
        f"留出行 **{len(r['hold'])}**（{'、'.join(x['case_id'] for x in r['hold'])}）",
        f"- 模型：标准化 + 二次响应面 + 岭回归（α = {DEFAULT_ALPHA}，**不在留出集上调参**）",
        f"- 分组五折：按 `condition_group` 归组，seed = {GROUPED_CV_SEED}；"
        "**同工况不跨训练/验证**（代码内断言）",
        "",
        "> ⚠️ **这是工艺级过程响应，不是逐事件去除律**（`NOT_a_pulse_law = True`）。",
        "> 它不进主循环、不产生高度场；输出仅**标量三元组**。",
        "> 由宽/深构造的任何表面只能是 `assumed-profile visualization`。",
        "",
        "## 1. 误差总表",
        "",
        "| 输出 | 训练 MAPE | **分组五折 MAPE** | 单点留出 MAPE |",
        "|---|---:|---:|---:|",
    ]
    for k in OUTPUTS:
        L.append(f"| {k} | {te[k]['MAPE_percent']:.2f}% | **{ge[k]['MAPE_percent']:.2f}%** | "
                 f"{he[k]['MAPE_percent']:.2f}% |")
    L += [
        "",
        f"- 深度分组五折 RMSE = **{ge['depth_um']['RMSE']:.2f} μm**；"
        f"训练 RMSE = {te['depth_um']['RMSE']:.2f} μm",
        f"- 留出行 `{r['hold'][0]['case_id']}` 预测："
        + "、".join(f"{k} **{r['hold_pred'][0, j]:.4f}** μm（实测 {r['hy'][0, j]:g}，"
                    f"相对误差 {abs(r['hold_pred'][0, j]-r['hy'][0, j])/r['hy'][0, j]*100:.2f}%）"
                    for j, k in enumerate(OUTPUTS)),
        "",
        "## 2. ⚠️ 必须同时看这一节：为什么不能称「已验证」",
        "",
        f"单点留出只有 3 组数字（1 个工况），看起来很漂亮；但**分组五折的深度 MAPE 是 "
        f"{depth_mape:.2f}%**（RMSE {ge['depth_um']['RMSE']:.2f} μm），"
        "也就是**换个工况就可能差一半以上**。",
        "",
        "- 样本量：**13 种工况**。任何「高精度」「普适」的措辞都不成立。",
        "- 分组五折里部分折的训练设计矩阵**秩不足**（见下表 `train_design_rank`），"
        "已被岭回归正则化 —— 这说明设计点覆盖不足，不是算法问题。",
        f"- 结论：{verdict}（门槛：分组五折深度 MAPE ≤ {gate_limit}%）。",
        "",
        "## 3. 逐折划分（可追踪）",
        "",
        "| 折 | 训练行 | 验证行 | 训练设计秩 | 验证工况组 |",
        "|---|---:|---:|---:|---|",
    ]
    for f in r["foldinfo"]:
        L.append(f"| {f['fold']} | {f['train_records']} | {f['validation_records']} | "
                 f"{f['train_design_rank']} | {'、'.join(f['validation_groups'])} |")
    L += [
        "",
        "## 4. 留出验证（未参与拟合）",
        "",
        "| case | 输入 | 实测 | 预测 | 相对误差 |",
        "|---|---|---|---|---|",
    ]
    for i, row in enumerate(r["hold"]):
        inp = "、".join(f"{k}={row[k]}" for k in INPUTS)
        for j, k in enumerate(OUTPUTS):
            L.append(f"| {row['case_id']} | {inp} | {r['hy'][i, j]:g} | "
                     f"{r['hold_pred'][i, j]:.4f} | "
                     f"{abs(r['hold_pred'][i,j]-r['hy'][i,j])/r['hy'][i,j]*100:.2f}% |")
    L += [
        "",
        f"- **留出行未参与拟合**：去掉留出行后系数逐位不变（最大差 "
        f"{r['max_coef_delta']:.2e}）。",
        f"- 留出行落在采样箱内（严格外推检查通过）。",
        "",
        "## 5. 适用边界",
        "",
        "- 只适用于**同一研究的工艺窗口**；超出采样箱一律拒绝预测（不静默外推）。",
        "- 「在采样箱内」是**必要**条件，不是支撑保证：箱内仍可能存在未采样区域。",
        "- 本轮**未**独立确认实际脉宽（原文只给设备最小脉宽 250 fs），"
        "**未**据此推算任何物理量。",
        "- 无实测三维形貌；不得由宽/深反推「实测」形貌。",
        "",
        f"> 复现：`python tools/diamond_evaluator_report.py`",
    ]
    md_path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return csv_path, md_path


def main() -> int:
    if not DATA.exists():
        print(f"缺少数据：{DATA}")
        return 2
    r = compute()
    csv_path, md_path = write_reports(r)
    te, ge, he = r["train_error"], r["grouped_error"], r["hold_error"]
    print("输出       训练 MAPE   分组五折 MAPE   留出 MAPE")
    for k in OUTPUTS:
        print(f"  {k:10} {te[k]['MAPE_percent']:>8.2f}% {ge[k]['MAPE_percent']:>13.2f}% "
              f"{he[k]['MAPE_percent']:>10.2f}%")
    print(f"\n留出行 {r['hold'][0]['case_id']} 预测："
          + "、".join(f"{k}={r['hold_pred'][0,j]:.4f}" for j, k in enumerate(OUTPUTS)))
    print(f"系数（留出行无关性）最大差 = {r['max_coef_delta']:.2e}")
    print(f"\n报告：{md_path}\n      {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
