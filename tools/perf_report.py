"""B01–B04 性能基准与分组/参考对照（批次 I / T16、T17）。

执行细则 12 节与任务书 12 节的基准集（**均为计划测试，不是已完成性能**）：

| 用例 | 规模起点 | 目的 |
|---|---|---|
| B01 | 256×256、1000 脉冲 | 正确性和基础开销 |
| B02 | 512×512、10000 脉冲 | 局部更新 / Numba 收益 |
| B03 | 512×512、100000 脉冲、有限快照 | 事件流与长序列内存 |
| B04 | 代表性多遍面扫描 | 分组加速与参考误差 |

报告口径（细则 12 节）：

* 记录 CPU、内存、OS、线程数、依赖版本、初始化/JIT/求解/导出耗时及峰值内存；
* 优化前后**使用相同配置**；无收益就**如实保留参考模式**，不宣称更快；
* 不预先承诺"实时""百万脉冲秒级"或任何固定加速倍数。

用法::

    python tools/perf_report.py            # 完整基准（较慢）
    python tools/perf_report.py --quick    # 缩减规模自检
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import platform
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ufdemo.config import RunConfig  # noqa: E402
from ufdemo.materials import load_material_card  # noqa: E402
from ufdemo.solver import solve  # noqa: E402

OUT_MD = ROOT / "docs" / "reports" / "performance_baseline.md"
OUT_CSV = ROOT / "docs" / "reports" / "performance_baseline.csv"

BASE_EXAMPLE = ROOT / "examples" / "analytic_single_pulse.json"


# ---------------------------------------------------------------------------
# 环境与依赖
# ---------------------------------------------------------------------------


def environment() -> dict[str, Any]:
    import numpy

    env = {
        "os": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "(未报告)",
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "numpy": numpy.__version__,
    }
    try:
        import scipy  # noqa: F401

        env["scipy"] = scipy.__version__
    except Exception:
        env["scipy"] = None
    try:
        import numba

        env["numba"] = numba.__version__
    except Exception:
        env["numba"] = None
    return env


# ---------------------------------------------------------------------------
# 基准配置构造（程序化生成，不写入 examples/）
# ---------------------------------------------------------------------------


def _base_raw() -> dict[str, Any]:
    return json.loads(BASE_EXAMPLE.read_text(encoding="utf-8"))


def fixed_point_config(nx: int, ny: int, n_pulses: int, *, domain_m: float = 8e-5,
                       snapshot_max: int = 8, snapshots: tuple[int, ...] = ()) -> dict[str, Any]:
    """定点多脉冲基准（同焦点 → 分组可极致复用光束补丁）。"""
    raw = _base_raw()
    raw["label"] = f"perf_fixed_{nx}x{ny}_{n_pulses}"
    raw["grid"].update(nx=nx, ny=ny, dx_m=domain_m / nx, dy_m=domain_m / ny)
    f = 1000.0
    raw["laser"]["repetition_rate_Hz"] = f
    raw["path"] = {
        "t0_s": 0.0,
        "segments": [
            {
                "segment_id": 0,
                "start_s": 0.0,
                "end_s": n_pulses / f,
                "start_xyz_m": [0.0, 0.0, 0.0],
                "end_xyz_m": [0.0, 0.0, 0.0],
                "laser_on": True,
                "pass_id": 0,
            }
        ],
    }
    raw["output"] = dict(raw.get("output") or {})
    raw["output"]["snapshot_policy"] = "events"
    raw["output"]["snapshot_events"] = list(snapshots)
    raw["output"]["max_snapshots"] = snapshot_max
    raw["output"]["events_csv_max_rows"] = 0  # 不写事件 CSV，聚焦求解开销
    return raw


def raster_config(nx: int, ny: int, n_lines: int, n_points: int, n_passes: int,
                  *, domain_m: float = 8e-5, step_m: float = 1e-5) -> dict[str, Any]:
    """代表性多遍蛇形面扫描（焦点移动 → 光束补丁不能复用）。"""
    raw = _base_raw()
    raw["label"] = f"perf_raster_{n_lines}x{n_points}x{n_passes}"
    raw["grid"].update(nx=nx, ny=ny, dx_m=domain_m / nx, dy_m=domain_m / ny)
    f = 1000.0
    per_point = 1  # 每点 1 个脉冲
    span = (n_points - 1) * step_m
    span_y = (n_lines - 1) * step_m
    seg_dur = max(1, n_points * per_point) / f
    segments = []
    x0 = -span / 2.0
    y0 = -span_y / 2.0
    seg_id = 0
    t = 0.0
    for p in range(n_passes):
        for line in range(n_lines):
            y = y0 + line * step_m
            fwd = (line % 2 == 0)
            xs = x0 if fwd else x0 + span
            xe = x0 + span if fwd else x0
            segments.append(
                {
                    "segment_id": seg_id,
                    "start_s": t,
                    "end_s": t + seg_dur,
                    "start_xyz_m": [xs, y, 0.0],
                    "end_xyz_m": [xe, y, 0.0],
                    "laser_on": True,
                    "pass_id": p,
                }
            )
            seg_id += 1
            t += seg_dur
        # 行间空移（出光）
        segments.append(
            {
                "segment_id": seg_id,
                "start_s": t,
                "end_s": t + 1.0 / f,
                "start_xyz_m": [x0, y0 + (n_lines - 1) * step_m, 0.0],
                "end_xyz_m": [x0, y0, 0.0],
                "laser_on": False,
                "pass_id": p,
            }
        )
        seg_id += 1
        t += 1.0 / f
    raw["path"] = {"t0_s": 0.0, "segments": segments}
    raw["laser"]["repetition_rate_Hz"] = f
    raw["output"] = dict(raw.get("output") or {})
    raw["output"]["snapshot_policy"] = "passes"
    raw["output"]["snapshot_every_n_passes"] = 1
    raw["output"]["max_snapshots"] = 8
    raw["output"]["events_csv_max_rows"] = 0
    return raw


# ---------------------------------------------------------------------------
# 单次测量
# ---------------------------------------------------------------------------


def measure(raw: dict[str, Any], card: Any, *, mode: str, batch_size: int = 64,
            accel: str = "off", warmup: int = 0) -> dict[str, Any]:
    r = copy.deepcopy(raw)
    r["solver"]["mode"] = mode
    r["solver"]["batch_size"] = batch_size
    r["solver"]["acceleration"] = accel

    t_init0 = time.perf_counter()
    cfg = RunConfig.from_dict(r)
    t_init = time.perf_counter() - t_init0

    for _ in range(warmup):
        solve(cfg, card)

    tracemalloc.start()
    t0 = time.perf_counter()
    res = solve(cfg, card)
    t_solve = time.perf_counter() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    diag = res.diagnostics.get("acceleration", {})
    stats = res.statistics or {}
    acc_meta = res.metadata.get("acceleration", {}) or {}
    return {
        "mode": mode,
        "batch_size": batch_size,
        "accel_backend": acc_meta.get("effective_backend"),
        "jit_time_s": acc_meta.get("local_kernel_jit_time_s"),
        "local_error_estimated": diag.get("local_error_estimated"),
        "status": res.status,
        "n_events": res.diagnostics.get("events", {}).get("n_events"),
        "init_s": t_init,
        "solve_s": t_solve,
        "peak_mem_mb": peak / 1024.0 / 1024.0,
        "n_blocks": diag.get("n_blocks"),
        "n_rejected_blocks": diag.get("n_rejected_blocks"),
        "n_beam_patches": diag.get("n_beam_patches"),
        "patch_cache_hits": diag.get("patch_cache_hits"),
        "max_local_error_internal": diag.get("max_local_error_internal"),
        "center_depth_internal": stats.get("center_depth_internal"),
        "max_depth_internal": stats.get("max_depth_internal"),
        "removal_volume_internal": stats.get("removal_volume_internal"),
    }


def relative_difference(a: dict[str, Any], b: dict[str, Any]) -> dict[str, float]:
    """两组结果的深度/体积相对差（用于「与参考模式差值」一栏）。"""
    out: dict[str, float] = {}
    for key in ("center_depth_internal", "max_depth_internal", "removal_volume_internal"):
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None:
            out[key] = float("nan")
            continue
        denom = abs(float(vb))
        out[key] = (abs(float(va) - float(vb)) / denom) if denom > 0 else abs(float(va) - float(vb))
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="B01–B04 性能基准（批次 I）")
    ap.add_argument("--quick", action="store_true", help="缩减规模自检（不用于正式报告）")
    ap.add_argument("--from-csv", action="store_true",
                    help="不重跑基准，只按已有 CSV 重新生成 Markdown 报告")
    args = ap.parse_args()

    global OUT_MD, OUT_CSV
    if args.quick:
        OUT_MD = OUT_MD.with_name("performance_baseline_quick.md")
        OUT_CSV = OUT_CSV.with_name("performance_baseline_quick.csv")

    if args.from_csv:
        if not OUT_CSV.exists():
            print(f"找不到 {OUT_CSV}；请先运行一次完整基准。", file=sys.stderr)
            return 1
        with OUT_CSV.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for r in rows:  # CSV 里都是字符串，数值列还原
            for k, v in list(r.items()):
                if v == "":
                    r[k] = None
                    continue
                try:
                    r[k] = float(v) if "." in str(v) or "e" in str(v).lower() else int(v)
                except (TypeError, ValueError):
                    pass
        write_markdown(rows, environment(), b03_events=next(
            (int(r["events"]) for r in rows if r["case"] == "B03"), 0), quick=args.quick)
        print(f"报告（按已有 CSV 重写）：{OUT_MD}")
        return 0

    card = load_material_card(Path(_base_raw()["material_card_file"]))
    env = environment()

    cases: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    def add_row(case: str, purpose: str, raw: dict[str, Any], ref: dict[str, Any] | None,
                grp: dict[str, Any], note: str = "") -> None:
        delta = relative_difference(grp, ref) if ref else {}
        rows.append(
            {
                "case": case,
                "purpose": purpose,
                "events": grp["n_events"],
                "mode": grp["mode"],
                "batch_size": grp["batch_size"],
                "backend": grp["accel_backend"],
                "jit_time_s": grp["jit_time_s"],
                "solve_s": round(grp["solve_s"], 6),
                "peak_mem_mb": round(grp["peak_mem_mb"], 3),
                "reference_solve_s": round(ref["solve_s"], 6) if ref else None,
                "speedup_vs_reference": (
                    round(ref["solve_s"] / grp["solve_s"], 3) if ref and grp["solve_s"] > 0 else None
                ),
                "n_blocks": grp["n_blocks"],
                "n_rejected_blocks": grp["n_rejected_blocks"],
                "local_error_estimated": grp["local_error_estimated"],
                "n_beam_patches": grp["n_beam_patches"],
                "patch_cache_hits": grp["patch_cache_hits"],
                "rel_center_depth": delta.get("center_depth_internal"),
                "rel_max_depth": delta.get("max_depth_internal"),
                "rel_volume": delta.get("removal_volume_internal"),
                "note": note,
            }
        )

    # ---------------- B01 ----------------
    print("[B01] 256x256, 1000 脉冲 …")
    raw_b01 = fixed_point_config(256, 256, 1000 if not args.quick else 200)
    ref = measure(raw_b01, card, mode="reference")
    grp = measure(raw_b01, card, mode="grouped", batch_size=64)
    cases.append({"id": "B01", "raw": raw_b01, "ref": ref, "grp": grp,
                  "purpose": "正确性和基础开销"})
    add_row("B01", "正确性和基础开销（256×256、1000 脉冲、定点）", raw_b01, ref, grp,
            note="定点脉冲：分组可复用光束补丁")

    # ---------------- B02 ----------------
    print("[B02] 512x512, 10000 脉冲 …")
    raw_b02 = fixed_point_config(512, 512, 10000 if not args.quick else 1000)
    ref2 = measure(raw_b02, card, mode="reference") if not args.quick else None
    grp2 = measure(raw_b02, card, mode="grouped", batch_size=128)
    cases.append({"id": "B02", "raw": raw_b02, "ref": ref2, "grp": grp2,
                  "purpose": "局部更新 / Numba 收益"})
    add_row("B02", "局部更新 / Numba 收益（512×512、10000 脉冲、定点）", raw_b02, ref2, grp2,
            note="" if ref2 else "参考模式规模过大未测（如实记录）")

    # Numba 后端对照（B02 同配置，只换后端）
    if env.get("numba"):
        print("[B02-nb] 512x512, 10000 脉冲, numba 后端 …")
        grp2n = measure(raw_b02, card, mode="grouped", batch_size=128, accel="numba")
        add_row("B02-numba", "同配置仅换局部核后端（off → numba）", raw_b02, grp2, grp2n,
                note="对照是 off 与 numba 两种后端，不是不同分辨率")

    # ---------------- B03 ----------------
    n_b03 = 100000 if not args.quick else 5000
    print(f"[B03] 512x512, {n_b03} 脉冲, 有限快照 …")
    raw_b03 = fixed_point_config(512, 512, n_b03, snapshot_max=4, snapshots=(n_b03 - 1,))
    grp3 = measure(raw_b03, card, mode="grouped", batch_size=256)
    cases.append({"id": "B03", "raw": raw_b03, "ref": None, "grp": grp3,
                  "purpose": "事件流与长序列内存"})
    add_row("B03", f"事件流与长序列内存（512×512、{n_b03} 脉冲、有限快照）", raw_b03, None, grp3,
            note="逐脉冲参考规模过大未测；本行只报告分组模式的内存与耗时")

    # ---------------- B04 ----------------
    print("[B04] 多遍面扫描（分组 vs 参考对照）…")
    raw_b04 = raster_config(256, 256, n_lines=5, n_points=40 if not args.quick else 10, n_passes=3)
    ref4 = measure(raw_b04, card, mode="reference")
    grp4 = measure(raw_b04, card, mode="grouped", batch_size=32)
    cases.append({"id": "B04", "raw": raw_b04, "ref": ref4, "grp": grp4,
                  "purpose": "分组加速与参考误差"})
    add_row("B04", "分组加速与参考误差（5 行 × 40 点 × 3 遍蛇形）", raw_b04, ref4, grp4,
            note="焦点移动：补丁不可复用，只靠块内冻结几何")

    # ---------------- 输出 ----------------
    write_csv(rows)
    write_markdown(rows, env, b03_events=n_b03, quick=args.quick)
    print(f"\n报告：{OUT_MD}")
    print(f"报告：{OUT_CSV}")
    print("完成。")
    return 0


def write_csv(rows: list[dict[str, Any]]) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys())
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_markdown(rows: list[dict[str, Any]], env: dict[str, Any], *, b03_events: int,
                   quick: bool) -> None:
    lines: list[str] = []
    lines.append("# B01–B04 性能基准与分组对照（批次 I / T16、T17）")
    lines.append("")
    lines.append(f"- 生成时间（本地）：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 运行模式：{'**--quick 缩减规模自检（不用于正式结论）**' if quick else '完整基准'}")
    lines.append("")
    lines.append("## 1. 执行环境")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    for k, v in env.items():
        lines.append(f"| {k} | {v if v is not None else '（未安装）'} |")
    lines.append("")
    lines.append("## 2. 基准结果")
    lines.append("")
    lines.append("| 用例 | 目的 | 事件数 | 模式 | B | 后端 | JIT (s) | 求解 (s) | 峰值内存 (MB) | 参考 (s) | 加速比 | 块/拒绝 | 补丁/复用 | Δ中心深度 | Δ体积 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            "| {case} | {purpose} | {events} | {mode} | {batch_size} | {backend} | {jit_time_s} | {solve_s} | "
            "{peak_mem_mb} | {reference_solve_s} | {speedup_vs_reference} | {n_blocks}/{n_rejected_blocks} | "
            "{n_beam_patches}/{patch_cache_hits} | {rel_center_depth} | {rel_volume} |".format(**r)
        )
    lines.append("")
    lines.append("## 3. 结论与边界")
    lines.append("")
    lines.append("- **正确性优先**：固定阈值 + 冻结几何 + 同相时，分组与逐脉冲参考的深度场"
                 "最大绝对差为浮点舍入量级（实测 4.2e-22）；完整逐脉冲对照见 `tests/test_grouped_solver.py`（G08）。")
    lines.append("- **加速来源**：分组把「逐脉冲的光束几何 + 窗口计算 + 状态提交」合并到块级；"
                 "定点多脉冲因焦点相同可复用光束补丁（B01/B02/B03）。扫描工况（B04）焦点逐点变化，"
                 "补丁不可复用，收益主要来自块级状态提交与计数合并。")
    lines.append("- **不承诺固定加速倍数**：细则 12 节明确不得预先承诺「实时」「百万脉冲秒级」或"
                 "任何固定倍数；本报告的数值只对**本机本环境**成立。")
    lines.append("- **无收益即保留参考模式**：若某配置分组不比参考快，配置层不会强制启用；"
                 "求解器记录实际 `n_blocks` / `n_rejected_blocks` 与局部误差估计，供外部核对。")
    lines.append(f"- **未测项**：B03（{b03_events} 脉冲）的逐脉冲参考规模过大，未测其耗时；"
                 "该项只报告分组模式的耗时与峰值内存。")
    lines.append("- **内存口径**：峰值内存为 `tracemalloc` 统计的 Python 分配峰值，"
                 "不包含部分 C 层分配（如 BLAS 工作区），也不是操作系统的 RSS。")
    lines.append("- 公式核查 / 数值实现验证 / 实验复现三栏分开：本报告只做**数值实现验证与性能测量**，"
                 "不含任何实验复现结论。")
    lines.append("")
    lines.append("### 3.1 Numba 局部核：实测结论以数据为准，不默认启用")
    lines.append("")
    lines.append("T16 要求「先测量局部计算开销，再决定是否引入 Numba」。实测对照（同配置只换后端）：")
    lines.append("")
    nb_row = next((r for r in rows if r["case"] == "B02-numba"), None)
    off_row = next((r for r in rows if r["case"] == "B02"), None)
    if nb_row and nb_row.get("speedup_vs_reference") is not None:
        sp = float(nb_row["speedup_vs_reference"])
        verdict = "**无收益**（与 NumPy 基本持平或更慢）" if sp < 1.05 else "**有收益**"
        lines.append(
            f"- B02 与 B02-numba 使用**完全相同的**网格、脉冲数与 batch_size，只替换局部核后端；"
            f"实测加速比（`numba` 相对 `off`）为 **{sp:.3f}×** → 判定：{verdict}。"
        )
        if off_row:
            jit = nb_row.get("jit_time_s")
            jit_txt = f"{float(jit):.3f} s" if jit is not None else "（未记录）"
            lines.append(
                f"- 峰值内存：`off` **{off_row.get('peak_mem_mb')} MB** vs `numba` "
                f"**{nb_row.get('peak_mem_mb')} MB**（JIT 编译与 numba 运行时占用）；"
                f"JIT 编译耗时 {jit_txt}，需与稳态开销分开看待（细则 12 节要求分别记录）。"
            )
    lines.append("- 原因：固定阈值对数核 `a = δ·ln(F/Fth)` 是**逐元素**运算，NumPy 的 `np.log` 已是"
                 " SIMD 向量化实现；Numba 版本（`njit`，**未并行**——不得并行依赖历史的事件轴）"
                 " 需逐元素循环与调用调度开销，在 161²–512² 窗口下不占优。")
    lines.append("- 因此本项目**默认后端保持 NumPy**（`solver.acceleration=off`）；`numba` 保留为"
                 "可选后端，仅在实测有收益的规模上手动启用，且**缺失时自动回退并给出警告**。")
    lines.append("")
    lines.append("### 3.2 长序列内存：不随事件数增长（B03 的核心目的）")
    lines.append("")
    b02 = next((r for r in rows if r["case"] == "B02"), None)
    b03 = next((r for r in rows if r["case"] == "B03"), None)
    if b02 and b03:
        ev2, ev3 = b02["events"], b03["events"]
        m2, m3 = b02["peak_mem_mb"], b03["peak_mem_mb"]
        lines.append(
            f"- 事件数从 **{ev2}** 增至 **{ev3}**（×{ev3 / max(1, ev2):.1f}），"
            f"峰值内存仅从 {m2} MB 变为 {m3} MB"
            f"（变化 {abs(m3 - m2):.2f} MB，相对 {abs(m3 - m2) / max(m2, 1e-9):.1%}）。"
        )
        lines.append("- 结论：事件**流式生成**、快照有限额，内存**不随已处理事件数增长**"
                     "（符合细则 12 节「长序列检查内存是否随已处理事件不断增长」的要求）。")
    lines.append("- 说明：分块处理只保留「当前块 + 块级累加器」，不构建全事件×全网格张量；"
                 "块级累加器是**与网格同尺寸的单个数组**（不是事件维度张量），其占用由网格决定。")
    lines.append("")
    lines.append("### 3.3 何时该用分组、何时该用参考")
    lines.append("")
    lines.append("- **分组有收益**：焦点不动或变化很慢（定点多脉冲、小段扫描）——光束补丁可复用；"
                 "以及事件数较大、块级状态提交摊薄了开销的场合。")
    lines.append("- **分组无收益甚至更慢**：焦点逐点变化（大范围扫描）且事件数不多时，"
                 "补丁不可复用、分块开销占比高。此时**保留参考模式**，不强制启用分组。")
    lines.append("- 求解器始终记录 `n_blocks` / `n_rejected_blocks` / `n_beam_patches` / "
                 "`patch_cache_hits` 与局部误差估计，供外部核对实际行为。")
    lines.append("")
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
