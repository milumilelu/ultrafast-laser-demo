"""路径规划：弓字形路径与 h/N 枚举（C5）。

任务书 §6 的硬要求（都写进代码）：

* **正向计算与导出使用同一个 `PathPlan`** —— 不是"算一套、导一套"。
* 必需字段：每段起终点、扫描速度、遍次、出光状态、段类型、时间。
* **禁止为了贴合边界用 linspace 静默改变请求间距** ——
  区域宽度不能被间距整除时，如实报告"需要 N+1 条扫描线、末条偏移量为 x"，
  而不是把间距偷偷拉伸到刚好铺满。
* 焦点 Z 恒为初始表面（`focus_strategy = fixed_original_surface`），
  不为每层做 Z 调整。
* 设备路径策略（换向减速、额外边框、关光能力）**固定在项目设置**，
  不确知的**不虚构**；转向速度未知时只给理想扫描时间并标"边界未校准"。

本模块只负责"生成路径 + 枚举候选"，**不自己算物理**：
每个候选都交给同一套已标定的逐脉冲求解器评估。
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .config import CONFIG_INVALID
from .errors import UFDemoError

#: 段类型。``scan`` = 出光扫描；``turn`` = 换向空走（不出光）。
SEG_SCAN = "scan"
SEG_TURN = "turn"

#: 默认的工程搜索网格（任务书 §6）。**不表示**这些组合都有实测。
DEFAULT_SPACINGS_UM: tuple[float, ...] = (2.0, 4.0, 6.0, 8.0, 10.0)
DEFAULT_PASS_COUNTS: tuple[int, ...] = (1, 2, 3, 4, 5)

#: 设备路径策略（任务书 §6：固定在项目设置，不给用户一排开关）。
DEVICE_PATH_POLICY: Mapping[str, Any] = {
    "corner_deceleration": "not_modeled_in_ideal_time",
    "extra_border": "not_known_do_not_invent",
    "shutter_off_capability": "not_assumed",
    "note": (
        "换向减速与额外边框**未确知**：时间只给理想扫描时间，并标注边界未校准；"
        "导出不依赖设备不存在的关光能力。"
    ),
}


@dataclass(frozen=True)
class PathSegment:
    """一段路径。坐标单位 m，时间单位 s。"""

    kind: str
    start_xyz_m: tuple[float, float, float]
    end_xyz_m: tuple[float, float, float]
    speed_m_s: float
    pass_index: int
    emitting: bool
    duration_s: float

    @property
    def length_m(self) -> float:
        dx = self.end_xyz_m[0] - self.start_xyz_m[0]
        dy = self.end_xyz_m[1] - self.start_xyz_m[1]
        dz = self.end_xyz_m[2] - self.start_xyz_m[2]
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "startXYZ_m": list(self.start_xyz_m),
            "endXYZ_m": list(self.end_xyz_m),
            "speed_m_s": self.speed_m_s,
            "passIndex": self.pass_index,
            "emitting": self.emitting,
            "duration_s": self.duration_s,
        }


@dataclass
class PathPlan:
    """一份路径规划。**正向计算与导出都用这一个对象**。

    ``spacing_requested_um`` 是用户请求值；``spacing_effective_um`` 是实际相邻
    扫描线的间距。**两者不相等时必须在报告里说明**（不得用 linspace 把间距
    悄悄改成"刚好铺满"）。
    """

    segments: list[PathSegment]
    region_um: tuple[float, float]
    spacing_requested_um: float
    pass_count: int
    focus_z_m: float
    scan_speed_mm_s: float
    spacing_effective_um: float | None = None
    n_scan_lines: int = 0
    notes: tuple[str, ...] = ()
    device_policy: Mapping[str, Any] = field(default_factory=lambda: dict(DEVICE_PATH_POLICY))

    # -- 时间 ----------------------------------------------------------------
    @property
    def ideal_scan_time_s(self) -> float:
        """理想扫描时间：所有段的时长之和（含换向空走，按同速计）。

        ⚠️ **不含**换向减速与额外边框 —— 这两者未确知，不得假装算准。
        """
        return float(sum(s.duration_s for s in self.segments))

    @property
    def time_is_calibrated(self) -> bool:
        """总时长是否可信。未知换向减速时为 ``False``（界面须标"边界未校准"）。"""
        return bool(DEVICE_PATH_POLICY["corner_deceleration"] != "not_modeled_in_ideal_time")

    # -- 导出 ----------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "ufdemo.path_plan/1",
            "regionUm": list(self.region_um),
            "spacingRequestedUm": self.spacing_requested_um,
            "spacingEffectiveUm": self.spacing_effective_um,
            "nScanLines": self.n_scan_lines,
            "passCount": self.pass_count,
            "focusZM": self.focus_z_m,
            "focusStrategy": "fixed_original_surface",
            "scanSpeedMmS": self.scan_speed_mm_s,
            "idealScanTimeS": self.ideal_scan_time_s,
            "timeCalibrated": self.time_is_calibrated,
            "devicePolicy": dict(self.device_policy),
            "notes": list(self.notes),
            "segments": [s.to_dict() for s in self.segments],
        }

    def to_segments_config(self, *, t0_s: float = 0.0) -> list[dict[str, Any]]:
        """转成 ``RunConfig.path.segments`` 的格式（累计时间轴）。

        **正向计算与导出走同一个 ``PathPlan``** —— 这个方法就是"同一份路径
        喂给求解器"的桥，避免出现"算一套、导一套"的两份路径。
        """
        out: list[dict[str, Any]] = []
        t = float(t0_s)
        for i, s in enumerate(self.segments):
            t_end = t + s.duration_s
            out.append({
                "segment_id": i,
                "pass_id": s.pass_index,
                "start_s": t,
                "end_s": t_end,
                "start_xyz_m": list(s.start_xyz_m),
                "end_xyz_m": list(s.end_xyz_m),
                "speed_m_s": s.speed_m_s,
                "laser_on": bool(s.emitting),
                "label": s.kind,
            })
            t = t_end
        return out

    def write_csv(self, path: str | Path) -> Path:
        """导出中立路径 CSV。

        **不冒称**这是"已通过机床验证的控制器代码"—— 文件头写明是中立路径。
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(["# ufdemo 中立路径（非机床控制器代码；焦点 Z 恒定）"])
            w.writerow(["# 理想扫描时间只含同速空走，不含换向减速与额外边框"])
            w.writerow([
                "index", "kind", "pass_index", "emitting",
                "x0_m", "y0_m", "z0_m", "x1_m", "y1_m", "z1_m",
                "speed_m_s", "duration_s",
            ])
            for i, s in enumerate(self.segments):
                w.writerow([
                    i, s.kind, s.pass_index, int(s.emitting),
                    repr(s.start_xyz_m[0]), repr(s.start_xyz_m[1]), repr(s.start_xyz_m[2]),
                    repr(s.end_xyz_m[0]), repr(s.end_xyz_m[1]), repr(s.end_xyz_m[2]),
                    repr(s.speed_m_s), repr(s.duration_s),
                ])
        return p


def serpentine_plan(
    *,
    region_um: tuple[float, float],
    spacing_um: float,
    pass_count: int,
    scan_speed_mm_s: float,
    focus_z_m: float = 0.0,
    y_start_um: float | None = None,
) -> PathPlan:
    """生成矩形区域的**弓字形**（serpentine）路径。

    扫描线沿 ±X 交替，沿 Y 逐条推进，逐遍重复；每遍的行进方向一致
    （同向多遍，不做反向），遍与遍之间用一条不出光的换向段连接。

    关于间距：本函数**严格按请求间距**布点。最后一行的位置由
    ``floor`` 决定，因此区域末端可能留一条**小于间距的余量** ——
    这个余量会如实记录在 ``notes`` 里，**不**通过拉伸间距来消除。
    """
    if spacing_um <= 0 or not math.isfinite(spacing_um):
        raise UFDemoError(
            CONFIG_INVALID, "间距必须是正有限数",
            field_path="planning.spacing_um", actual=spacing_um, requirement="> 0",
        )
    if pass_count < 1 or int(pass_count) != pass_count:
        raise UFDemoError(
            CONFIG_INVALID, "遍数必须是 ≥1 的整数",
            field_path="planning.pass_count", actual=pass_count, requirement="整数 ≥ 1",
        )
    if scan_speed_mm_s <= 0 or not math.isfinite(scan_speed_mm_s):
        raise UFDemoError(
            CONFIG_INVALID, "扫描速度必须是正有限数",
            field_path="planning.scan_speed_mm_s", actual=scan_speed_mm_s, requirement="> 0",
        )
    wx_um, wy_um = float(region_um[0]), float(region_um[1])
    if wx_um <= 0 or wy_um <= 0:
        raise UFDemoError(
            CONFIG_INVALID, "区域尺寸必须为正",
            field_path="planning.region_um", actual=list(region_um), requirement="两维均 > 0",
        )

    speed_m_s = float(scan_speed_mm_s) * 1e-3
    x0, x1 = -wx_um / 2.0 * 1e-6, wx_um / 2.0 * 1e-6
    y_base = (0.0 if y_start_um is None else float(y_start_um) * 1e-6) - wy_um / 2.0 * 1e-6
    spacing_m = float(spacing_um) * 1e-6

    # 扫描线条数：起点 + 每 spacing 一条；**不把间距拉伸到刚好铺满**
    n_lines = int(math.floor(wy_um / spacing_um)) + 1
    n_lines = max(1, n_lines)
    ys = [y_base + i * spacing_m for i in range(n_lines)]
    covered_um = (n_lines - 1) * spacing_um
    leftover_um = wy_um - covered_um

    segs: list[PathSegment] = []
    for pi in range(int(pass_count)):
        for li, y in enumerate(ys):
            left_to_right = (li % 2 == 0)
            sx, ex = (x0, x1) if left_to_right else (x1, x0)
            segs.append(PathSegment(
                kind=SEG_SCAN,
                start_xyz_m=(sx, y, focus_z_m),
                end_xyz_m=(ex, y, focus_z_m),
                speed_m_s=speed_m_s,
                pass_index=pi,
                emitting=True,
                duration_s=abs(x1 - x0) / speed_m_s,
            ))
            # 换向段：连到下一条线（不出光）。**不假设设备有关光能力之外的加速性能**
            if li < n_lines - 1:
                y_next = ys[li + 1]
                segs.append(PathSegment(
                    kind=SEG_TURN,
                    start_xyz_m=(ex, y, focus_z_m),
                    end_xyz_m=(ex, y_next, focus_z_m),
                    speed_m_s=speed_m_s,
                    pass_index=pi,
                    emitting=False,
                    duration_s=abs(y_next - y) / speed_m_s,
                ))
        # 遍间换向：回到本遍起点一侧（不出光）
        if pi < int(pass_count) - 1:
            y_last = ys[-1]
            segs.append(PathSegment(
                kind=SEG_TURN,
                start_xyz_m=(segs[-1].end_xyz_m[0], y_last, focus_z_m),
                end_xyz_m=(x0 if n_lines % 2 == 1 else x1,
                           y_base, focus_z_m),
                speed_m_s=speed_m_s,
                pass_index=pi,
                emitting=False,
                duration_s=math.hypot(
                    (x0 if n_lines % 2 == 1 else x1) - segs[-1].end_xyz_m[0],
                    y_last - y_base,
                ) / speed_m_s,
            ))

    notes = [
        f"扫描线 {n_lines} 条，按请求间距 {spacing_um:g} μm 布点；"
        f"末端余量 {leftover_um:.3f} μm（**未**通过拉伸间距消除）。",
        "焦点 Z 恒定（fixed_original_surface）：遍次增加不改变 Z。",
        DEVICE_PATH_POLICY["note"],
    ]
    if abs(leftover_um) > 1e-9:
        notes.append(
            f"区域 Y 向 {wy_um:g} μm 不能被间距 {spacing_um:g} μm 整除："
            f"最后一条之后余 {leftover_um:.3f} μm。这是**如实报告**，不是错误。"
        )

    return PathPlan(
        segments=segs,
        region_um=(wx_um, wy_um),
        spacing_requested_um=float(spacing_um),
        pass_count=int(pass_count),
        focus_z_m=float(focus_z_m),
        scan_speed_mm_s=float(scan_speed_mm_s),
        spacing_effective_um=float(spacing_um),
        n_scan_lines=n_lines,
        notes=tuple(notes),
    )


def enumerate_candidates(
    *,
    spacings_um: Iterable[float] = DEFAULT_SPACINGS_UM,
    pass_counts: Iterable[int] = DEFAULT_PASS_COUNTS,
) -> list[tuple[float, int]]:
    """枚举 (h, N) 候选网格。

    ⚠️ 这是**工程搜索网格**，不表示指定工况的这些组合都已实测。
    """
    return [(float(h), int(n)) for h in spacings_um for n in pass_counts]
