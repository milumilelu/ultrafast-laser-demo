"""G04：路径与观测算子（执行细则 10 节、5.3 节 / 任务书 10 节）。

覆盖：

* 统一时钟 ``t_j = t0 + j/f``，段区间左闭右开，连接处不重复出光；
* ``v/f`` 位置间距；返程/转向出光显式关闭；
* 首个事件测试例（``f=1000 Hz``、段 ``[0,0.003 s)``、``v=1 m/s`` → 3 个事件）；
* 正反向扫描镜像等价；
* ROI 平均与全域平均分别正确；空 ROI 返回不可用。
"""

from __future__ import annotations

import copy
import math

import numpy as np
import pytest

from conftest import W0, load_example, make_config
from ufdemo.config import LaserConfig, PathConfig, UnitContext
from ufdemo.materials import load_material_card
from ufdemo.paths import count_events, iter_events
from ufdemo.solver import solve

pytestmark = pytest.mark.g04

UM = 1.0e-6
SI = UnitContext.from_config({"unit_system": "SI"})


def _laser(f_rep: float, energy: float = 1e-5) -> LaserConfig:
    return LaserConfig.from_dict(
        {
            "wavelength_m": None,
            "pulse_duration_s": None,
            "pulse_energy_J": energy,
            "repetition_rate_Hz": f_rep,
            "spot_radius_m": W0,
            "focus_xyz_m": [0.0, 0.0, 0.0],
            "direction_unit": [0.0, 0.0, 1.0],
        },
        SI,
    )


def _path(segments: list[dict], f_rep: float = 1000.0) -> PathConfig:
    return PathConfig.from_dict({"t0_s": 0.0, "segments": segments}, _laser(f_rep), SI)


# ---------------------------------------------------------------------------
# 统一时钟
# ---------------------------------------------------------------------------


def test_first_event_example_from_spec():
    """f=1000 Hz、段 [0,0.003 s)、v=1 m/s → t=0/0.001/0.002 s，位置间隔 1 mm。"""
    laser = _laser(1000.0)
    path = PathConfig.from_dict(
        {
            "t0_s": 0.0,
            "segments": [
                {"segment_id": 0, "pass_id": 0, "start_s": 0.0, "end_s": 0.003,
                 "start_xyz_m": [0.0, 0.0, 0.0], "end_xyz_m": [0.003, 0.0, 0.0], "laser_on": True}
            ],
        },
        laser,
        SI,
    )
    events = list(iter_events(path, laser))
    assert [e.time_s for e in events] == pytest.approx([0.0, 0.001, 0.002], abs=1e-15)
    xs = [e.focus_xyz_m[0] for e in events]
    assert xs == pytest.approx([0.0, 0.001, 0.002], rel=1e-12)
    assert [xs[i + 1] - xs[i] for i in range(len(xs) - 1)] == pytest.approx([0.001, 0.001], rel=1e-12)
    # 固定频率下整条路径终点不出额外脉冲
    assert len(events) == 3


def test_adjacent_segment_gets_boundary_event_without_duplication():
    """下一段从 0.003 s 开始且出光时，该时刻事件只由下一段获得。"""
    laser = _laser(1000.0)
    path = PathConfig.from_dict(
        {
            "t0_s": 0.0,
            "segments": [
                {"segment_id": 0, "pass_id": 0, "start_s": 0.0, "end_s": 0.003,
                 "start_xyz_m": [0.0, 0.0, 0.0], "end_xyz_m": [0.003, 0.0, 0.0], "laser_on": True},
                {"segment_id": 1, "pass_id": 1, "start_s": 0.003, "end_s": 0.006,
                 "start_xyz_m": [0.5, 0.0, 0.0], "end_xyz_m": [0.503, 0.0, 0.0], "laser_on": True},
            ],
        },
        laser,
        SI,
    )
    events = list(iter_events(path, laser))
    assert [e.time_s for e in events] == pytest.approx([0.0, 0.001, 0.002, 0.003, 0.004, 0.005], abs=1e-15)
    # 无重复时间戳
    assert len({round(e.time_s, 12) for e in events}) == len(events)
    # 0.003 s 的事件归属下一段，位置为下一段起点
    boundary = [e for e in events if abs(e.time_s - 0.003) < 1e-12][0]
    assert boundary.segment_id == 1
    assert boundary.focus_xyz_m[0] == pytest.approx(0.5, rel=1e-12)
    # 事件索引连续（只数真实出光事件）
    assert [e.index for e in events] == list(range(6))


def test_laser_off_keeps_clock_running_and_no_phase_reset():
    """关闭出光期间时钟继续；下一条扫描线不重置脉冲相位。"""
    laser = _laser(1000.0)
    path = PathConfig.from_dict(
        {
            "t0_s": 0.0,
            "segments": [
                {"segment_id": 0, "pass_id": 0, "start_s": 0.0, "end_s": 0.002,
                 "start_xyz_m": [0.0, 0.0, 0.0], "end_xyz_m": [0.002, 0.0, 0.0], "laser_on": True},
                {"segment_id": 1, "pass_id": 0, "start_s": 0.002, "end_s": 0.004,
                 "start_xyz_m": [0.002, 0.0, 0.0], "end_xyz_m": [0.002, 1e-6, 0.0], "laser_on": False},
                {"segment_id": 2, "pass_id": 1, "start_s": 0.004, "end_s": 0.006,
                 "start_xyz_m": [0.0, 1e-6, 0.0], "end_xyz_m": [0.002, 1e-6, 0.0], "laser_on": True},
            ],
        },
        laser,
        SI,
    )
    events = list(iter_events(path, laser))
    # 关闭出光期间没有事件，但时钟没有重置：段 2 的第一个事件在 0.004 s，而不是 0
    assert [e.time_s for e in events] == pytest.approx([0.0, 0.001, 0.004, 0.005], abs=1e-15)
    assert count_events(path, laser) == len(events) == 4


def test_no_extra_pulse_at_path_end():
    laser = _laser(1000.0)
    path = PathConfig.from_dict(
        {
            "segments": [
                {"segment_id": 0, "pass_id": 0, "start_s": 0.0, "end_s": 0.005,
                 "start_xyz_m": [0.0, 0.0, 0.0], "end_xyz_m": [0.005, 0.0, 0.0], "laser_on": True}
            ]
        },
        laser,
        SI,
    )
    assert count_events(path, laser) == 5  # t = 0..0.004，终点 0.005 不产生脉冲


# ---------------------------------------------------------------------------
# 直线扫描的 v/f 间距与镜像等价
# ---------------------------------------------------------------------------


def _run_line(reverse: bool = False, extend: bool = False):
    raw = load_example("line_scan.json")
    f = raw["laser"]["repetition_rate_Hz"]
    dt = 1.0 / f
    segs = list(raw["path"]["segments"])
    if reverse:
        a, b = segs[0]["start_xyz_m"], segs[0]["end_xyz_m"]
        segs[0]["start_xyz_m"], segs[0]["end_xyz_m"] = [30 * UM, 0.0, 0.0], [-40 * UM, 0.0, 0.0]
    if extend:
        segs.append({"segment_id": 1, "pass_id": 0, "start_s": 7 * dt, "end_s": 9 * dt,
                     "start_xyz_m": [40 * UM, 0.0, 0.0], "end_xyz_m": [60 * UM, 0.0, 0.0],
                     "laser_on": True, "label": "line_extended"})
    raw["path"]["segments"] = segs
    cfg = make_config(raw)
    return solve(cfg, load_material_card(raw["material_card_file"]))


def test_v_over_f_spacing():
    res = _run_line()
    xs = [float(r["focus_x_m"]) for r in res.events_rows]
    assert len(xs) == 7
    spacing = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    assert spacing == pytest.approx([10 * UM] * 6, rel=1e-9)


def test_forward_and_reverse_are_mirror_equivalent():
    fwd = _run_line(reverse=False)
    rev = _run_line(reverse=True)
    pos_f = sorted(round(float(r["focus_x_m"]), 12) for r in fwd.events_rows)
    pos_r = sorted(round(float(r["focus_x_m"]), 12) for r in rev.events_rows)
    assert pos_f == pos_r  # 对称工况下事件位置集合互为镜像且重合
    assert fwd.statistics["removal_volume_internal"] == pytest.approx(rev.statistics["removal_volume_internal"], rel=0)
    assert np.allclose(fwd.surface.depth, rev.surface.depth, rtol=1e-12, atol=1e-21)


def test_extending_track_does_not_change_middle_protocol():
    short = _run_line()
    long = _run_line(extend=True)
    assert len(long.events_rows) == 9
    for a, b in zip(short.events_rows, long.events_rows[:7]):
        assert float(a["time_s"]) == pytest.approx(float(b["time_s"]), abs=1e-15)
        assert float(a["focus_x_m"]) == pytest.approx(float(b["focus_x_m"]), rel=0)


# ---------------------------------------------------------------------------
# ROI 与全域统计
# ---------------------------------------------------------------------------


def test_roi_and_domain_statistics_are_separate_and_correct():
    res = _run_line()
    surface = res.surface
    depth = surface.depth
    ys, xs = surface.y, surface.x
    XX, YY = np.meshgrid(xs, ys)
    mask = (XX ** 2 + YY ** 2) <= W0 ** 2
    expect_mean = float(np.mean(depth[mask]))
    expect_area = int(np.count_nonzero(mask)) * surface.grid.dx_m * surface.grid.dy_m
    expect_max = float(np.max(depth[mask]))

    roi = [r for r in res.rois if r["name"] == "center_10um"][0]
    assert roi["available"] is True
    assert roi["mean_depth_internal"] == pytest.approx(expect_mean, rel=1e-12)
    assert roi["max_depth_internal"] == pytest.approx(expect_max, rel=1e-12)
    assert roi["actual_area_internal"] == pytest.approx(expect_area, rel=1e-12)
    assert "cell_centers_within_circle" in roi["rule"]

    domain_mean = res.statistics["mean_depth_internal"]
    domain_area = res.statistics["domain_area_internal"]
    assert domain_mean == pytest.approx(res.statistics["removal_volume_internal"] / domain_area, rel=1e-12)
    assert domain_mean < expect_mean  # 全域平均被未照射区域拉低，两者不混用


def test_empty_roi_returns_unavailable_not_zero():
    raw = load_example("line_scan.json")
    raw["output"]["roi"] = [{"name": "far_away", "radius_m": 0.4 * UM, "center_xy_m": [35.5 * UM, 15.5 * UM]}]
    cfg = make_config(raw)
    res = solve(cfg, load_material_card(raw["material_card_file"]))
    roi = res.rois[0]
    assert roi["available"] is False
    assert roi["mean_depth_internal"] is None
    assert roi["n_cells"] == 0
    assert "空 ROI" in roi["reason"]
