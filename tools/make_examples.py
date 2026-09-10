"""生成 ``examples/*.json`` 配置（批次 C 交付物：单坑 / 10 脉冲 / 直线 / 多遍栅格）。

生成而不是手写的原因：栅格与多遍的轨迹段必须逐段可核，手写出错概率高。
生成结果与脚本一起提交，任何改动都能通过重跑脚本复核。

用法::

    python tools/make_examples.py

约定（执行细则 2.3、5.3）：

* 人工解析 fixture 采用 SI + 正入射 + 固定几何解析基准；
* 每个活动段的时长是 ``1/f`` 的整数倍，事件落在段内且不跨段重复；
* 段区间左闭右开，路径终点不产生额外脉冲。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
FIXTURE = "tests/fixtures/analytic_fixture.json"
FIXTURE_ID = "analytic_fixture_not_a_material"

# G01 参数（执行细则 10 节）
FTH = 1.0e4           # J/m^2 = 1 J/cm^2
DELTA = 1.0e-7        # m = 100 nm
W0 = 1.0e-5           # m = 10 um
F0 = math.exp(2.0) * FTH
EP = F0 * math.pi * W0 * W0 / 2.0

UM = 1.0e-6


def base_config(*, label: str, grid: dict, laser: dict, path: dict, output: dict, notes: str = "") -> dict:
    return {
        "schema_version": "1.0",
        "label": label,
        "run_mode": "reference_case",
        "unit_system": "SI",
        "material_id": FIXTURE_ID,
        "material_card_file": FIXTURE,
        "seed": 20260910,
        "grid": grid,
        "laser": laser,
        "path": path,
        "solver": {
            "mode": "reference",
            "geometry_feedback": "fixed_geometry",
            "history_enabled": False,
            "tail_epsilon": 1e-08,
            "memory_budget_bytes": 2147483648,
            "budget_safety_factor": 1.5,
            "cancel_check_interval": 256,
            "acceleration": "off",
            "multiline_incubation": False,
            "structured_interface": False
        },
        "output": output,
        "notes": notes
    }


def laser_block(*, f_rep: float) -> dict:
    return {
        "wavelength_m": None,
        "pulse_duration_s": None,
        "pulse_energy_J": EP,
        "repetition_rate_Hz": f_rep,
        "spot_radius_m": W0,
        "focus_xyz_m": [0.0, 0.0, 0.0],
        "direction_unit": [0.0, 0.0, 1.0],
        "rayleigh_range_m": None,
        "m2": None,
        "power_measurement_location": "sample_surface",
        "parameter_sources": ["analytic_test_definition"]
    }


def seg(seg_id: int, pass_id: int, t0: float, t1: float, a, b, *, on: bool, label: str) -> dict:
    span = t1 - t0
    dist = math.dist(a, b)
    speed = dist / span if span > 0 else 0.0
    return {
        "segment_id": seg_id,
        "pass_id": pass_id,
        "start_s": t0,
        "end_s": t1,
        "start_xyz_m": list(a),
        "end_xyz_m": list(b),
        "speed_m_s": speed,
        "laser_on": on,
        "label": label
    }


def write(name: str, obj: dict) -> None:
    p = EXAMPLES / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("written", p.relative_to(ROOT))


def main() -> None:
    EXAMPLES.mkdir(parents=True, exist_ok=True)

    # --- 1. 单坑（G01）--------------------------------------------------
    f_rep = 1000.0
    write(
        "analytic_single_pulse.json",
        base_config(
            label="analytic_single_pulse",
            grid={"nx": 161, "ny": 161, "dx_m": W0 / 20, "dy_m": W0 / 20, "center_x_m": 0.0, "center_y_m": 0.0,
                  "origin": "cell_center", "initial_surface": "flat", "initial_height_m": 0.0},
            laser=laser_block(f_rep=f_rep),
            path={"t0_s": 0.0, "time_tolerance_s": 1e-12,
                  "segments": [seg(0, 0, 0.0, 1.0 / f_rep, (0, 0, 0), (0, 0, 0), on=True, label="single_pulse_point")]},
            output={
                "snapshot_policy": "events", "snapshot_events": [0], "max_snapshots": 8,
                "max_snapshot_bytes": 268435456,
                "cross_section": {"axis": "x", "offsets_m": [0.0]},
                "roi": [{"name": "center_10um", "radius_m": W0, "center_xy_m": [0.0, 0.0]}]
            },
            notes="G01：单脉冲解析基准。网格 dx=dy=w/20，覆盖 ±4w。"
        )
    )

    # --- 2. 定点 10 脉冲（G02）------------------------------------------
    write(
        "ten_pulses.json",
        base_config(
            label="analytic_ten_pulses",
            grid={"nx": 161, "ny": 161, "dx_m": W0 / 20, "dy_m": W0 / 20, "center_x_m": 0.0, "center_y_m": 0.0,
                  "origin": "cell_center", "initial_surface": "flat", "initial_height_m": 0.0},
            laser=laser_block(f_rep=f_rep),
            path={"t0_s": 0.0, "time_tolerance_s": 1e-12,
                  "segments": [seg(0, 0, 0.0, 10.0 / f_rep, (0, 0, 0), (0, 0, 0), on=True, label="ten_pulse_point")]},
            output={
                "snapshot_policy": "events", "snapshot_events": [0, 4], "max_snapshots": 8,
                "max_snapshot_bytes": 268435456,
                "cross_section": {"axis": "x", "offsets_m": [0.0]},
                "roi": [{"name": "center_10um", "radius_m": W0, "center_xy_m": [0.0, 0.0]}]
            },
            notes="G02：关闭离焦与历史的 10 个相同脉冲；要求线性叠加。"
        )
    )

    # --- 3. 直线扫描（G04）----------------------------------------------
    f_line = 1.0e5
    dt = 1.0 / f_line
    write(
        "line_scan.json",
        base_config(
            label="analytic_line_scan",
            grid={"nx": 131, "ny": 71, "dx_m": 1.0 * UM, "dy_m": 1.0 * UM, "center_x_m": 0.0, "center_y_m": 0.0,
                  "origin": "cell_center", "initial_surface": "flat", "initial_height_m": 0.0},
            laser=laser_block(f_rep=f_line),
            path={"t0_s": 0.0, "time_tolerance_s": 1e-12,
                  "segments": [seg(0, 0, 0.0, 7 * dt, (-30 * UM, 0, 0), (40 * UM, 0, 0), on=True, label="line_forward")]},
            output={
                "snapshot_policy": "events", "snapshot_events": [0, 3], "max_snapshots": 8,
                "max_snapshot_bytes": 268435456,
                "cross_section": {"axis": "x", "offsets_m": [0.0]},
                "roi": [{"name": "center_10um", "radius_m": W0, "center_xy_m": [0.0, 0.0]}]
            },
            notes="G04：v/f = 10 um 间距；段区间 [0, 7/f) 左闭右开，终点 x=+40 um 不产生脉冲。"
        )
    )

    # --- 4. 多遍栅格（批次 C 交付物）-------------------------------------
    f_r = 1.0e5
    dtr = 1.0 / f_r
    lines_y = [-40 * UM, -20 * UM, 0.0, 20 * UM, 40 * UM]
    xa, xb = -30 * UM, 40 * UM
    segments = []
    sid = 0
    t = 0.0
    for p in range(1, 4):
        for row, y in enumerate(lines_y):
            if row % 2 == 0:
                a, b = (xa, y, 0.0), (xb, y, 0.0)
            else:
                a, b = (xb, y, 0.0), (xa, y, 0.0)
            segments.append(seg(sid, p, t, t + 7 * dtr, a, b, on=True, label=f"scan_row{row}")); sid += 1; t += 7 * dtr
            if row < len(lines_y) - 1:
                y_next = lines_y[row + 1]
                if row % 2 == 0:
                    a, b = (xb, y, 0.0), (xb, y_next, 0.0)
                else:
                    a, b = (xa, y, 0.0), (xa, y_next, 0.0)
                segments.append(seg(sid, p, t, t + 7 * dtr, a, b, on=False, label=f"turn_to_row{row + 1}")); sid += 1; t += 7 * dtr
        if p < 3:
            segments.append(seg(sid, p, t, t + 7 * dtr, segments[-1]["end_xyz_m"], segments[-1]["end_xyz_m"], on=False, label="inter_pass_dwell"))
            sid += 1
            t += 7 * dtr

    write(
        "raster_multipass.json",
        base_config(
            label="analytic_raster_multipass",
            grid={"nx": 141, "ny": 161, "dx_m": 1.0 * UM, "dy_m": 1.0 * UM, "center_x_m": 0.0, "center_y_m": 0.0,
                  "origin": "cell_center", "initial_surface": "flat", "initial_height_m": 0.0},
            laser=laser_block(f_rep=f_r),
            path={"t0_s": 0.0, "time_tolerance_s": 1e-12, "segments": segments},
            output={
                "snapshot_policy": "passes", "snapshot_every_n_passes": 1, "max_snapshots": 8,
                "max_snapshot_bytes": 268435456,
                "cross_section": {"axis": "x", "offsets_m": [0.0, 20 * UM]},
                "roi": [
                    {"name": "center_10um", "radius_m": W0, "center_xy_m": [0.0, 0.0]},
                    {"name": "row20_y20um", "radius_m": W0, "center_xy_m": [0.0, 20 * UM]}
                ]
            },
            notes="多遍栅格：5 行 × 蛇形 × 3 遍；行距 20 um，转向与遍间停顿 laser_on=false，时钟连续不重置相位。"
        )
    )

    # --- 5. 合成演示（无量纲，显式选择）--------------------------------
    write(
        "synthetic_demo_point.json",
        {
            "schema_version": "1.0",
            "label": "synthetic_demo_point",
            "run_mode": "synthetic_demo",
            "unit_system": "dimensionless",
            "reference_scales": {"L_ref_m": 1.0e-5, "F_ref_J_m2": 1.0e4, "delta_ref_m": 1.0e-7},
            "material_id": "synthetic_demo_isotropic",
            "material_card_file": "data/materials/_synthetic_demo_isotropic.json",
            "seed": 1,
            "grid": {"nx": 121, "ny": 121, "dx_m": 1.0e-5 / 20, "dy_m": 1.0e-5 / 20,
                     "center_x_m": 0.0, "center_y_m": 0.0, "origin": "cell_center",
                     "initial_surface": "flat", "initial_height_m": 0.0},
            "laser": {
                "wavelength_m": None, "pulse_duration_s": None,
                "pulse_energy_J": math.exp(2.0) * 1.0e4 * math.pi * (1.0e-5) ** 2 / 2.0,
                "repetition_rate_Hz": 1000.0, "spot_radius_m": 1.0e-5,
                "focus_xyz_m": [0.0, 0.0, 0.0], "direction_unit": [0.0, 0.0, 1.0],
                "rayleigh_range_m": None, "m2": None,
                "power_measurement_location": "sample_surface",
                "parameter_sources": ["synthetic_definition"]
            },
            "path": {"t0_s": 0.0, "time_tolerance_s": 1e-12,
                     "segments": [seg(0, 0, 0.0, 5.0 / 1000.0, (0, 0, 0), (0, 0, 0), on=True, label="synthetic_point")]},
            "solver": {"mode": "reference", "geometry_feedback": "fixed_geometry", "history_enabled": False,
                       "tail_epsilon": 1e-08, "memory_budget_bytes": 1073741824,
                       "budget_safety_factor": 1.5, "cancel_check_interval": 256,
                       "acceleration": "off", "multiline_incubation": False, "structured_interface": False},
            "output": {"snapshot_policy": "events", "snapshot_events": [0], "max_snapshots": 4,
                       "max_snapshot_bytes": 134217728,
                       "cross_section": {"axis": "x", "offsets_m": [0.0]},
                       "roi": [{"name": "center_1Lref", "radius_m": 1.0e-5, "center_xy_m": [0.0, 0.0]}]},
            "notes": "合成演示：所有长度按 L_ref=10 um 归一、能流按 F_ref=1 J/cm^2 归一；禁止导出物理 um 深度。"
        }
    )


if __name__ == "__main__":
    main()
