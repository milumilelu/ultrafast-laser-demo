"""高斯能流、离焦与局部裁剪窗口（执行细则 4.3、5.2 第 2–3 步）。

约定（任务书 6.1 节，实现时不得自行变更）：

* 光斑半径统一为**能流分布的 1/e² 半径**；
* ``F_perp(r,s) = 2E_p/(pi w(s)^2) * exp(-2 r^2 / w(s)^2)``，
  ``w(s) = w0 * sqrt(1 + (s/zR)^2)``；
* 局部更新半径 ``r_cut = w * sqrt(ln(1/eps)/2)``，默认 ``eps=1e-8``，
  约为 ``3.034854 w``。这是数值设置，**不是物理损伤阈值**。

M0 只开放正入射；倾斜投影与法向厚度转换属 M3（``geometry.py``）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import GEOMETRY_UNSUPPORTED, NUMERIC_NONFINITE, UFDemoError

# 细则 4.1：默认测试值
DEFAULT_TAIL_EPSILON = 1e-8


def w_of_s(w0: float, s: float, zR: float | None) -> float:
    """轴上距离 s 处的 1/e² 半径。``zR=None`` 表示准直（无发散），返回 w0。"""
    if zR is None:
        return w0
    return w0 * math.sqrt(1.0 + (s / zR) ** 2)


def cut_radius(w: float, epsilon: float = DEFAULT_TAIL_EPSILON) -> float:
    """相对能流截断 ``eps`` 对应的裁剪半径 ``w*sqrt(ln(1/eps)/2)``。"""
    return w * math.sqrt(math.log(1.0 / epsilon) / 2.0)


def peak_fluence(pulse_energy_J: float, w: float) -> float:
    """平面高斯峰值能流 ``2E/(pi w^2)``。"""
    return 2.0 * pulse_energy_J / (math.pi * w * w)


def gaussian_fluence_perp(r2, pulse_energy_J: float, w: float):
    """``F_perp(r) = 2E/(pi w^2) exp(-2 r^2 / w^2)``，接受数组输入。"""
    import numpy as np

    return (2.0 * pulse_energy_J / (math.pi * w * w)) * np.exp(-2.0 * np.asarray(r2, dtype=np.float64) / (w * w))


def enclosed_energy_fraction(r: float, w: float) -> float:
    """半径 r 圆内的能量占比 ``1 - exp(-2r^2/w^2)``。"""
    return 1.0 - math.exp(-2.0 * (r * r) / (w * w))


def _window_bounds(axis, center: float, r_cut: float) -> tuple[int, int]:
    """在严格递增的坐标轴上定位 ``[center - r_cut, center + r_cut]`` 覆盖的单元区间。"""
    import numpy as np

    lo = float(center) - r_cut
    hi = float(center) + r_cut
    i0 = int(np.searchsorted(axis, lo, side="left"))
    i1 = int(np.searchsorted(axis, hi, side="right"))
    return max(0, i0), min(int(axis.size), i1)


@dataclass
class FluencePatch:
    """局部能流窗口。

    至少包含网格切片、局部能流、有效照射掩膜和能量账本信息（细则 4.3）。
    """

    iy0: int
    iy1: int
    ix0: int
    ix1: int
    r2: Any  # (ny_win, nx_win) float64
    fluence: Any  # (ny_win, nx_win) float64，入射能流
    mask: Any  # bool，有效照射掩膜
    axial_distance: float
    spot_radius: float
    emitted_energy_J: float
    estimated_intercepted_energy_J: float
    domain_truncated_fraction: float
    tail_truncated_fraction: float
    notes: list[str] = field(default_factory=list)
    geometry_mode: str = "fixed_geometry"

    @property
    def shape(self) -> tuple[int, int]:
        return (self.iy1 - self.iy0, self.ix1 - self.ix0)

    @property
    def empty(self) -> bool:
        return self.shape[0] == 0 or self.shape[1] == 0

    def ledger(self) -> dict[str, Any]:
        """能量账本。细则 5.4：不把发射与截获差值解释为热损失。"""
        delivered = self.fluid_delivered()
        return {
            "emitted_energy_J": self.emitted_energy_J,
            "estimated_intercepted_energy_J": self.estimated_intercepted_energy_J,
            "numerical_tail_truncated_fraction": self.tail_truncated_fraction,
            "domain_truncated_fraction": self.domain_truncated_fraction,
            "fluence_patch_sum_fraction": delivered,
            "note": (
                "发射能量与估计截获能量之差只反映数值裁剪与计算域边界，"
                "不得解释为热损失或被吸收能量。"
            ),
        }

    def fluid_delivered(self) -> float:
        if self.emitted_energy_J == 0.0:
            return 0.0
        return self.estimated_intercepted_energy_J / self.emitted_energy_J


@dataclass
class BeamOptions:
    geometry_feedback: str = "fixed_geometry"
    tail_epsilon: float = DEFAULT_TAIL_EPSILON
    section: tuple[Any, Any] | None = None  # (iy0, iy1, ix0, ix1) 预计算窗口
    record_ledger: bool = True


def beam_patch(event: Any, surface: Any, options: BeamOptions | Mapping[str, Any]) -> FluencePatch:
    """计算某事件在表面上的局部能流入射。

    细则 5.2 第 3 步：**读取本事件开始时的高度**计算能流，不得边更新局部像素
    边重算同一事件的光束几何。

    细则 5.2 第 2 步：窗口与计算域完全无交集时，仍记录发射能量和事件，
    不制造去除。
    """
    import numpy as np

    if isinstance(options, Mapping):
        options = BeamOptions(**{k: v for k, v in options.items() if k in BeamOptions.__dataclass_fields__})

    laser = surface.laser
    cfg_dir = laser.direction_unit
    # --- 正入射准入（M0）---------------------------------------------------
    if abs(abs(cfg_dir[2]) - 1.0) > 1e-12 or abs(cfg_dir[0]) > 1e-12 or abs(cfg_dir[1]) > 1e-12:
        raise UFDemoError(
            GEOMETRY_UNSUPPORTED,
            "M0 只支持正入射（传播方向 ±z）",
            field_path="laser.direction_unit",
            actual=list(cfg_dir),
            requirement="|k_z|=1 且 k_x=k_y=0",
            suggestion="改用正入射；斜入射与法向厚度转换属 M3（T18）。",
        )

    w0 = laser.spot_radius_m
    zR = laser.rayleigh_range_m
    h = surface.height
    h_ref = surface.initial_height if options.geometry_feedback == "fixed_geometry" else h

    # --- 局部窗口 ----------------------------------------------------------
    # 离焦会扩大光斑；窗口必须覆盖整个当前表面可能出现的最大光斑。
    fx, fy, fz = event.focus_xyz_m
    max_s = max(abs(float(np.min(h_ref)) - fz), abs(float(np.max(h_ref)) - fz))
    w_bound = w_of_s(w0, max_s, zR)
    r_cut = cut_radius(w_bound, options.tail_epsilon)
    if options.section is not None:
        iy0, iy1, ix0, ix1 = options.section
        iy0, iy1 = max(0, int(iy0)), min(surface.grid.ny, int(iy1))
        ix0, ix1 = max(0, int(ix0)), min(surface.grid.nx, int(ix1))
    else:
        fx, fy, _fz = event.focus_xyz_m
        # 用裁剪半径在坐标轴上直接定位窗口，而不是用最近网格点 + 固定跨度：
        # 焦点远离计算域时必须得到空窗口，不能吸附到边界后错误地覆盖部分网格。
        iy0, iy1 = _window_bounds(surface.y, fy, r_cut)
        ix0, ix1 = _window_bounds(surface.x, fx, r_cut)

    emitted = float(event.energy_J)

    if iy1 <= iy0 or ix1 <= ix0:
        return FluencePatch(
            iy0=iy0,
            iy1=max(iy0, iy1),
            ix0=ix0,
            ix1=max(ix0, ix1),
            r2=np.zeros((0, 0), dtype=np.float64),
            fluence=np.zeros((0, 0), dtype=np.float64),
            mask=np.zeros((0, 0), dtype=bool),
            axial_distance=0.0,
            spot_radius=w0,
            emitted_energy_J=emitted,
            estimated_intercepted_energy_J=0.0,
            domain_truncated_fraction=1.0,
            tail_truncated_fraction=1.0,
            notes=["局部窗口与计算域无交集：记录发射能量，不制造去除。"],
            geometry_mode=options.geometry_feedback,
        )

    xn = surface.x[ix0:ix1]
    yn = surface.y[iy0:iy1]
    XX, YY = np.meshgrid(xn, yn)  # (ny_win, nx_win)
    HH = h_ref[iy0:iy1, ix0:ix1]

    fx, fy, fz = event.focus_xyz_m
    # 正入射：k=(0,0,±1)。取 k_z=+1 的约定，轴向距离 s = h - z_f
    s_field = HH - fz
    dx2 = (XX - fx) ** 2 + (YY - fy) ** 2
    # r^2 = |q-q_f|^2 - s^2；仅将浮点舍入导致的极小负值截为零
    r2 = dx2  # 正入射直接计算，避免大轴向距离下的消减误差。

    if zR is None:
        F = gaussian_fluence_perp(r2, emitted, w0)
        w_used = w0
        s_mean = 0.0
    else:
        s_mean = float(np.mean(s_field))
        w_field = w0 * np.sqrt(1.0 + (s_field / zR) ** 2)
        w_used = float(np.max(w_field))
        F = gaussian_fluence_perp(r2, emitted, w_field)

    if not np.all(np.isfinite(F)):
        raise UFDemoError(
            NUMERIC_NONFINITE,
            "能流场出现非有限值",
            field_path="beam_patch.fluence",
            actual="NaN/Inf present",
            suggestion="检查 spot_radius_m、pulse_energy_J 与网格间距。",
        )

    local_w = w0 if zR is None else w_field
    mask = r2 <= local_w ** 2 * math.log(1.0 / options.tail_epsilon) / 2.0
    dA = surface.grid.dx_m * surface.grid.dy_m
    intercepted = float(np.sum(F[mask]) * dA)
    total_plane = float(np.sum(F) * dA)

    # 尾部截断：裁剪半径之外的能量占比（解析值）。它是数值设置，不是物理阈值。
    tail_frac_analytic = math.exp(-2.0 * (r_cut * r_cut) / (w_used * w_used))
    domain_frac = max(0.0, min(1.0, 1.0 - intercepted / emitted)) if emitted > 0 else 0.0

    notes: list[str] = []
    if abs(float(np.min(HH)) - float(np.max(HH))) > 0.0 and options.geometry_feedback == "fixed_geometry":
        notes.append("固定几何解析基准：本事件使用初始表面高度计算离焦，忽略当前高度变化。")

    return FluencePatch(
        iy0=iy0,
        iy1=iy1,
        ix0=ix0,
        ix1=ix1,
        r2=r2,
        fluence=F,
        mask=mask,
        axial_distance=s_mean,
        spot_radius=w_used,
        emitted_energy_J=emitted,
        estimated_intercepted_energy_J=intercepted,
        domain_truncated_fraction=domain_frac,
        tail_truncated_fraction=tail_frac_analytic,
        notes=notes,
        geometry_mode=options.geometry_feedback,
    )


def analytic_fixed_geometry_section(surface: Any, center_xy: tuple[float, float], epsilon: float) -> tuple[int, int, int, int]:
    """预计算局部窗口，供批量/重复调用复用（不改变物理语义）。"""
    w = surface.laser.spot_radius_m
    r_cut = cut_radius(w, epsilon)
    iy0, iy1 = _window_bounds(surface.y, center_xy[1], r_cut)
    ix0, ix1 = _window_bounds(surface.x, center_xy[0], r_cut)
    return (iy0, iy1, ix0, ix1)
