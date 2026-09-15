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
    # 批次 J（T18）：斜入射/动态角度的诊断。正入射时保持默认值，逐位不变。
    oblique: bool = False
    dynamic_angle: bool = False
    mu: Any = None                    # (ny_win, nx_win) 入射余弦；正入射为 None
    nz: Any = None                    # (ny_win, nx_win) 法向 z 分量；正入射为 None
    visibility: dict[str, Any] | None = None   # 首次交点可见性统计
    intercepted_energy_plane_J: float | None = None  # 未经 μ 缩放的平面能量（诊断用）

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
    # 批次 J（T18）：动态角度——逐点法向 + 可见性；关闭时斜入射按**平面法向**处理
    dynamic_angle: bool = False


def is_axial_direction(direction_unit: Any) -> bool:
    """是否为纯正入射（``k_x=k_y=0`` 且 ``k_z>0``，本工程的"光轴正向"约定）。

    纯正入射走**既有正入射核**，保证 0° 严格退化（G07 第一条断言）且与批次 A–I 逐位一致。
    """
    k = tuple(float(v) for v in direction_unit)
    return abs(k[0]) < 1e-15 and abs(k[1]) < 1e-15 and k[2] > 0.0


def axial_and_lateral(k: Any, XX: Any, YY: Any, ZZ: Any, fx: float, fy: float, fz: float):
    """一般方向下的轴向距离与横向距离平方。

    任务书 6.6：``s = (q-q_f)·k``，``r^2 = |q-q_f|^2 - s^2``。
    **只**把浮点舍入导致的极小负 ``r^2`` 截为零；明显负值视为实现错误并报错
    （细则 9.2：「仅对浮点舍入导致的极小负 r² 截为零，明显负值视为实现错误」）。
    """
    import numpy as np

    kv = np.asarray(k, dtype=np.float64)
    QX = XX - fx
    QY = YY - fy
    QZ = ZZ - fz
    s = QX * kv[0] + QY * kv[1] + QZ * kv[2]
    d2 = QX * QX + QY * QY + QZ * QZ
    r2 = d2 - s * s
    if np.any(r2 < 0.0):
        # 容差按量级取，只吸收浮点舍入（相对 1e-12），不掩盖几何错误
        tol = 1e-12 * np.maximum(d2, 1e-300)
        bad = r2 < -tol
        if np.any(bad):
            raise UFDemoError(
                NUMERIC_NONFINITE,
                "r^2 出现明显负值（应为实现错误，不是舍入）",
                field_path="beam.r2",
                actual=float(np.min(r2)),
                requirement="r^2 >= 0（极小负值仅由浮点舍入产生）",
                suggestion="检查方向单位矢量是否已归一化、以及 q-q_f 的计算。",
            )
        r2 = np.where(r2 < 0.0, 0.0, r2)
    return s, r2


def oblique_window_radius(r_cut: float, k: Any, axial_span: float, *, min_mu: float = 0.5) -> float:
    """斜入射/动态角度下的保守窗口半径。

    横向足迹被拉伸：绕轴半径 ``r_cut`` 的圆投影到 x/y 后，沿倾斜方向半轴约放大
    ``1/μ``（60° 时正好 2 倍）。离焦（``|s|`` 不可忽略）还会带来横向平移
    ``|s|·tanθ``。动态角度下逐点法向可继续缩小 μ，故用``μ``下界（默认 0.5，
    与支持范围一致）再算一次。

    **宁可窗口偏大**（网格会裁剪），不可偏小（会静默漏掉被照射单元）。
    """
    kz = max(float(k[2]), 1e-12)
    mu_floor = max(min(kz, float(min_mu)), 1e-12)
    lateral = abs(float(axial_span)) * math.sqrt(max(0.0, 1.0 / (kz * kz) - 1.0))  # = |s|·tanθ
    return (float(r_cut) + lateral) / mu_floor


def is_flat_initial_surface(grid: Any) -> bool:
    """初始面是否为**真正平面**（面法向 = ``(0,0,1)``）。

    为什么需要它：``beam_patch`` 里有一条「轴向光束 + 关闭动态角度」的快捷路径，
    直接 ``μ=None``、``F=F_perp``。这条路径的隐含假设是
    **「光束沿全局 z 轴」⇒「表面水平」⇒ μ=1**。

    但 ``SurfaceState.initialize`` 支持 ``initial_surface="tilted_plane"``，
    此时**轴向 ≠ 垂直**：斜面法向不是 z 轴，仍需要按 μ 做投影、并按
    ``Δh = -a_n/n_z`` 做法向厚度换算。关闭 ``dynamic_angle`` 只应表示
    「不随形貌演化更新法向」，**不等于初始坡度不存在**。

    端到端实测（修复前）：轴向光束 + 45° 斜面 + ``dynamic_angle=False``，
    中心列高下降恰好等于 ``ln(2)·δ``（= 完全按水平面算），
    正确值应为 ``ln(√2)·√2·δ``，**高估 41.42%**。

    所以快捷路径只对真正平面成立；斜面落到常规分支 —— 该分支在
    ``dynamic=False`` 时本来就用 ``geometry.analytic_plane_normal()`` 的解析法向，
    语义正好是「固定角度、不随形变更新」。
    """
    if str(getattr(grid, "initial_surface", "flat")) != "tilted_plane":
        return True
    sx, sy = getattr(grid, "initial_slope", (0.0, 0.0))
    return abs(float(sx)) <= 0.0 and abs(float(sy)) <= 0.0


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
    kvec = (float(cfg_dir[0]), float(cfg_dir[1]), float(cfg_dir[2]))
    dynamic = bool(getattr(options, "dynamic_angle", False))
    axial = is_axial_direction(kvec)

    # --- 方向约定（本工程：k 指向光源侧，k·n>0 才算被照射）----------------
    if not (abs(kvec[0] ** 2 + kvec[1] ** 2 + kvec[2] ** 2 - 1.0) < 1e-9):
        raise UFDemoError(
            GEOMETRY_UNSUPPORTED,
            "direction_unit 未归一化",
            field_path="laser.direction_unit",
            actual=list(kvec),
            requirement="单位矢量（|k|=1）",
        )
    if kvec[2] <= 0.0:
        raise UFDemoError(
            GEOMETRY_UNSUPPORTED,
            "direction_unit 的 z 分量必须为正（本工程取「光轴正向」约定）",
            field_path="laser.direction_unit",
            actual=list(kvec),
            requirement="k_z > 0，使 μ=k·n>0 表示被照射",
            suggestion="把方向整体取反；符号约定见 ufdemo.geometry 模块说明。",
        )

    w0 = laser.spot_radius_m
    zR = laser.rayleigh_range_m
    h = surface.height
    h_ref = surface.initial_height if options.geometry_feedback == "fixed_geometry" else h

    # --- 局部窗口（迭代定尺寸）----------------------------------------------
    #
    # 旧实现用**整个网格**的轴向跨度定窗口半径：
    #     max_s = max(|min(h_ref)-fz|, |max(h_ref)-fz|)
    # 于是「域里任何一处深坑」都会放大**所有**事件的光斑，窗口大小随域尺寸而变；
    # 而光束只关心**它自己脚下**（窗口内）的表面起伏。现在改成局部迭代：
    # 名义半径开窗 → 看窗口内的起伏 → 放大光斑 → 重开窗，直到不再增长。
    # 收敛后窗口外的格子能流 ≤ ε·峰值（ε = tail_epsilon），与既有尾部截断同一量级。
    #
    # 实测效果（1,040 事件、dx 0.5 µm、加工区 100 µm，域 100/200/400 µm）：
    #   旧 1.60 / 14.20 / 22.20 s → 新 1.50 / 8.89 / 14.17 s。
    # **不是数量级提速**：本算例的深区本来就在光束脚下，全局跨度 ≈ 局部跨度。
    #
    # ⚠️ 真正的瓶颈**不在本函数**（已用对照实验排除「隐藏的 O(网格) 代码路径」）：
    # `geometry_feedback=fixed_geometry`（离焦恒为 0）时，域 100/200/400 µm 的
    # 单事件成本是 131 / 132 / 138 µs —— **网格涨 16 倍，成本几乎不变**。
    # 成本 ≈ 0.06 µs × 窗口格数，**完全由窗口大小决定**：`axial_defocus` 下
    # 本算例烧到 132 µm ≫ zR=4.47 µm ⇒ 离焦 30 倍 ⇒ 光斑 1.33→39 µm
    # ⇒ ε 窗口直径 237 µm（≈ 吃满 200 µm 域）。而窗口内**超阈值**（对去除
    # 真有贡献）的格子只占 **0.8%** —— 99% 的计算是零贡献。
    # ⇒ 优化方向是「按超阈值半径开窗」，不是改 numpy 用法。见 docs/reports/。
    fx, fy, fz = event.focus_xyz_m
    _eps = options.tail_epsilon
    _oblique = not (axial and not dynamic)

    axial_span_true = 0.0
    # `r_cut` 是**横向**裁剪半径（不含斜入射的窗口平移项），后面算尾部截断比例要用它；
    # `window_r` 才是最终开窗半径（斜入射时含平移）。
    r_cut = cut_radius(w0, _eps)
    window_r = r_cut if not _oblique else oblique_window_radius(r_cut, kvec, 0.0)
    if options.section is None:
        for _ in range(8):
            iy0, iy1 = _window_bounds(surface.y, fy, window_r)
            ix0, ix1 = _window_bounds(surface.x, fx, window_r)
            if iy1 <= iy0 or ix1 <= ix0:
                break
            _HH = h_ref[iy0:iy1, ix0:ix1]
            # **只看窗口内**的轴向跨度（这正是与旧实现的关键差别）
            s_local = max(abs(float(_HH.min()) - fz), abs(float(_HH.max()) - fz))
            axial_span_true = s_local
            m_s = s_local
            if not axial:
                _r_probe = cut_radius(w_of_s(w0, m_s, zR), _eps)
                m_s += 2.5 * _r_probe * math.hypot(kvec[0], kvec[1])
            r_new = cut_radius(w_of_s(w0, m_s, zR), _eps)
            r_cut = r_new
            w_new = oblique_window_radius(r_new, kvec, s_local) if _oblique else r_new
            if w_new <= window_r * (1.0 + 1e-12):
                break                      # 收敛：窗口已能容纳最大光斑
            window_r = w_new

    if options.section is not None:
        iy0, iy1, ix0, ix1 = options.section
        iy0, iy1 = max(0, int(iy0)), min(surface.grid.ny, int(iy1))
        ix0, ix1 = max(0, int(ix0)), min(surface.grid.nx, int(ix1))
    else:
        # 用裁剪半径在坐标轴上直接定位窗口，而不是用最近网格点 + 固定跨度：
        # 焦点远离计算域时必须得到空窗口，不能吸附到边界后错误地覆盖部分网格。
        iy0, iy1 = _window_bounds(surface.y, fy, window_r)
        ix0, ix1 = _window_bounds(surface.x, fx, window_r)

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
            oblique=False,
            dynamic_angle=dynamic,
        )
    xn = surface.x[ix0:ix1]
    yn = surface.y[iy0:iy1]
    XX, YY = np.meshgrid(xn, yn)  # (ny_win, nx_win)
    HH = h_ref[iy0:iy1, ix0:ix1]

    fx, fy, fz = event.focus_xyz_m
    if axial and not dynamic:
        # 正入射：k=(0,0,1)。取 k_z=+1 的约定，轴向距离 s = h - z_f
        s_field = HH - fz
        dx2 = (XX - fx) ** 2 + (YY - fy) ** 2
        # r^2 = |q-q_f|^2 - s^2；正入射下直接由横向距离给出，避免大轴向距离的消减误差。
        r2 = dx2
    else:
        s_field, r2 = axial_and_lateral(kvec, XX, YY, HH, fx, fy, fz)

    if zR is None:
        F_perp = gaussian_fluence_perp(r2, emitted, w0)
        w_used = w0
        s_mean = 0.0
    else:
        s_mean = float(np.mean(s_field))
        w_field = w0 * np.sqrt(1.0 + (s_field / zR) ** 2)
        w_used = float(np.max(w_field))
        F_perp = gaussian_fluence_perp(r2, emitted, w_field)

    if not np.all(np.isfinite(F_perp)):
        raise UFDemoError(
            NUMERIC_NONFINITE,
            "能流场出现非有限值",
            field_path="beam_patch.fluence",
            actual="NaN/Inf present",
            suggestion="检查 spot_radius_m、pulse_energy_J 与网格间距。",
        )

    local_w = w0 if zR is None else w_field
    geom_mask = r2 <= local_w ** 2 * math.log(1.0 / options.tail_epsilon) / 2.0

    notes: list[str] = []
    if abs(float(np.min(HH)) - float(np.max(HH))) > 0.0 and options.geometry_feedback == "fixed_geometry":
        notes.append("固定几何解析基准：本事件使用初始表面高度计算离焦，忽略当前高度变化。")

    # --- 批次 J：投影（F_s = μ F_perp）与可见性 ----------------------------
    # 快捷路径**只对真正平面**成立（轴向光束 ≠ 垂直于倾斜工件）：
    # 斜面即使关闭动态角度也必须做 μ 投影与法向厚度换算，见 is_flat_initial_surface()。
    # 这个判据在下面构造 FluencePatch 时**还要用一次**（决定 nz 是否上报），
    # 所以只算一次、两处共用 —— 避免像旧代码那样在三处各写一遍「axial and not dynamic」，
    # 而其中两处漏了斜面情形。
    axial_flat_shortcut = axial and not dynamic and is_flat_initial_surface(surface.grid)
    if axial_flat_shortcut:
        mu = None
        mask = geom_mask
        F = F_perp
        vis_stats = None
    else:
        from .geometry import (
            analytic_plane_normal,
            check_geometry_range,
            first_intersection_visibility,
            incidence_cosine,
            project_fluence,
            surface_normal,
            visibility_summary,
        )

        if dynamic:
            # 动态角度：逐点法向取自**当前窗口高度场**的梯度
            n_x, n_y, n_z = surface_normal(HH, surface.grid)
        else:
            # 固定角度：用**解析平面法向**（不随形变更新，也无离散误差）
            pnx, pny, pnz = analytic_plane_normal(surface.grid)
            n_x = np.full(HH.shape, pnx, dtype=np.float64)
            n_y = np.full(HH.shape, pny, dtype=np.float64)
            n_z = np.full(HH.shape, pnz, dtype=np.float64)

        mu = incidence_cosine(kvec, n_x, n_y, n_z)
        # 越界即停（不裁剪角度继续），并给出位置与原因
        check_geometry_range(n_z, mu, surface.grid, where="solver.dynamic_angle")

        visible = first_intersection_visibility(
            HH, surface.grid, kvec, section=(iy0, iy1, ix0, ix1),
            global_height=h_ref,
        )
        vis_stats = visibility_summary(visible, surface.grid)
        F = project_fluence(F_perp, mu, visible=visible)
        # 背向 (mu<=0) 与几何掩膜之外一律不照射
        mask = geom_mask & visible & (mu > 0.0)
        n_shadowed_win = int(np.count_nonzero(~visible))
        if n_shadowed_win:
            notes.append(
                f"本事件窗口内有 {n_shadowed_win} 个单元处于遮挡/背光（零直接照射）；"
                "可见性按首次交点射线检查，未做域外假设。"
            )
        if float(np.min(mu)) <= 0.0:
            notes.append(
                "本事件窗口内存在背向单元（μ<=0），其直接照射记为 0；"
                "这不代表材料内部无任何响应，而是几何上不直接受照。"
            )
    dA = surface.grid.dx_m * surface.grid.dy_m
    # F is defined per unit *surface* area after projection.  Convert the
    # horizontal cell area to surface area for sloped faces; the old
    # projection-area integral under-counted a complete oblique plane by μ.
    area_factor = np.ones_like(F, dtype=np.float64) if mu is None else (1.0 / np.maximum(n_z, 1e-15))
    intercepted = float(np.sum(F[mask] * area_factor[mask]) * dA)
    total_plane = float(np.sum(F * area_factor) * dA)

    # 尾部截断：裁剪半径之外的能量占比（解析值）。它是数值设置，不是物理阈值。
    tail_frac_analytic = math.exp(-2.0 * (r_cut * r_cut) / (w_used * w_used))
    domain_frac = max(0.0, min(1.0, 1.0 - intercepted / emitted)) if emitted > 0 else 0.0

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
        oblique=not axial,
        dynamic_angle=dynamic,
        mu=mu,
          # nz 只在**真正平面**的轴向快捷路径上可以缺省（n_z ≡ 1，恒等转换）。
          # 斜面即使是轴向光束 + 关闭动态角度，也必须上报 n_z，
          # 否则 solver 端的 Δh=a_n/n_z 换算会被跳过（实测高估 41.42%）。
          nz=(None if axial_flat_shortcut else n_z),
        visibility=vis_stats,
        intercepted_energy_plane_J=total_plane,
    )


def analytic_fixed_geometry_section(surface: Any, center_xy: tuple[float, float], epsilon: float) -> tuple[int, int, int, int]:
    """预计算局部窗口，供批量/重复调用复用（不改变物理语义）。"""
    w = surface.laser.spot_radius_m
    r_cut = cut_radius(w, epsilon)
    iy0, iy1 = _window_bounds(surface.y, center_xy[1], r_cut)
    ix0, ix1 = _window_bounds(surface.x, center_xy[0], r_cut)
    return (iy0, iy1, ix0, ix1)
