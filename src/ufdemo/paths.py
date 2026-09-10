"""轨迹段、统一时钟与事件迭代器（执行细则 5.3 节、任务书 6.2 节）。

统一整数时钟：``t_j = t0 + j/f``，在各轨迹段 ``[start, end)`` 查找位置。
关闭出光期间时钟继续，**不在下一条扫描线重置脉冲相位**。

事件按需生成（生成器），禁止建立 ``N_pulses x Nx x Ny`` 全量能流张量。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterator

from .config import LaserConfig, PathConfig, PathSegment
from .errors import CONFIG_INVALID, UFDemoError


@dataclass(frozen=True)
class PulseEvent:
    """任务书 6.2 的 ``PulseEvent``。"""

    index: int
    time_s: float
    focus_xyz_m: tuple[float, float, float]
    direction_unit: tuple[float, float, float]
    energy_J: float
    segment_id: int
    pass_id: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "time_s": self.time_s,
            "focus_x_m": self.focus_xyz_m[0],
            "focus_y_m": self.focus_xyz_m[1],
            "focus_z_m": self.focus_xyz_m[2],
            "direction_x": self.direction_unit[0],
            "direction_y": self.direction_unit[1],
            "direction_z": self.direction_unit[2],
            "energy_J": self.energy_J,
            "segment_id": self.segment_id,
            "pass_id": self.pass_id,
        }

    CSV_FIELDS = (
        "index",
        "time_s",
        "focus_x_m",
        "focus_y_m",
        "focus_z_m",
        "direction_x",
        "direction_y",
        "direction_z",
        "energy_J",
        "segment_id",
        "pass_id",
    )


def clock_index_bounds(path: PathConfig, laser: LaserConfig) -> tuple[int, int]:
    """返回 ``t_j = t0 + j/f`` 需要检查的整数时钟范围（闭区间）。

    时间边界容差集中在此定义（细则 5.3），不在不同路径类型各写一套舍入规则。
    """
    if laser.repetition_rate_Hz is None or not path.segments:
        return (0, -1)
    f = laser.repetition_rate_Hz
    t0 = path.t0_s
    t_end = max(s.end_s for s in path.segments)
    if t_end <= t0:
        return (0, -1)
    scale = max(1.0, abs((t_end - t0) * f))
    idx_tol = max(1e-9, 1e-12 * scale)
    j_end = int(math.floor((t_end - t0) * f + idx_tol))
    return (0, j_end)


def _segment_at(path: PathConfig, t: float) -> PathSegment | None:
    """区间统一为左闭右开，避免连接处重复打脉冲。"""
    tol = path.time_tolerance_s * max(1.0, abs(t))
    hit: PathSegment | None = None
    for s in path.segments:
        if s.end_s <= s.start_s:
            continue  # 空区间永不包含 t
        if (t >= s.start_s - tol) and (t < s.end_s - tol):
            if hit is not None:
                # 重叠段：报配置错误而不是静默取一个
                raise UFDemoError(
                    CONFIG_INVALID,
                    "轨迹段重叠，同一时刻落入多个段",
                    field_path="path.segments",
                    actual={"t_s": t, "segments": [hit.segment_id, s.segment_id]},
                    requirement="段区间 [start,end) 互不重叠",
                    suggestion="调整段边界；连接处按左闭右开归入下一段。",
                )
            hit = s
    return hit


def iter_events(path: PathConfig, laser: LaserConfig) -> Iterator[PulseEvent]:
    """按需产生真实物理脉冲事件。

    * 只有落在出光段内的事件才产生；出光关闭时时钟继续前进。
    * 计算域外的焦点照样产生事件（是否照射到表面由光束窗口判断）。
    * 段不存在时（例如整条路径终点）不产生额外脉冲。
    """
    if laser.repetition_rate_Hz is None:
        return
    f = laser.repetition_rate_Hz
    t0 = path.t0_s
    j_start, j_end = clock_index_bounds(path, laser)
    if j_end < j_start:
        return
    index = 0
    for j in range(j_start, j_end + 1):
        t = t0 + j / f
        seg = _segment_at(path, t)
        if seg is None or not seg.laser_on:
            continue
        pos = seg.position_at(t, path.time_tolerance_s)
        yield PulseEvent(
            index=index,
            time_s=t,
            focus_xyz_m=pos,
            direction_unit=laser.direction_unit,
            energy_J=laser.pulse_energy_J,
            segment_id=seg.segment_id,
            pass_id=seg.pass_id,
        )
        index += 1


def count_events(path: PathConfig, laser: LaserConfig) -> int:
    """与 :func:`iter_events` 完全一致的计数（用于预处理预估，不构造事件）。"""
    n = 0
    for _ in iter_events(path, laser):
        n += 1
    return n
