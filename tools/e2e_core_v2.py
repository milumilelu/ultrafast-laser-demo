"""V2 核心链路端到端验证（C6）。

一次跑完五个环节，并**如实报告每一步的真实结果**（包括失败与"无可行方案"）：

    ① 上游轮廓载入（C2）       → 本仓库只有**虚拟**样例可用
    ② 实验 CSV 载入（C4）      → 上层 `数据/` 目录的真实表
    ③ 基线 Fth/δ（C3）         → 读取已反推的候选值
    ④ 标定增益 a（C4）         → 训练/留出**分开报**
    ⑤ h/N 目标筛选（C5）       → 无可行方案时如实说明原因
    ⑥ 导出路径与摘要

用法::

    PYTHONPATH=src python tools/e2e_core_v2.py
    PYTHONPATH=src python tools/e2e_core_v2.py --target-um 20 --tol-um 3

⚠️ 本脚本**不修改**任何原始数据或材料卡：基线参数通过内存覆盖生效。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

#: 上层目录里的真实实验表（不在 git 内）
EXP_DIR = ROOT.parent / "数据"
#: 虚拟上游样例（仅用于展示与端到端测试）
UPSTREAM_CASE = ROOT / "examples" / "upstream" / "synthetic_pit_demo" / "upstream_case.json"
#: C3 反推的候选基线
BASELINE = ROOT / "data" / "baselines" / "alsic_223fs_candidate.json"
#: 作基座的中性卡（**无 reference_protocol**，不会触发协议匹配拦截）
BASE_CARD = "tests/fixtures/analytic_fixture.json"


def _h(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def stage_1_upstream() -> dict:
    _h("① 上游轮廓（C2）")
    from ufdemo.upstream import load_upstream_case

    if not UPSTREAM_CASE.exists():
        print("  ✗ 虚拟样例不存在")
        return {"ok": False}
    prof, wnote = load_upstream_case(UPSTREAM_CASE)
    print(f"  case      : {prof.case_id}")
    print(f"  几何      : {prof.geometry}（{prof.x_axis} 轴）")
    print(f"  中心深度  : {prof.center_depth_um:.3f} μm")
    print(f"  半高全宽  : {prof.width_at_fraction_um(0.5):.3f} μm")
    print(f"  轮廓面积  : {prof.area_um2():.2f} μm²")
    print(f"  is_synthetic = {prof.is_synthetic}")
    if prof.is_synthetic:
        print("  ⚠️ **虚拟输入**：仅验证链路，不得据此得出上游对照结论")
    if wnote:
        print(f"  半径定义  : {wnote}")
    return {"ok": True, "case_id": prof.case_id, "is_synthetic": prof.is_synthetic,
            "center_depth_um": prof.center_depth_um}


def stage_2_csv() -> dict:
    _h("② 实验 CSV（C4 导入）")
    from ufdemo.calibration import load_experiment_csv

    p = EXP_DIR / "AlSiC.csv"
    if not p.exists():
        print(f"  ✗ 找不到 {p}")
        return {"ok": False}
    t = load_experiment_csv(p)
    print(f"  文件      : {p.name}（编码 {t.encoding}）")
    print(f"  行数      : 原始 {t.n_raw_rows} → 采用 {t.n_used}（跳过 {len(t.skipped_rows)}）")
    neg = [r.sample_id for r in t.rows if r.mean_depth_um < 0]
    print(f"  负均值行  : {len(neg)} 行 {neg[:6]}")
    r0 = t.rows[0]
    print(f"  首行      : τ={r0.pulse_duration_fs:g}fs f={r0.repetition_rate_kHz:g}kHz "
          f"h={r0.hatch_spacing_um:g}μm N={r0.pass_count} 深度={r0.mean_depth_um:g}μm")
    print("  间距列按 mm→μm ×1000 读取")
    return {"ok": True, "rows": t.rows, "n": t.n_used}


def stage_3_baseline() -> dict:
    _h("③ 基线 Fth/δ（C3 反推候选）")
    if not BASELINE.exists():
        print("  ✗ 候选基线不存在（先跑 identify_baseline）")
        return {"ok": False}
    d = json.loads(BASELINE.read_text(encoding="utf-8"))
    print(f"  材料/脉宽 : {d['materialFamily']} / {d['pulseDurationFs']:g} fs")
    print(f"  Fth       : {d['thresholdJcm2']:.3f} J/cm²")
    print(f"  δ         : {d['deltaNm']:.1f} nm")
    print(f"  训练/留出 : {d['nTrain']} / {d['nHoldout']} 行")
    print(f"  训练中位相对误差 : {d['trainMedianRelErr']:.1%}")
    if d.get("holdoutMedianRelErr") is not None:
        print(f"  留出中位相对误差 : {d['holdoutMedianRelErr']:.1%}")
    print(f"  证据状态  : {d['evidenceStatus']}")
    print("  ⚠️ 工程有效参数（对同批实验拟合），**不是**独立实测常数")
    return {"ok": True, "baseline": d}


def stage_4_calibrate(rows: list, bl: dict) -> dict:
    _h("④ 标定增益 a（C4）")
    from ufdemo.calibration import (
        PredictionSpec, calibrate_gain, group_split,
    )

    override = {
        "kind": "log_fixed", "output_semantics": "event_depth_increment",
        "fluence_basis": "incident_peak_fluence", "depth_direction": "surface_normal",
        "threshold_J_m2": bl["thresholdJm2"], "delta_m": bl["deltaM"],
    }
    tau = float(bl["pulseDurationFs"])
    sub = [r for r in rows if abs(r.pulse_duration_fs - tau) < 1e-9 and r.mean_depth_um > 0]
    if len(sub) < 4:
        print(f"  ✗ 该脉宽可用行不足（{len(sub)}）")
        return {"ok": False, "override": override}
    tr, ho, info = group_split(sub, holdout_groups=max(1, len(sub) // 5))
    print(f"  训练 {len(tr)} / 留出 {len(ho)} 行（跨侧泄漏组：{info['leakage_groups'] or '无'}）")
    spec = PredictionSpec(material_card_file=BASE_CARD, window_um=20.0, dx_um=0.5,
                          response_override=override)
    t0 = time.perf_counter()
    res = calibrate_gain(tr, spec=spec, holdout_rows=ho, bounds=(0.05, 20.0),
                         residual_scale_um=5.0, material_id=f"{bl['materialFamily']}_{tau:g}fs")
    dt = time.perf_counter() - t0
    m = res.metrics()
    print(f"  用时 {dt:.1f}s  →  **a = {res.gain:.4f}**")
    print(f"  训练 MAE  : {m['train']['mae_before_um']:.3f} → {m['train']['mae_after_um']:.3f} μm")
    if m["holdout"]["n"]:
        print(f"  留出 MAE  : {m['holdout']['mae_before_um']:.3f} → {m['holdout']['mae_after_um']:.3f} μm")
        worse = (m["holdout"]["mae_after_um"] or 0) > (m["holdout"]["mae_before_um"] or 0)
        if worse:
            print("  ⚠️ **留出变差 → 过拟合**：训练集变好不代表模型更可信。")
            print("     样本量小 + 基线本身误差大时，单参数 a 无法同时满足所有工况。")
    return {"ok": True, "gain": res.gain, "metrics": m, "override": override,
            "train": len(tr), "holdout": len(ho)}


def stage_5_plan(bl: dict, override: dict, target_um: float, tol_um: float) -> dict:
    _h("⑤ h/N 目标筛选（C5）")
    from ufdemo.planning import plan_for_target

    t0 = time.perf_counter()
    res = plan_for_target(
        material_card_file=BASE_CARD,
        target_depth_um=target_um, tolerance_um=tol_um,
        pulse_duration_fs=float(bl["pulseDurationFs"]),
        repetition_rate_kHz=2.0, scan_speed_mm_s=50.0,
        region_um=(40.0, 40.0), dx_um=0.5,
        response_override=override,
    )
    dt = time.perf_counter() - t0
    print(f"  目标 {target_um:g}±{tol_um:g} μm；候选 {len(res.candidates)} 个，"
          f"可行 {len(res.feasible_candidates)} 个（{dt:.1f}s）")
    if res.recommended:
        r = res.recommended
        print(f"  ★ 推荐：h={r.spacing_um:g} μm, N={r.pass_count} → {r.mean_depth_um:.2f} μm，"
              f"理想时间 {r.ideal_time_s:.4f} s")
        print(f"    覆盖 {r.coverage_fraction:.1%}  过切 {r.over_depth_fraction:.1%}  "
              f"深度标准差 {r.depth_std_um:.2f} μm")
    else:
        print(f"  ✗ 无可行方案：{res.infeasible_reason}")
    bad = Counter(c.status for c in res.candidates if not c.feasible)
    if bad:
        print(f"  被否原因分布：{dict(bad)}")
        worst = [c for c in res.candidates if c.mean_depth_um is not None]
        if worst:
            cov = [c.coverage_fraction for c in worst if c.coverage_fraction is not None]
            if cov:
                print(f"  覆盖率范围：{min(cov):.1%} – {max(cov):.1%}")
    print("  ⚠️ 覆盖率/过切/均匀性是**模型预测**；未导入实测高度图时**不构成**二维形貌验证")
    return {"ok": True, "plan": res}


def stage_6_export(res) -> dict:
    _h("⑥ 导出")
    out_dir = ROOT / "runs" / "e2e_core_v2"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = out_dir / "summary.json"
    summary.write_text(json.dumps(
        {"targetPlan": res.to_dict() if res else None},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  摘要：{summary}")
    if res and res.recommended:
        csv = out_dir / "path.csv"
        res.recommended.plan.write_csv(csv)
        print(f"  路径：{csv}（中立路径，非机床控制器代码）")
        print(f"       分段 {len(res.recommended.plan.segments)} 段，"
              f"焦点 Z 恒为 {res.recommended.plan.focus_z_m}")
    else:
        print("  无推荐方案 → 不导出路径（**不给一个假的最优解**）")
    return {"ok": True, "out_dir": str(out_dir)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-um", type=float, default=20.0)
    ap.add_argument("--tol-um", type=float, default=3.0)
    args = ap.parse_args()

    print("V2 核心链路端到端验证")
    print(f"目标 {args.target_um:g}±{args.tol_um:g} μm")

    s1 = stage_1_upstream()
    s2 = stage_2_csv()
    s3 = stage_3_baseline()
    if not (s2.get("ok") and s3.get("ok")):
        print("\n前置环节未就绪，后续跳过。")
        return 2
    s4 = stage_4_calibrate(s2["rows"], s3["baseline"])
    if not s4.get("ok"):
        return 2
    s5 = stage_5_plan(s3["baseline"], s4["override"], args.target_um, args.tol_um)
    stage_6_export(s5.get("plan"))

    _h("结论")
    print(f"  ① 上游载入      : {'✅' if s1.get('ok') else '✗'}"
          f"{'（虚拟输入）' if s1.get('is_synthetic') else ''}")
    print(f"  ② CSV 导入      : {'✅' if s2.get('ok') else '✗'}（{s2.get('n')} 行）")
    print(f"  ③ 基线          : {'✅' if s3.get('ok') else '✗'}")
    print(f"  ④ 标定          : {'✅' if s4.get('ok') else '✗'}（a={s4.get('gain'):.4f}）"
          if s4.get("ok") else "  ④ 标定          : ✗")
    plan = s5.get("plan")
    ok_plan = bool(plan and plan.recommended)
    print(f"  ⑤ 规划          : {'✅ 有推荐方案' if ok_plan else '⚠️ 无可行方案（如实返回）'}")
    print()
    print("  **链路是通的**；每一步的数值结论请分别看待，不要合并成一句「验证通过」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
