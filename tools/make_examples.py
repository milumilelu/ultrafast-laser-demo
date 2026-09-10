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

# 合成模式参考尺度（无量纲内部量：长度/L_ref、能流/F_ref）
L_REF_M = 1.0e-5     # m = 10 um
F_REF = 1.0e4        # J/m^2 = 1 J/cm^2
DELTA_REF_M = 1.0e-7  # m = 100 nm，仅用于 d/delta_ref 显示换算


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


def synthetic_base(*, label: str, seed: int, grid: dict, laser: dict, path: dict, output: dict,
                   structure: dict, notes: str = "",
                   material_id: str | None = None, material_card_file: str | None = None) -> dict:
    """无量纲合成配置骨架（任务书 4.2 节 / 166 行「合成模式的单位规则」）。

    所有长度除以 ``L_ref``、能流除以 ``F_ref`` 后显式给出；``delta_ref`` 只保留为
    显示换算基准，不参与几何。``structured_interface`` 必须与 ``structure`` 同时出现，
    不一致会在配置层被拒（不允许"配了结构却被静默忽略"）。
    """
    return {
        "schema_version": "1.0",
        "label": label,
        "run_mode": "synthetic_demo",
        "unit_system": "dimensionless",
        "reference_scales": {"L_ref_m": L_REF_M, "F_ref_J_m2": F_REF, "delta_ref_m": DELTA_REF_M},
        "material_id": material_id,
        "material_card_file": material_card_file,
        "seed": seed,
        "grid": grid,
        "laser": laser,
        "path": path,
        "solver": {
            "mode": "reference",
            "geometry_feedback": "fixed_geometry",
            "history_enabled": False,
            "tail_epsilon": 1e-08,
            "memory_budget_bytes": 1073741824,
            "budget_safety_factor": 1.5,
            "cancel_check_interval": 256,
            "acceleration": "off",
            "multiline_incubation": False,
            "structured_interface": True,
        },
        "output": output,
        "structure": structure,
        "notes": notes,
    }


def synthetic_laser(*, f_rep: float) -> dict:
    """峰值能流 = e^2 * F_ref 的合成光束（与 M0 合成演示同一约定）。"""
    return {
        "wavelength_m": None,
        "pulse_duration_s": None,
        "pulse_energy_J": math.exp(2.0) * F_REF * math.pi * L_REF_M ** 2 / 2.0,
        "repetition_rate_Hz": f_rep,
        "spot_radius_m": L_REF_M,
        "focus_xyz_m": [0.0, 0.0, 0.0],
        "direction_unit": [0.0, 0.0, 1.0],
        "rayleigh_range_m": None,
        "m2": None,
        "power_measurement_location": "sample_surface",
        "parameter_sources": ["synthetic_definition"],
    }


def synthetic_grid(*, nx: int, ny: int, dx_um: float) -> dict:
    return {
        "nx": nx,
        "ny": ny,
        "dx_m": dx_um * UM,
        "dy_m": dx_um * UM,
        "center_x_m": 0.0,
        "center_y_m": 0.0,
        "origin": "cell_center",
        "initial_surface": "flat",
        "initial_height_m": 0.0,
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

    # --- 6. 铝基 SiC 合成颗粒（批次 G / T12）-----------------------------
    # 颗粒尺寸与体积分数只借卡里 microstructure.synthetic_geometry_example 的
    # **几何**（S09，来自纳秒研究），不借它的阈值：两相响应一律内联合成定义。
    f_g = 1000.0
    dt_g = 1.0 / f_g
    rows_y = [-6 * UM, 0.0, 6 * UM]
    seg_g: list[dict] = []
    sg_id, tg = 0, 0.0
    for row, y in enumerate(rows_y):
        a, b = ((-7 * UM, y, 0.0), (7 * UM, y, 0.0)) if row % 2 == 0 else ((7 * UM, y, 0.0), (-7 * UM, y, 0.0))
        seg_g.append(seg(sg_id, 0, tg, tg + 7 * dt_g, a, b, on=True, label=f"particle_row{row}")); sg_id += 1; tg += 7 * dt_g
        if row < len(rows_y) - 1:
            y_next = rows_y[row + 1]
            x_turn = a[0]
            seg_g.append(seg(sg_id, 0, tg, tg + 2 * dt_g, (x_turn, y, 0.0), (x_turn, y_next, 0.0),
                             on=False, label=f"turn_to_row{row + 1}")); sg_id += 1; tg += 2 * dt_g

    write(
        "alsic_particle_composite.json",
        synthetic_base(
            label="synthetic_alsic_particle_composite",
            seed=20260910,
            material_id="alsic_sicp_aa2024_1030nm",
            material_card_file="data/materials/alsic_sicp_aa2024_1030nm.json",
            grid=synthetic_grid(nx=161, ny=161, dx_um=1.0),
            laser=synthetic_laser(f_rep=f_g),
            path={"t0_s": 0.0, "time_tolerance_s": 1e-12, "segments": seg_g},
            output={
                "snapshot_policy": "passes", "snapshot_every_n_passes": 1, "max_snapshots": 4,
                "max_snapshot_bytes": 268435456,
                "cross_section": {"axis": "x", "offsets_m": [0.0]},
                "roi": [
                    {"name": "center_1Lref", "radius_m": L_REF_M, "center_xy_m": [0.0, 0.0]},
                    {"name": "corner_off", "radius_m": L_REF_M, "center_xy_m": [70 * UM, 70 * UM]},
                ],
            },
            structure={
                "structure_type": "particle_composite",
                "seed": 20260910,
                # 目标体积分数是**生成参数**：0.45（卡里纳秒研究的几何）超出随机顺序放置
                # 的可达上限，这里取 0.32 保证颗粒能全部放下；实际值另行报告。
                "target_volume_fraction": 0.32,
                "phases": [
                    {
                        "name": "Al_matrix", "role": "matrix",
                        "threshold_over_F_ref": 1.00, "delta_over_L_ref": 0.50,
                        "depth_direction": "surface_normal",
                        "output_semantics": "event_depth_increment",
                        "response_source": "synthetic_definition",
                        "source_equation": "a = (delta/L_ref)*[ln(F/F_ref)]_+",
                        "note": "合成定义：铝基体相，未用任何单晶 SiC 或纳秒阈值。",
                    },
                    {
                        "name": "SiC_particle", "role": "particle",
                        "threshold_over_F_ref": 2.35, "delta_over_L_ref": 0.20,
                        "depth_direction": "surface_normal",
                        "output_semantics": "event_depth_increment",
                        "response_source": "synthetic_definition",
                        "source_equation": "a = (delta/L_ref)*[ln(F/F_ref)]_+",
                        "note": "合成定义：颗粒相；不得把块体 4H-SiC 卡当作该相标定。",
                    },
                ],
                "particles": {
                    "matrix_phase": "Al_matrix", "particle_phase": "SiC_particle",
                    "mean_diameter_m": 3.0e-5,          # = 3 L_ref，几何只取自卡中 S09 示例
                    "depth_m": 5.0e-5,                  # = 5 L_ref 颗粒层厚度
                    "min_gap_rel": 0.05,
                    "diameter_spread_rel": 0.25,
                    "max_placement_attempts": 40000,
                },
            },
            notes=(
                "批次 G / T12：铝基 SiC 合成颗粒。几何（颗粒直径 3 um）取自材料卡 "
                "microstructure 示例且只作几何；目标体积分数 0.32 是生成参数（卡里的 0.45 来自纳秒研究、"
                "超出随机顺序放置可达上限），实际体积分数由确定性采样单独报告。"
                "两相阈值/去除尺度一律内联合成定义，缺分相标定故不输出物理深度。"
                "路径为 3 行 × 7 脉冲（v/f = 2 um）以同时暴露基体与颗粒。"
            ),
        )
    )

    # --- 7. CFRP 合成铺层（批次 G / T13）---------------------------------
    # 铺层 [0/90/0/90]；树脂/纤维阈值不得等于卡上整体 Fth1(=0.84 F_ref)。
    f_l = 1000.0
    write(
        "cfrp_laminated_ply.json",
        synthetic_base(
            label="synthetic_cfrp_laminated_ply",
            seed=20260911,
            material_id="cfrp_t700_yb01_800nm",
            material_card_file="data/materials/cfrp_t700_yb01_800nm.json",
            grid=synthetic_grid(nx=141, ny=141, dx_um=1.0),
            laser=synthetic_laser(f_rep=f_l),
            path={
                "t0_s": 0.0, "time_tolerance_s": 1e-12,
                "segments": [seg(0, 0, 0.0, 16.0 / f_l, (0, 0, 0), (0, 0, 0), on=True, label="ply_drill_point")],
            },
            output={
                "snapshot_policy": "events", "snapshot_events": [0, 5, 10], "max_snapshots": 6,
                "max_snapshot_bytes": 268435456,
                "cross_section": {"axis": "x", "offsets_m": [0.0]},
                "roi": [{"name": "drill_1Lref", "radius_m": L_REF_M, "center_xy_m": [0.0, 0.0]}],
            },
            structure={
                "structure_type": "laminated_fiber_composite",
                "seed": 20260911,
                "phases": [
                    {
                        "name": "resin", "role": "matrix",
                        "threshold_over_F_ref": 0.70, "delta_over_L_ref": 0.35,
                        "depth_direction": "surface_normal",
                        "output_semantics": "event_depth_increment",
                        "response_source": "synthetic_definition",
                        "source_equation": "a = (delta/L_ref)*[ln(F/F_ref)]_+",
                        "note": "合成定义：树脂相。不得用卡上整体 Fth1=0.84 F_ref 充当该相阈值。",
                    },
                    {
                        "name": "fiber", "role": "fiber",
                        "threshold_over_F_ref": 2.10, "delta_over_L_ref": 0.15,
                        "depth_direction": "surface_normal",
                        "output_semantics": "event_depth_increment",
                        "response_source": "synthetic_definition",
                        "source_equation": "a = (delta/L_ref)*[ln(F/F_ref)]_+",
                        "note": "合成定义：碳纤维相；缺分相标定，不输出物理深度。",
                    },
                ],
                "layers": [
                    {"thickness_m": 2.0e-5, "matrix_phase": "resin", "fiber_phase": "fiber",
                     "fiber_angle_deg": 0.0, "fiber_volume_fraction": 0.55, "fiber_width_m": 4.0e-6},
                    {"thickness_m": 2.0e-5, "matrix_phase": "resin", "fiber_phase": "fiber",
                     "fiber_angle_deg": 90.0, "fiber_volume_fraction": 0.55, "fiber_width_m": 4.0e-6},
                    {"thickness_m": 2.0e-5, "matrix_phase": "resin", "fiber_phase": "fiber",
                     "fiber_angle_deg": 0.0, "fiber_volume_fraction": 0.55, "fiber_width_m": 4.0e-6},
                    {"thickness_m": 2.0e-5, "matrix_phase": "resin", "fiber_phase": "fiber",
                     "fiber_angle_deg": 90.0, "fiber_volume_fraction": 0.55, "fiber_width_m": 4.0e-6},
                ],
            },
            notes=(
                "批次 G / T13：CFRP 合成铺层 [0/90/0/90]，层厚 2 L_ref、纤维体积分数 55%。"
                "相响应为内联合成定义（树脂阈值 0.70 ≠ 卡上整体 Fth1=0.84 F_ref，避免把整体阈值拆给分相）。"
                "定点 16 脉冲用于逐层钻进，观察相标签切换与跨相截断。"
            ),
        )
    )


if __name__ == "__main__":
    main()
