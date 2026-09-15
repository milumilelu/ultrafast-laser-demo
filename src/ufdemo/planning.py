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

    @property
    def layer_count(self) -> int:
        """层数 N（= 旧的 pass_count）。每层是一次完整弓字形扫描。"""
        return int(self.pass_count)

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
            "layerCount": self.pass_count,     # N = 层数
            "passCount": self.pass_count,      # 旧名，保留兼容
            "machinedShape": "rectangular_pocket",
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
    layer_count: int | None = None,
    pass_count: int | None = None,      # 旧名（= 层数），保留兼容

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
    # N 是**层数**：每层是一次完整的弓字形扫描。旧名 pass_count 等价，保留兼容。
    pass_count = layer_count if layer_count is not None else pass_count
    if pass_count is None:
        raise UFDemoError(
            CONFIG_INVALID, "缺少层数 N",
            field_path="planning.layer_count", actual=None, requirement="整数 ≥ 1",
        )
    if pass_count < 1 or int(pass_count) != pass_count:
        raise UFDemoError(
            CONFIG_INVALID, "层数 N 必须是 ≥1 的整数",
            field_path="planning.layer_count",
            actual=pass_count, requirement="整数 ≥ 1",
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


# ===========================================================================
# C5：h/N 枚举与目标筛选
# ===========================================================================
#
# 任务书 §6 的硬要求：
# * **每个候选都必须调用同一套已标定求解器**（不另建模型、不用代理近似）；
# * 模拟与导出用**同一个 PathPlan**；
# * 间距选择**不能只看平均深度** —— 还要看覆盖、漏加工、过切；
# * **无可行组合时返回「当前范围内无可行方案」**，不硬给一个"最优"；
# * 质量信息分两类：**标量深度误差**（有实验证据）
#   vs **二维覆盖/几何均匀性**（模型预测，未导入实测高度图不得声称已验证）。

def _violation(c: "CandidateResult", *, target_um: float, tol_um: float) -> float:
    """**违规程度**（0 = 完全满足）。用于"无可行方案时给一个最接近的"。

    这不是"放宽判据"，而是在**没有可行解时**仍然给一个可诊断的参考 ——
    报告里必须显示它**不满足约束**，不得被读成推荐。
    """
    if c.mean_depth_um is None:
        return float("inf")
    v = 0.0
    lo, hi = target_um - tol_um, target_um + tol_um
    if c.mean_depth_um < lo:
        v += (lo - c.mean_depth_um) / max(tol_um, 1e-9)
    elif c.mean_depth_um > hi:
        v += (c.mean_depth_um - hi) / max(tol_um, 1e-9)
    if c.coverage_fraction is not None and c.coverage_fraction < 0.98:
        v += (0.98 - c.coverage_fraction) / 0.98
    if c.over_depth_fraction is not None and c.over_depth_fraction > 0.05:
        v += (c.over_depth_fraction - 0.05) / 0.95
    return v


def _rank_key(c: "CandidateResult") -> tuple[float, float]:
    """候选排序键：**先看理想时间，再看深度均匀性**。

    抽成函数而不是在两处各写一遍 —— 写两处必然会把「推荐」和「表格顺序」
    弄成不一致（测试抓到过：推荐是排序后第一个，表格却是原始顺序）。
    """
    return (
        float("inf") if c.ideal_time_s is None else c.ideal_time_s,
        float("inf") if c.depth_std_um is None else c.depth_std_um,
    )


#: 候选不通过的原因码（报告直接显示，便于解释"为什么没有方案"）。
CAND_OK = "ok"
CAND_TOO_SHALLOW = "too_shallow"
CAND_TOO_DEEP = "too_deep"
CAND_UNDERCOVERED = "undercovered"
CAND_OVERCUT = "overcut"
CAND_SOLVE_FAILED = "solve_failed"


def _policy_notes(policy: Any, skipped: Any) -> list[str]:
    """窗口半径策略的**口径说明**（只在启用时给出，默认关闭不产生任何噪音）。"""
    out: list[str] = []
    if str(policy or "") == "above_threshold":
        frac = f"{float(skipped):.1%}" if isinstance(skipped, (int, float)) else "未知"
        out.append(
            "窗口半径策略 above_threshold（超阈值开窗）：只计算能流可能超过响应阈值的区域，"
            f"被裁掉的窗口格比例 {frac}。这些格子对去除量的贡献**恰好为 0**（不影响深度），"
            "但 cumulative_fluence / illumination_count 等**剂量观测量**只在该半径内统计 —— "
            "口径与既有不一致，见 fluence_ledger.threshold_window。"
        )
    return out


@dataclass
class CandidateResult:
    """一个 (h, N) 候选的评估结果。

    ⚠️ ``mean_depth_um`` 等是**模型预测**；只有与实验对照过的标量深度误差
    才有实验证据。覆盖/均匀性是模型预测，**不是**实测二维形貌验证。
    """

    spacing_um: float
    pass_count: int
    plan: PathPlan
    status: str = CAND_OK
    mean_depth_um: float | None = None
    max_depth_um: float | None = None
    ideal_time_s: float | None = None
    coverage_fraction: float | None = None      # 被至少一个脉冲照到的格比例
    under_depth_fraction: float | None = None   # 浅于 目标-容差 的比例（漏加工）
    over_depth_fraction: float | None = None    # 深于 目标+容差 的比例（过切）
    depth_std_um: float | None = None           # 几何均匀性（模型预测）
    #: **矩形槽形貌**（下采样深度图 + 中心截面）。只在需要时填充（默认 None），
    #: 否则 25 个候选各带一张图会让响应体爆炸。
    surface: Mapping[str, Any] | None = None
    #: 仿真域与加工区（μm）。两者不同才有外围余量。
    domain_um: float | None = None
    machining_region_um: float | None = None
    #: 加工区**外围**的最大去除深度 —— 应接近 0，用来确认"路径没跑出加工区"。
    margin_max_depth_um: float | None = None
    notes: tuple[str, ...] = ()
    #: 本次求解**实际生效**的窗口半径策略（性能开关，见 ADR-0017）。
    #: ``tail_epsilon`` = 既有 ε 尾部截断；``above_threshold`` = 超阈值开窗。
    window_radius_policy: str | None = None
    #: 被超阈值半径裁掉的**窗口格比例**（仅 above_threshold 时非 None）。
    #: 注意：启用后 ``cumulative_fluence`` / ``illumination_count`` 等**剂量观测量**
    #: 的统计范围随之缩小 —— 去除量不变，但口径变了，必须随结果一起上报。
    window_cells_skipped_fraction: float | None = None

    @property
    def layer_count(self) -> int:
        """层数 N（= 旧的 ``pass_count``）。"""
        return int(self.pass_count)

    @property
    def feasible(self) -> bool:
        return self.status == CAND_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "spacingUm": self.spacing_um,
            "layerCount": self.pass_count,
            "passCount": self.pass_count,     # 旧名，保留兼容
            "machinedShape": "rectangular_pocket",
            "status": self.status,
            "feasible": self.feasible,
            "meanDepthUm": self.mean_depth_um,
            "maxDepthUm": self.max_depth_um,
            "idealTimeS": self.ideal_time_s,
            "coverageFraction": self.coverage_fraction,
            "underDepthFraction": self.under_depth_fraction,
            "overDepthFraction": self.over_depth_fraction,
            "depthStdUm": self.depth_std_um,
            "surface": dict(self.surface) if self.surface else None,
            "domainUm": self.domain_um,
            "machiningRegionUm": self.machining_region_um,
            "marginMaxDepthUm": self.margin_max_depth_um,
            "notes": list(self.notes),
            "windowRadiusPolicy": self.window_radius_policy,
            "windowCellsSkippedFraction": self.window_cells_skipped_fraction,
        }


def evaluate_candidate(
    spacing_um: float,
    pass_count: int,
    *,
    material_card_file: str,
    region_um: tuple[float, float],
    dx_um: float,
    domain_um: tuple[float, float] | None = None,
    pulse_duration_fs: float,
    repetition_rate_kHz: float,
    scan_speed_mm_s: float,
    target_depth_um: float,
    tolerance_um: float,
    gain: float = 1.0,
    response_override: Mapping[str, Any] | None = None,
    min_coverage: float = 0.98,
    max_overcut_fraction: float = 0.05,
    want_surface: bool = False,
    max_surface_cells: int = 96,
    bg: Any | None = None,
    window_radius_policy: str = "tail_epsilon",
    window_threshold_margin: float = 1.25,
) -> CandidateResult:
    """评估一个 (h, N)：生成路径 → **同一套求解器** → 统计 → 判可行。

    复用 ``calibration.predict_mean_depth`` 的装配路径，保证"规划用的求解器"
    与"标定用的求解器"是**同一个**（不另建一套）。
    """
    from .calibration import ExperimentRow, PredictionSpec, build_row_config

    plan = serpentine_plan(
        region_um=region_um, spacing_um=float(spacing_um),
        pass_count=int(pass_count), scan_speed_mm_s=float(scan_speed_mm_s),
    )
    # **仿真域**可以与**加工区**不同：域更大时外围留出未加工余量。
    # 统计必须只算加工区 —— 否则外围的 0 深度会被当成"漏加工"，把指标算歪。
    dom = domain_um or region_um
    row = ExperimentRow(
        sample_id=f"h{spacing_um:g}-N{pass_count}",
        pulse_duration_fs=float(pulse_duration_fs),
        repetition_rate_kHz=float(repetition_rate_kHz),
        scan_speed_mm_s=float(scan_speed_mm_s),
        hatch_spacing_um=float(spacing_um),
        pass_count=int(pass_count),
        mean_depth_um=0.0,
    )
    spec = PredictionSpec(
        material_card_file=material_card_file,
        window_um=float(dom[0]), dx_um=float(dx_um),
        machining_region_um=float(region_um[0]),
        response_override=response_override,
    )
    from .materials import MaterialSpec, load_material_card
    from .solver import solve

    cfg = build_row_config(row, spec=spec, gain=gain, bg=bg)
    # --- 窗口半径策略（性能开关，默认关闭；ADR-0017）------------------------
    # ``above_threshold`` 只计算「能流可能超过响应阈值」的区域。阈值型核在
    # ``F <= F_th`` 处返回**恰好 0**，所以**去除量逐位不变**（有测试守着）；
    # 但剂量观测量的统计范围会缩小 —— 实际策略与裁掉比例都随结果上报，不许静默。
    cfg.solver.window_radius_policy = str(window_radius_policy)
    cfg.solver.window_threshold_margin = float(window_threshold_margin)
    card_path = Path(cfg.material_card_file)
    if response_override:
        import json as _json

        raw = _json.loads(card_path.read_text(encoding="utf-8"))
        raw["response"] = {**(raw.get("response") or {}), **dict(response_override)}
        material = MaterialSpec.from_dict(raw)
    else:
        material = load_material_card(card_path)

    res = solve(cfg, material)
    _ledger = (res.diagnostics or {}).get("fluence_ledger") or {}
    _tw = _ledger.get("threshold_window") or {}
    applied_policy = _ledger.get("window_radius_policy") or str(window_radius_policy)
    skipped_fraction = _tw.get("cells_skipped_fraction")
    if res.status != "completed" or res.surface is None:
        return CandidateResult(
            spacing_um=float(spacing_um), pass_count=int(pass_count), plan=plan,
            status=CAND_SOLVE_FAILED,
            notes=tuple(f"求解未完成：{res.status}"),
            window_radius_policy=applied_policy,
            window_cells_skipped_fraction=skipped_fraction,
        )

    import numpy as np

    drop_full = res.surface.initial_height - res.surface.height
    # 取**加工区**子块做统计（域更大时外围是未加工余量，不该进指标）
    g0 = res.surface.grid
    n_reg = int(round(float(region_um[0]) * 1e-6 / g0.dx_m))
    n_reg = max(1, min(n_reg, drop_full.shape[0], drop_full.shape[1]))
    yy0 = (drop_full.shape[0] - n_reg) // 2
    xx0 = (drop_full.shape[1] - n_reg) // 2
    drop = drop_full[yy0:yy0 + n_reg, xx0:xx0 + n_reg]
    margin_max_um = 0.0
    if n_reg < drop_full.shape[0]:
        _mask = np.ones(drop_full.shape, dtype=bool)
        _mask[yy0:yy0 + n_reg, xx0:xx0 + n_reg] = False
        margin_max_um = float(drop_full[_mask].max()) * 1e6 if np.any(_mask) else 0.0
    touched = drop > 0.0

    # --- 矩形槽形貌（按需）--------------------------------------------------
    # 弓字形填充整个矩形 → 结果是一个**矩形槽**，不是单个圆坑。
    # 只有需要展示时才带图（25 个候选全带会让响应体爆掉）。
    surf: dict[str, Any] | None = None
    if want_surface:
        ny, nx = drop.shape
        stride = max(1, int(np.ceil(max(nx, ny) / max_surface_cells)))
        sub = drop[::stride, ::stride] * 1e6          # → μm
        g = res.surface.grid
        # 取**深度最大的一行**作为"沿扫描线"截面 —— 不能固定取几何中心那一行：
        # 扫描线按间距 h 分布，中心行很可能**正好落在两条线之间**，
        # 那样截出来全是 0，看起来像"什么都没加工"（实测踩过）。
        row_idx = int(np.argmax(drop.mean(axis=1)))
        col_idx = nx // 2
        surf = {
            "kind": "rectangular_pocket",
            "domainUm": float(dom[0]),
            "machiningRegionUm": float(region_um[0]),
            "marginMaxDepthUm": margin_max_um,
            "nx": int(sub.shape[1]),
            "ny": int(sub.shape[0]),
            "stride": stride,
            "dxUm": float(g.dx_m) * 1e6 * stride,
            "dyUm": float(g.dy_m) * 1e6 * stride,
            "regionUm": [float(region_um[0]), float(region_um[1])],
            "depthUm": [[round(float(v), 4) for v in row] for row in sub],
            # 沿扫描方向（X）：某一扫描线上的纵向起伏
            "sectionAlongXUm": [round(float(v), 4) for v in (drop[row_idx] * 1e6)],
            "sectionXAxisUm": [round(float(v), 4) for v in
                               ((np.arange(nx) - nx // 2) * g.dx_m * 1e6)],
            # 垂直扫描方向（Y）：**这一条最能看出搭接起伏与漏加工**
            "sectionAlongYUm": [round(float(v), 4) for v in (drop[:, col_idx] * 1e6)],
            "sectionYAxisUm": [round(float(v), 4) for v in
                               ((np.arange(ny) - ny // 2) * g.dy_m * 1e6)],
            "stats": {
                "meanUm": float(drop.mean()) * 1e6,
                "maxUm": float(drop.max()) * 1e6,
                "p5Um": float(np.percentile(drop, 5)) * 1e6,
                "p95Um": float(np.percentile(drop, 95)) * 1e6,
                "ripplePvUm": float(np.percentile(drop, 95) - np.percentile(drop, 5)) * 1e6,
            },
            "note": ("沿 Y 的截面最能看出**搭接起伏**：h 越小重叠越多、底部越平，"
                     "但深度也越大。高斯光束**做不到理想平底**。"),
        }
    mean_d = float(np.mean(drop)) * 1e6
    max_d = float(np.max(drop)) * 1e6
    cov = float(np.mean(touched))
    under = float(np.mean(drop < (target_depth_um - tolerance_um) * 1e-6))
    over = float(np.mean(drop > (target_depth_um + tolerance_um) * 1e-6))
    std = float(np.std(drop[touched])) * 1e6 if np.any(touched) else 0.0

    status = CAND_OK
    reasons: list[str] = []
    if mean_d < target_depth_um - tolerance_um:
        status = CAND_TOO_SHALLOW
        reasons.append(f"平均深度 {mean_d:.2f} μm < 目标下界 {target_depth_um - tolerance_um:.2f} μm")
    elif mean_d > target_depth_um + tolerance_um:
        status = CAND_TOO_DEEP
        reasons.append(f"平均深度 {mean_d:.2f} μm > 目标上界 {target_depth_um + tolerance_um:.2f} μm")
    if cov < min_coverage:
        status = CAND_UNDERCOVERED
        reasons.append(f"覆盖率 {cov:.1%} < {min_coverage:.0%}（存在未加工区）")
    if over > max_overcut_fraction:
        # 过切判据：阈值默认固定（不给用户一排开关），但保留可配以便开发者核对灵敏度
        status = CAND_OVERCUT
        reasons.append(
            f"过切面积占比 {over:.1%} > {max_overcut_fraction:.0%}（目标 {target_depth_um:g}±{tolerance_um:g} μm）"
        )

    return CandidateResult(
        spacing_um=float(spacing_um), pass_count=int(pass_count), plan=plan,
        status=status,
        mean_depth_um=mean_d, max_depth_um=max_d,
        ideal_time_s=plan.ideal_scan_time_s,
        coverage_fraction=cov, under_depth_fraction=under, over_depth_fraction=over,
        depth_std_um=std,
        surface=surf,
        domain_um=float(dom[0]),
        machining_region_um=float(region_um[0]),
        margin_max_depth_um=margin_max_um,
        notes=tuple(reasons + _policy_notes(applied_policy, skipped_fraction)),
        window_radius_policy=applied_policy,
        window_cells_skipped_fraction=skipped_fraction,
    )


@dataclass
class TargetPlanResult:
    """h/N 规划的完整结果。**无可行方案时如实说明**。"""

    target_depth_um: float
    tolerance_um: float
    region_um: tuple[float, float]
    domain_um: tuple[float, float] | None = None
    candidates: tuple[CandidateResult, ...] = ()
    recommended: CandidateResult | None = None
    infeasible_reason: str | None = None
    #: 加工形貌类型 —— 矩形区域走弓字形填充，结果是**矩形槽**。
    machined_shape: str = "rectangular_pocket"
    #: 筛选用的网格步长（μm）与细核步长（μm）。两者不同即为**两级**。
    screening_dx_um: float | None = None
    final_dx_um: float | None = None
    #: **无可行方案时**的最接近候选（**明确不满足约束**，仅供诊断，不是推荐）。
    best_effort: CandidateResult | None = None
    best_effort_violation: float | None = None
    #: 几何依据：声明的实测单线宽度、等效光斑半径、名义束腰。
    geometry_basis: Mapping[str, Any] = field(default_factory=dict)
    #: 本次枚举**实际生效**的窗口半径策略与余量（性能开关，见 ADR-0017）。
    window_radius_policy: str = "tail_epsilon"
    window_threshold_margin: float = 1.25
    notes: tuple[str, ...] = ()

    @property
    def feasible_candidates(self) -> tuple[CandidateResult, ...]:
        """可行候选，**与 ``recommended`` 用同一排序**。

        这样「推荐」恒等于本列表第一项 —— 否则界面上的推荐与表格对不上。
        """
        fs = [c for c in self.candidates if c.feasible]
        fs.sort(key=_rank_key)
        return tuple(fs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "ufdemo.target_plan/1",
            "domainUm": list(self.domain_um) if self.domain_um else list(self.region_um),
            "targetDepthUm": self.target_depth_um,
            "toleranceUm": self.tolerance_um,
            "regionUm": list(self.region_um),
            "nCandidates": len(self.candidates),
            "nFeasible": len(self.feasible_candidates),
            "machinedShape": self.machined_shape,
            "screeningDxUm": self.screening_dx_um,
            "finalDxUm": self.final_dx_um,
            "geometryBasis": dict(self.geometry_basis),
            "windowRadiusPolicy": self.window_radius_policy,
            "windowThresholdMargin": self.window_threshold_margin,
            "recommended": self.recommended.to_dict() if self.recommended else None,
            "infeasibleReason": self.infeasible_reason,
            "bestEffort": self.best_effort.to_dict() if self.best_effort else None,
            "bestEffortViolation": self.best_effort_violation,
            "bestEffortNote": (
                "**最接近的候选，不满足约束** —— 仅用于诊断，不得当作推荐方案。"
                if self.best_effort else None
            ),
            "notes": list(self.notes),
            "candidates": [c.to_dict() for c in self.candidates],
        }


def plan_for_target(
    *,
    material_card_file: str,
    target_depth_um: float,
    tolerance_um: float,
    pulse_duration_fs: float,
    repetition_rate_kHz: float,
    scan_speed_mm_s: float,
    region_um: tuple[float, float] = (200.0, 200.0),
    domain_um: tuple[float, float] | None = None,
    dx_um: float = 0.5,
    spacings_um: Iterable[float] = DEFAULT_SPACINGS_UM,
    pass_counts: Iterable[int] = DEFAULT_PASS_COUNTS,
    gain: float = 1.0,
    response_override: Mapping[str, Any] | None = None,
    min_coverage: float = 0.98,
    max_overcut_fraction: float = 0.05,
    auto_coarsen: bool = True,
    bg: Any | None = None,
    window_radius_policy: str = "tail_epsilon",
    window_threshold_margin: float = 1.25,
) -> TargetPlanResult:
    """枚举 h/N，筛出可行方案，按**时间**与**均匀性**排序给推荐。

    **不引入复杂多目标优化器**（任务书 §6）：默认给一个推荐方案，
    其余可行候选放在表格里；无可行方案时给出**原因**。

    排序只用**模型可给**的量：先看时间（越短越好），时间并列再看均匀性
    （深度标准差越小越好）。间距**不精确到任意小数** ——
    只有平均深度证据时，输出的是可行范围而非"精确最优 h"。
    """
    # --- 两阶段：**粗网格筛选 → 细网格复核** ------------------------------
    # 200×200 加工区在 0.5 μm 网格上单个候选就要 20–50 s，25 个候选要 20+ 分钟。
    # 而 h/N 的排序（时间、覆盖）在粗网格上已经稳定，所以：
    #   · 用粗网格把所有候选过一遍（快）
    #   · 只把**推荐/最接近**的那一个用细网格复核（准）
    # 任务书 §7.1 允许"内部自动选合理网格"，这里把两级都如实报出来。
    screen_dx = float(dx_um)
    if auto_coarsen and float(region_um[0]) >= 100.0:
        screen_dx = max(screen_dx, 1.0)
    screening = screen_dx != float(dx_um)

    cands: list[CandidateResult] = []
    for h in spacings_um:
        for n in pass_counts:
            cands.append(evaluate_candidate(
                h, n,
                material_card_file=material_card_file,
                region_um=region_um, dx_um=screen_dx, domain_um=domain_um,
                pulse_duration_fs=pulse_duration_fs,
                repetition_rate_kHz=repetition_rate_kHz,
                scan_speed_mm_s=scan_speed_mm_s,
                target_depth_um=target_depth_um, tolerance_um=tolerance_um,
                gain=gain, response_override=response_override,
                min_coverage=min_coverage, max_overcut_fraction=max_overcut_fraction,
                bg=bg,
                window_radius_policy=window_radius_policy,
                window_threshold_margin=window_threshold_margin,
            ))

    feasible = [c for c in cands if c.feasible]
    rec: CandidateResult | None = None
    reason: str | None = None
    if feasible:
        feasible.sort(key=_rank_key)
        rec = feasible[0]
        # 给推荐候选补一张**矩形槽形貌**（其余候选不带，避免响应体过大）。
        # 注意这是**再跑一次同一个候选**，不是把上面的数值搬过来 ——
        # 保证展示的图与推荐参数是同一套输入算出来的。
        rec_with_surface = evaluate_candidate(
            rec.spacing_um, rec.pass_count,
            material_card_file=material_card_file,
            region_um=region_um, dx_um=float(dx_um), domain_um=domain_um,
            pulse_duration_fs=pulse_duration_fs,
            repetition_rate_kHz=repetition_rate_kHz,
            scan_speed_mm_s=scan_speed_mm_s,
            target_depth_um=target_depth_um, tolerance_um=tolerance_um,
            gain=gain, response_override=response_override,
            min_coverage=min_coverage, max_overcut_fraction=max_overcut_fraction,
            want_surface=True, bg=bg,
            window_radius_policy=window_radius_policy,
            window_threshold_margin=window_threshold_margin,
        )
        if rec_with_surface.surface is not None:
            # **替换回列表**，而不是留一个游离的对象 ——
            # 否则 `recommended` 与 `feasible_candidates[0]` 是"同参数不同实例"，
            # 「界面推荐 = 表格第一行」这个不变量就断了（测试抓到过）。
            for _i, _c in enumerate(cands):
                if (_c.spacing_um == rec.spacing_um
                        and _c.pass_count == rec.pass_count):
                    cands[_i] = rec_with_surface
                    break
            rec = rec_with_surface
    else:
        # 无可行组合 → 说清是"都太浅"还是"都太深"，而不是丢一句"无解"。
        d = [c.mean_depth_um for c in cands if c.mean_depth_um is not None]
        if d and max(d) < target_depth_um - tolerance_um:
            reason = (f"当前范围内无可行方案：所有候选都**达不到**目标深度"
                      f"（最大只能到 {max(d):.2f} μm，目标 {target_depth_um:.2f} μm）。"
                      "需提高能量/降低速度，或放宽 h/N 范围。")
        elif d and min(d) > target_depth_um + tolerance_um:
            reason = (f"当前范围内无可行方案：所有候选都**超过**目标深度"
                      f"（最小 {min(d):.2f} μm，目标 {target_depth_um:.2f} μm）。"
                      "需降低能量/提高速度，或放宽 h/N 范围。")
        else:
            bad = {c.status for c in cands if not c.feasible}
            reason = (f"当前范围内无可行方案：候选因以下原因被否 {sorted(bad)}"
                      "（覆盖/过切等横向约束不满足）。")

    # 无可行方案时，挑一个**违规最小**的候选并附上矩形槽形貌 ——
    # 目的是让人能看见"实际会做成什么样"，报告里会明确标注它不满足约束。
    best: CandidateResult | None = None
    best_v: float | None = None
    if rec is None and cands:
        scored = [(c, _violation(c, target_um=target_depth_um, tol_um=tolerance_um))
                  for c in cands]
        scored = [(c, v) for c, v in scored if c.mean_depth_um is not None]
        if scored:
            scored.sort(key=lambda kv: kv[1])
            cand0, v0 = scored[0]
            best_v = v0
            cand_s = evaluate_candidate(
                cand0.spacing_um, cand0.pass_count,
                material_card_file=material_card_file,
                region_um=region_um, dx_um=float(dx_um), domain_um=domain_um,
                pulse_duration_fs=pulse_duration_fs,
                repetition_rate_kHz=repetition_rate_kHz,
                scan_speed_mm_s=scan_speed_mm_s,
                target_depth_um=target_depth_um, tolerance_um=tolerance_um,
                gain=gain, response_override=response_override,
                min_coverage=min_coverage, max_overcut_fraction=max_overcut_fraction,
                want_surface=True, bg=bg,
                window_radius_policy=window_radius_policy,
                window_threshold_margin=window_threshold_margin,
            )
            if cand_s.surface is not None:
                for _i, _c in enumerate(cands):
                    if (_c.spacing_um == cand0.spacing_um
                            and _c.pass_count == cand0.pass_count):
                        cands[_i] = cand_s
                        break
                best = cand_s
            else:
                best = cand0

    # 几何依据：把「名义光学值」与「实测单线宽度 / 等效光斑」分开写清楚。
    from .config import load_shared_background as _lsb

    _bg = bg or _lsb()
    _thr = (response_override or {}).get("threshold_J_m2")
    geom: dict[str, Any] = {"machinedShape": "rectangular_pocket"}
    if _thr:
        _energy = _bg.pulse_energy_J(float(repetition_rate_kHz) * 1e3)
        _w, _basis = _bg.equivalent_spot_radius_m(
            pulse_energy_J=_energy, threshold_J_m2=float(_thr))
        _half = _bg.ablated_half_width_m(spot_radius_m=_w, pulse_energy_J=_energy,
                                         threshold_J_m2=float(_thr))
        geom.update({
            "thresholdJm2": float(_thr),
            "pulseEnergyUJ": _energy * 1e6,
            "spotRadiusUm": _w * 1e6,
            "lineWidthModelUm": 2.0 * _half * 1e6,
            "spotRadiusBasis": _basis,
        })
        if _basis.get("declared_width_um") is not None:
            geom["declaredLineWidthUm"] = _basis["declared_width_um"]
            geom["nominalWaistUm"] = _basis.get("nominal_waist_um")
            geom["nominalOpticsWidthUm"] = _basis.get("nominal_width_um")
    else:
        geom["nominalWaistUm"] = _bg.derived_waist_m() * 1e6
        geom["note"] = "未给阈值 → 无法按实测单线宽度反推等效光斑"

    notes = [
        "**加工形貌是矩形槽**（矩形区域按弓字形填充），不是单个圆坑。",
        (f"仿真域 {float((domain_um or region_um)[0]):g} μm，加工区 "
         f"{float(region_um[0]):g} μm —— 统计只算**加工区**，外围余量单列。"),
        "N 是**层数**：每层一次完整弓字形扫描；**不做逐层 Z 调整**（焦点恒在原始上表面）。",
        "**标量深度误差**（若有实验对照）属实验证据；"
        "**覆盖率 / 漏加工 / 过切 / 深度标准差**是**模型预测**，"
        "未导入实测高度图时**不构成**二维形貌验证。",
        "推荐按「理想加工时间 → 深度标准差」排序；"
        "只有平均深度证据时，h 只给到搜索网格的粒度，**不精确到任意小数**。",
        "总时长为理想值（不含换向减速与额外边框，未确知）。",
    ]
    if screening:
        notes.append(
            f"**两级网格**：25 个候选用 {screen_dx:g} μm 粗筛（快），"
            f"推荐/最接近的那一个用 {float(dx_um):g} μm 细核（准）；"
            "表里的数值来自**粗筛**，展示的形貌与标注来自**细核** —— "
            "两者不一致时以细核为准。"
        )
    if not feasible:
        notes.append("**本次未给出推荐方案** —— 这是如实的结果，不是失败。")

    return TargetPlanResult(
        target_depth_um=float(target_depth_um),
        tolerance_um=float(tolerance_um),
        region_um=(float(region_um[0]), float(region_um[1])),
        domain_um=(float((domain_um or region_um)[0]), float((domain_um or region_um)[1])),
        candidates=tuple(cands),
        recommended=rec,
        infeasible_reason=reason,
        machined_shape="rectangular_pocket",
        screening_dx_um=screen_dx,
        final_dx_um=float(dx_um),
        best_effort=best,
        best_effort_violation=best_v,
        geometry_basis=geom,
        window_radius_policy=str(window_radius_policy),
        window_threshold_margin=float(window_threshold_margin),
        notes=tuple(notes),
    )
