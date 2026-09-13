"""法向、投影与可见性（批次 J / T18，执行细则 9.2、任务书 6.6）。

**符号约定（必须先读，否则会把入射角算反）**

任务书 6.6 给出 ``mu = max(0, -k·n)``，其中 ``k`` 指向传播方向、``n`` 指向**外**法向。
本工程沿用既有内部约定（``beam.py`` 正入射核：``s = h - z_f``，即 ``s = (q-q_f)·k``）：

* ``k = laser.direction_unit``，且要求 ``k_z > 0``（"光轴正向"约定，见 `config.validate_run`）；
* ``n = (-h_x, -h_y, 1) / sqrt(1+h_x^2+h_y^2)``（朝外，与任务书一致）；
* 入射余弦取等价形式 ``mu = max(0, k·n)``。

两者的差别只是 ``k`` 的整体符号：把任务书的 ``k'`` 取为 ``-k``，则 ``-k'·n = k·n``。
采用本形式可保证 **0° 时严格退化到既有正入射核**（G07 的第一条断言），
且不牵动已交付的 345 项测试。符号行为由 G07 的 0°/60° 断言锁定。

**核心公式**

* 轴向距离 ``s = (q - q_f)·k``，横向 ``r^2 = |q - q_f|^2 - s^2``；
* 表面入射能流 ``F_s = mu * F_perp``，**只作用于可见的首次交点**；
* 法向厚度到高度变化的一阶关系 ``dh = -a_n / n_z``（**不是** ``-a_n * n_z``）。

``n_z >= 0.5`` 且入射角 ``<= 60°`` 是**软件数值/展示范围**，不是七类材料的物理边界；
超出即停止该模式并给出位置与原因，**不裁剪角度继续运行**。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import GEOMETRY_UNSUPPORTED, NUMERIC_NONFINITE, UFDemoError

# 软件支持范围（细则 9.2 / 任务书 6.6）：数值与展示范围，不是材料物理边界
MIN_NZ = 0.5
MAX_INCIDENCE_DEG = 60.0

# 可见性射线步进：步长取网格间距的比例；步数上限防止病态几何下退化
DEFAULT_STEP_FACTOR = 0.5
DEFAULT_MAX_STEPS = 2000


@dataclass(frozen=True)
class GeometryRange:
    """几何支持范围（可由配置覆盖，但默认即上表）。"""

    min_nz: float = MIN_NZ
    max_incidence_deg: float = MAX_INCIDENCE_DEG

    @property
    def min_mu(self) -> float:
        """支持范围内的最小入射余弦。"""
        return float(np.cos(np.deg2rad(self.max_incidence_deg)))


# ---------------------------------------------------------------------------
# 法向
# ---------------------------------------------------------------------------


def surface_normal(height: Any, grid: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """表面外法向三分量。

    细则 9.2：「法向通过高度梯度计算，**内部中心差分、边界单边差分**」。
    ``np.gradient`` 恰为此行为（内部二阶中心差分、边界单边差分），
    且对**线性平面**精确 → 可用于「平面解析法向检查」。
    """
    h = np.asarray(height, dtype=np.float64)
    if h.ndim != 2:
        raise UFDemoError(
            NUMERIC_NONFINITE,
            "高度场必须是二维数组",
            field_path="geometry.surface_normal",
            actual=list(h.shape),
        )
    # 轴序：(ny, nx)；dy 对应 axis=0，dx 对应 axis=1
    hy, hx = np.gradient(h, float(grid.dy_m), float(grid.dx_m))
    w = np.sqrt(1.0 + hx * hx + hy * hy)
    return -hx / w, -hy / w, 1.0 / w


def normal_z(height: Any, grid: Any) -> np.ndarray:
    """法向 z 分量 ``n_z = 1/sqrt(1+h_x^2+h_y^2)``（`Δh=-a_n/n_z` 直接用它）。"""
    h = np.asarray(height, dtype=np.float64)
    hy, hx = np.gradient(h, float(grid.dy_m), float(grid.dx_m))
    return 1.0 / np.sqrt(1.0 + hx * hx + hy * hy)


def analytic_plane_normal(grid: Any) -> tuple[float, float, float]:
    """初始平面的**解析**外法向。

    ``flat`` → ``(0, 0, 1)``；``tilted_plane`` → 由 ``initial_slope_x/y`` 直接给出
    ``(-s_x, -s_y, 1)/sqrt(1+s_x^2+s_y^2)``。

    固定角度模式（``dynamic_angle=false``）用它而不是逐点梯度：解析值**无离散误差**，
    且语义清楚——"角度固定"意味着法向取自名义平面，不随烧蚀形变更新。
    """
    sx = float(getattr(grid, "initial_slope_x", 0.0) or 0.0)
    sy = float(getattr(grid, "initial_slope_y", 0.0) or 0.0)
    w = math.sqrt(1.0 + sx * sx + sy * sy)
    return (-sx / w, -sy / w, 1.0 / w)


def incidence_cosine(direction_unit: Any, nx: Any, ny: Any, nz: Any) -> np.ndarray:
    """入射余弦 ``mu = max(0, k·n)``（符号约定见模块 docstring）。"""
    k = np.asarray(direction_unit, dtype=np.float64)
    if k.shape != (3,):
        raise UFDemoError(
            NUMERIC_NONFINITE,
            "direction_unit 必须是长度 3 的矢量",
            field_path="laser.direction_unit",
            actual=list(np.shape(k)),
        )
    mu = k[0] * np.asarray(nx) + k[1] * np.asarray(ny) + k[2] * np.asarray(nz)
    return np.maximum(0.0, mu)


def incidence_angle_deg(mu: Any) -> np.ndarray:
    """入射角（度）``arccos(mu)``；``mu<=0``（背向）记 90°。"""
    m = np.clip(np.asarray(mu, dtype=np.float64), 0.0, 1.0)
    return np.rad2deg(np.arccos(m))


def check_geometry_range(
    nz: Any,
    mu: Any,
    grid: Any,
    *,
    geometry_range: GeometryRange | None = None,
    where: str = "geometry",
) -> dict[str, Any]:
    """检查是否落在软件支持范围内；超出即报错并给出**位置与原因**。

    细则 9.2：「超过 ``n_z>=0.5`` 或入射角≤60°的软件范围时**停止该模式**并给出
    位置与原因，**不裁剪角度继续运行**。」

    注意：**背向单元（``mu<=0``）不算越界**——它们几何上就不受直接照射（零照射），
    是合法情形，不触发"停止"。只有"确实被照到但角度过陡（``0<μ<cos60°``）"
    才属于超出软件范围。
    """
    rng = geometry_range or GeometryRange()
    nz_a = np.asarray(nz, dtype=np.float64)
    mu_a = np.asarray(mu, dtype=np.float64)

    bad_nz = nz_a < rng.min_nz
    backfacing = mu_a <= 0.0
    bad_mu = (mu_a < rng.min_mu) & (~backfacing)
    bad = bad_nz | bad_mu
    n_bad = int(np.count_nonzero(bad))
    if n_bad == 0:
        angle = incidence_angle_deg(mu_a)
        lit = mu_a > 0.0
        return {
            "ok": True,
            "n_violating_cells": 0,
            "n_backfacing_cells": int(np.count_nonzero(backfacing)),
            "min_nz": float(np.min(nz_a)) if nz_a.size else None,
            "max_incidence_deg": float(np.max(angle[lit])) if np.any(lit) else None,
            "min_mu": float(np.min(mu_a[lit])) if np.any(lit) else None,
        }

    iy, ix = np.unravel_index(int(np.argmax(bad)), bad.shape)
    worst_nz = float(nz_a[iy, ix])
    worst_mu = float(mu_a[iy, ix])
    worst_angle = float(incidence_angle_deg(np.asarray(worst_mu)))
    reasons = []
    if bad_nz[iy, ix]:
        reasons.append(f"n_z={worst_nz:.4g} < {rng.min_nz:g}")
    if bad_mu[iy, ix]:
        reasons.append(f"入射角={worst_angle:.2f}° > {rng.max_incidence_deg:g}°")

    raise UFDemoError(
        GEOMETRY_UNSUPPORTED,
        "表面几何超出软件支持范围，已停止该模式（未裁剪角度继续）",
        field_path=where,
        actual={
            "n_violating_cells": n_bad,
            "first_index": [int(iy), int(ix)],
            "n_z": worst_nz,
            "incidence_deg": worst_angle,
        },
        requirement=(
            f"n_z >= {rng.min_nz:g} 且入射角 <= {rng.max_incidence_deg:g}°"
            "（软件数值/展示范围，不是材料物理边界）"
        ),
        suggestion=(
            f"首个越界单元位于 (iy={iy}, ix={ix})：" + "；".join(reasons)
            + "。请减小表面倾角/入射角，或改用正入射。"
        ),
    )


# ---------------------------------------------------------------------------
# 投影
# ---------------------------------------------------------------------------


def project_fluence(fluence_perp: Any, mu: Any, *, visible: Any = None) -> np.ndarray:
    """表面入射能流 ``F_s = mu * F_perp``；仅作用于可见的首次交点。

    任务书 6.6：「上式自然包含平面斜入射的椭圆投影，**不再额外重复缩放光斑/乘同一余弦**」。
    因此本函数**只做一次** ``mu`` 缩放；调用方不得再乘一次余弦。
    """
    f = np.asarray(fluence_perp, dtype=np.float64)
    m = np.asarray(mu, dtype=np.float64)
    if m.shape != f.shape:
        raise UFDemoError(
            NUMERIC_NONFINITE,
            "入射余弦与能流形状不匹配",
            field_path="geometry.project_fluence",
            actual={"fluence": list(f.shape), "mu": list(m.shape)},
            requirement="两者形状一致",
        )
    out = m * f
    if visible is not None:
        v = np.asarray(visible, dtype=bool)
        if v.shape != out.shape:
            raise UFDemoError(
                NUMERIC_NONFINITE,
                "可见性掩膜与能流形状不匹配",
                field_path="geometry.project_fluence",
                actual={"fluence": list(out.shape), "visible": list(v.shape)},
            )
        out = np.where(v, out, 0.0)
    return out


def normal_thickness_to_vertical_depth(a_n: Any, nz: Any) -> np.ndarray:
    """法向厚度 → **垂直去除量**（正值）：``d = a_n / n_z``。

    高度场主循环里 ``cand_arr`` 表示"去除量"（正值 = 向下挖掉），
    因此这里返回**正**的垂直深度；不要与 `normal_thickness_to_height_drop`（负的 Δh）混用。
    """
    a, z = _check_thickness_inputs(a_n, nz)
    return a / z


def normal_thickness_to_height_drop(a_n: Any, nz: Any) -> np.ndarray:
    """法向厚度 → **高度变化**（负值）：``dh = -a_n / n_z``。

    任务书 6.6 明确：「若核明确输出法向后退厚度 ``a_n``，固定 ``(x,y)`` 的一阶高度
    更新为 ``dh=-a_n/n_z``，**不是** ``-a_n*n_z``」。

    推导（一阶）：表面沿外法向 ``n=(-h_x,-h_y,1)/W`` 向内平移 ``a_n`` 后，
    对同一 ``(x,y)`` 有 ``h' - h = -a_n * W = -a_n / n_z``（因 ``n_z = 1/W``）。
    正入射 ``n_z=1`` 时退化为 ``dh=-a_n``。
    """
    return -normal_thickness_to_vertical_depth(a_n, nz)


def _check_thickness_inputs(a_n: Any, nz: Any) -> tuple[np.ndarray, np.ndarray]:
    a = np.asarray(a_n, dtype=np.float64)
    z = np.asarray(nz, dtype=np.float64)
    if z.shape != a.shape:
        raise UFDemoError(
            NUMERIC_NONFINITE,
            "法向厚度与 n_z 形状不匹配",
            field_path="geometry.normal_thickness_to_height_drop",
            actual={"a_n": list(a.shape), "n_z": list(z.shape)},
        )
    if np.any(z <= 0.0):
        raise UFDemoError(
            GEOMETRY_UNSUPPORTED,
            "n_z <= 0：表面与光轴平行或背向，无法定义法向厚度转换",
            field_path="geometry.normal_thickness_to_height_drop",
            actual=float(np.min(z)),
            requirement="n_z > 0（本工程支持 n_z >= 0.5）",
            suggestion="减小表面倾角；该关系要求表面与传播方向不平行。",
        )
    return a, z


# ---------------------------------------------------------------------------
# 可见性（首次交点）
# ---------------------------------------------------------------------------


def _axis_origin(grid: Any, which: str) -> float:
    """轴的最小坐标。网格以 ``center`` 为中心，``axis[0] = center - (n-1)/2*d``。"""
    if which == "x":
        n, c, d = int(grid.nx), float(grid.center_x_m), float(grid.dx_m)
    else:
        n, c, d = int(grid.ny), float(grid.center_y_m), float(grid.dy_m)
    return c - (n - 1) / 2.0 * d


def _sample_height_bilinear(
    height: np.ndarray, nx: int, ny: int, x0: float, y0: float,
    dx: float, dy: float, x: float, y: float,
) -> float | None:
    """双线性采样高度；越界返回 ``None``（表示射线已离开计算域）。

    只做**线性插值**，不做外推：域外一律返回 ``None``，避免对未知区域编造高度。
    """
    fx = (x - x0) / dx
    fy = (y - y0) / dy
    if fx < 0.0 or fy < 0.0 or fx > nx - 1 or fy > ny - 1:
        return None
    if nx < 2 or ny < 2:  # pragma: no cover - 退化网格
        return float(height[min(max(int(round(fy)), 0), ny - 1), min(max(int(round(fx)), 0), nx - 1)])
    ix0 = min(int(np.floor(fx)), nx - 2)
    iy0 = min(int(np.floor(fy)), ny - 2)
    tx = fx - ix0
    ty = fy - iy0
    h00 = float(height[iy0, ix0])
    h01 = float(height[iy0, ix0 + 1])
    h10 = float(height[iy0 + 1, ix0])
    h11 = float(height[iy0 + 1, ix0 + 1])
    return (
        h00 * (1 - tx) * (1 - ty)
        + h01 * tx * (1 - ty)
        + h10 * (1 - tx) * ty
        + h11 * tx * ty
    )


def first_intersection_visibility(
    height: Any,
    grid: Any,
    direction_unit: Any,
    *,
    section: tuple[int, int, int, int] | None = None,
    global_height: Any | None = None,
    step_factor: float = DEFAULT_STEP_FACTOR,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> np.ndarray:
    """首次交点可见性掩膜：``True`` = 该表面点可直接被光照射到。

    做法（细则 9.2「曲面必须具备受验证的无自遮挡约束或**首次交点可见性**算法」）：
    从每个表面点 ``q=(x,y,h)`` 沿 **``+k``** 步进（本工程约定 ``k`` 指向光源侧：
    ``k·n>0`` 才算被照射，见模块 docstring），若射线在离开计算域之前**落到表面之下**，
    则该点被上游表面遮挡。

    性质：

    * 水平面上射线不下降（``k_z=1`` 时沿 +z 上升）→ 恒可见，平面无自遮挡；
    * 背光面（``mu<=0``）由 `project_fluence` 归零，与可见性无关；
    * 射线离开计算域即停止，**不做域外假设**（不假设域外是真空或无限高）。

    复杂度 ``O(窗口单元数 × 步数)``，但**按步向量化**（每步一次数组运算，而非逐单元
    Python 循环），并无条件走「完全平坦 → 无自遮挡」的解析快速路径；
    仍只对局部照射窗口调用（`beam.py` 内），不对全网格无条件启用。
    """
    # Keep the full surface for ray sampling when the requested result is a
    # local window.  A local height crop alone cannot detect upstream points
    # outside that crop and can also be indexed with global dimensions.
    h = np.asarray(global_height if global_height is not None else height, dtype=np.float64)
    k = np.asarray(direction_unit, dtype=np.float64)
    to_source = k  # 朝光源侧（k·n>0 的约定下即为 +k）

    if section is None:
        iy0, iy1, ix0, ix1 = 0, int(grid.ny), 0, int(grid.nx)
    else:
        iy0, iy1, ix0, ix1 = section
        iy0, iy1 = max(0, int(iy0)), min(int(grid.ny), int(iy1))
        ix0, ix1 = max(0, int(ix0)), min(int(grid.nx), int(ix1))

    n_rows, n_cols = iy1 - iy0, ix1 - ix0
    visible = np.ones((n_rows, n_cols), dtype=bool)
    if n_rows <= 0 or n_cols <= 0:
        return visible
    if to_source[2] <= 0.0:
        # 朝光源方向不朝上 → 无"上游"概念；方向约定由 config.validate_run 拦截
        return visible

    nx, ny = int(grid.nx), int(grid.ny)
    dx, dy = float(grid.dx_m), float(grid.dy_m)
    x0a, y0a = _axis_origin(grid, "x"), _axis_origin(grid, "y")
    step = float(step_factor) * min(dx, dy)
    if step <= 0.0:  # pragma: no cover - 网格校验已保证
        return visible

    h_win = h[iy0:iy1, ix0:ix1]
    # 解析快速路径只能在**全局**表面近似平坦时使用。局部窗口平坦并不
    # 排除窗口外上游高墙；斜入射射线可能先穿过该高墙后才离开窗口。
    # 因此这里必须检查 global_height（若提供），而不是仅检查 h_win。
    if float(np.max(h)) - float(np.min(h)) <= step:
        return visible

    dz = float(to_source[2]) * step
    dsx, dsy = float(to_source[0]) * step, float(to_source[1]) * step
    h_max = float(np.max(h))

    xs = x0a + np.arange(ix0, ix1, dtype=np.float64) * dx
    ys = y0a + np.arange(iy0, iy1, dtype=np.float64) * dy
    CX, CY = np.meshgrid(xs, ys)
    cx = CX.ravel().copy()
    cy = CY.ravel().copy()
    cz = h_win.ravel().copy()
    blocked = np.zeros(cx.size, dtype=bool)
    active = np.ones(cx.size, dtype=bool)

    fx_lo, fy_lo = 0.0, 0.0
    fx_hi, fy_hi = float(nx - 1), float(ny - 1)

    for _ in range(int(max_steps)):
        if not active.any():
            break
        idx = np.nonzero(active)[0]
        cx[idx] += dsx
        cy[idx] += dsy
        cz[idx] += dz

        fx = (cx[idx] - x0a) / dx
        fy = (cy[idx] - y0a) / dy
        inside = (fx >= fx_lo) & (fy >= fy_lo) & (fx <= fx_hi) & (fy <= fy_hi)
        if not inside.any():
            active[idx] = False
            continue
        leave = ~inside
        ii = idx[inside]
        fxi, fyi = fx[inside], fy[inside]
        ix0i = np.clip(np.floor(fxi).astype(np.int64), 0, nx - 2)
        iy0i = np.clip(np.floor(fyi).astype(np.int64), 0, ny - 2)
        txi, tyi = fxi - ix0i, fyi - iy0i
        h00 = h[iy0i, ix0i]
        h01 = h[iy0i, ix0i + 1]
        h10 = h[iy0i + 1, ix0i]
        h11 = h[iy0i + 1, ix0i + 1]
        hs = (
            h00 * (1 - txi) * (1 - tyi)
            + h01 * txi * (1 - tyi)
            + h10 * (1 - txi) * tyi
            + h11 * txi * tyi
        )
        below = cz[ii] < hs
        # 高度使用 SI/归一化内部单位，固定加 1.0 会在微米或无量纲网格
        # 下变成不可能达到的巨大阈值；当前步长上移 dz 即足够判定已越过
        # 全局最高点。
        high = cz[ii] > h_max + dz
        if below.any():
            blocked[ii[below]] = True
        # 退出活跃集：离开计算域 / 已远高于全场最高点 / 已判遮挡。
        # 注意索引空间：`leave` 对应 `idx`（整个活跃集合），`high`/`below` 对应 `ii`（域内子集）
        if leave.any():
            active[idx[leave]] = False
        if high.any():
            active[ii[high]] = False
        if below.any():
            active[ii[below]] = False

    return (visible.ravel() & ~blocked).reshape(n_rows, n_cols)


def visibility_summary(visible: Any, grid: Any) -> dict[str, Any]:
    """可见性统计（面积与占比）。"""
    v = np.asarray(visible, dtype=bool)
    n = int(v.size)
    n_vis = int(np.count_nonzero(v))
    dA = float(grid.dx_m) * float(grid.dy_m)
    return {
        "n_cells": n,
        "n_visible": n_vis,
        "n_shadowed": n - n_vis,
        "shadowed_fraction": (float(n - n_vis) / n) if n else 0.0,
        "visible_area_internal": n_vis * dA,
        "shadowed_area_internal": (n - n_vis) * dA,
    }
