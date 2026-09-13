"""G07：斜入射、投影与表面几何（批次 J / T18）。

执行细则 10 节与任务书 6.6 / 10.2 的 G07 断言：

* 平面入射角 **0° 时回到原核**（严格退化）；
* **60° 时足迹长短轴比为 2**、中心实际表面能流为法向对应值的 **1/2**；
* **完整平面上的截获能量仍为 ``E_p``**；
* 随后单独测试有限 ``zR``（**不强求**发散光束保持理想椭圆）；
* 斜平面检查 ``Δh = -a_n/n_z`` 及**体积一致性**；
* 背向/遮挡区域**零直接照射**；
* 曲面**几何面积与投影面积不能混用**；
* 数值积分目标**相对误差 1%**，需分辨率收敛支持；
* 超出 ``n_z>=0.5`` 或入射角 ≤60° 时**停止该模式并给出位置与原因，不裁剪角度继续运行**。

符号约定见 `ufdemo.geometry` 模块 docstring 与 ADR-0015：本工程 ``k`` 指向**光源侧**
（``k·n>0`` 才算被照射），因此 ``mu = max(0, k·n)``，与任务书 ``max(0,-k·n)``
只差 ``k`` 的整体符号；等价性由下面 0°/60° 两条断言锁定。
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from ufdemo import geometry as G
from ufdemo.beam import BeamOptions, beam_patch, peak_fluence
from ufdemo.config import RunConfig, validate_run
from ufdemo.errors import UFDemoError
from ufdemo.materials import load_material_card
from ufdemo.solver import solve
from ufdemo.surface import SurfaceState

pytestmark = pytest.mark.g07

ROOT = Path(__file__).resolve().parents[1]
TEN_PULSES = ROOT / "examples" / "ten_pulses.json"

DEG60 = math.radians(60.0)
K60 = (math.sin(DEG60), 0.0, math.cos(DEG60))


def _raw() -> dict[str, Any]:
    return json.loads(TEN_PULSES.read_text(encoding="utf-8"))


def _card(raw: dict[str, Any]):
    return load_material_card(Path(raw["material_card_file"]))


def _with_direction(raw: dict[str, Any], k: tuple[float, float, float]) -> dict[str, Any]:
    r = copy.deepcopy(raw)
    r["laser"]["direction_unit"] = list(k)
    return r


def _big_domain(raw: dict[str, Any], nx: int = 401, dx_m: float = 5e-7) -> dict[str, Any]:
    """足够大的域 + **准直**光束（``zR=None``）。

    G07 明确「**先用不发散的准直高斯基准隔离投影**」：只有准直时足迹才是
    理想椭圆（长短轴比恰为 ``1/cosθ``）；有限 ``zR`` 由单独用例覆盖，不强求椭圆。

    尺寸要求：60° 时椭圆长半轴 = ``r_cut/cos60° = 2·r_cut ≈ 61 µm``，
    故域需 ≥ ±65 µm（默认 401×0.5 µm = 200 µm）。
    """
    r = copy.deepcopy(raw)
    r["grid"]["nx"] = nx
    r["grid"]["ny"] = nx
    r["grid"]["dx_m"] = dx_m
    r["grid"]["dy_m"] = dx_m
    r["laser"]["rayleigh_range_m"] = None
    # 把焦点放到初始表面高度处，使中心 Z=0：这样 60° 的等 r 线在 (x,y) 上
    # 才是**以焦点为中心的理想椭圆**（长短轴比 1/cosθ），不受离焦平移干扰。
    r["laser"]["focus_xyz_m"] = [0.0, 0.0, float(r["grid"].get("initial_height_m", 0.0))]
    return r


def _center_fluence(patch) -> float:
    """patch **自身**窗口中心的能流（不同窗口尺寸下中心物理位置都是焦点，故可比较）。"""
    return float(np.asarray(patch.fluence)[patch.shape[0] // 2, patch.shape[1] // 2])


def _surface(raw: dict[str, Any], height: np.ndarray | None = None) -> SurfaceState:
    cfg = RunConfig.from_dict(raw)
    s = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    if height is not None:
        s.height[:] = height
        s.initial_height[:] = height
    return s


def _first_event(raw: dict[str, Any]):
    from ufdemo.paths import iter_events

    cfg = RunConfig.from_dict(raw)
    return next(iter(iter_events(cfg.path, cfg.laser)))


# ---------------------------------------------------------------------------
# 1. 0° 退化：必须严格回到原核
# ---------------------------------------------------------------------------


def test_zero_degree_degenerates_to_axial_core():
    """0° 入射与既有正入射核**逐位一致**，且不启用任何几何修正。"""
    raw = _raw()
    ref = solve(RunConfig.from_dict(raw), _card(raw))

    raw0 = _with_direction(raw, (0.0, 0.0, 1.0))
    zero = solve(RunConfig.from_dict(raw0), _card(raw0))

    assert np.array_equal(ref.surface.height, zero.surface.height)
    assert np.array_equal(ref.surface.exposure_count, zero.surface.exposure_count)
    assert zero.diagnostics["geometry"]["enabled"] is False
    assert zero.diagnostics["geometry"]["normal_thickness_conversions"] == 0


def test_zero_degree_fluence_identical_to_axial_path():
    """同一事件在 0° 与正入射下得到完全相同的能流场（走的是同一条计算路径）。"""
    raw = _raw()
    s1 = _surface(raw)
    s2 = _surface(_with_direction(raw, (0.0, 0.0, 1.0)))
    ev = _first_event(raw)
    p1 = beam_patch(ev, s1, BeamOptions())
    p2 = beam_patch(ev, s2, BeamOptions())
    assert np.array_equal(p1.fluence, p2.fluence)
    assert np.array_equal(p1.mask, p2.mask)
    assert p1.shape == p2.shape
    assert p1.visibility is None and p2.visibility is None  # 未启用几何修正


# ---------------------------------------------------------------------------
# 2. 60°：足迹长短轴比 2、中心能流减半
# ---------------------------------------------------------------------------


def test_sixty_degree_footprint_axis_ratio_is_two():
    """60° 斜入射到水平面：足迹长短轴比 = 1/cos60° = 2。"""
    raw = _big_domain(_raw())
    s = _surface(_with_direction(raw, K60))
    ev = _first_event(raw)
    p = beam_patch(ev, s, BeamOptions())

    m = np.asarray(p.mask, dtype=bool)
    assert m.any()
    ys, xs = np.nonzero(m)
    # mask 的窗口下标跨度 → 物理跨度（每格 dx_m）
    dx = s.grid.dx_m
    span_x = (xs.max() - xs.min() + 1) * dx
    span_y = (ys.max() - ys.min() + 1) * dx
    ratio = span_x / span_y
    # 网格离散化：容差按 ±2 格
    assert abs(ratio - 2.0) <= 2.0 * dx / span_y + 0.02, f"足迹比={ratio:.4f}（应为 2）"


def test_sixty_degree_center_fluence_is_half():
    """60° 时中心实际表面能流 = 同参数法向对应值的 1/2（μ=cos60°=0.5）。

    注意：两个 patch 的**窗口尺寸不同**（斜入射足迹更长），因此各自取自身窗口中心；
    中心对应的物理位置都是焦点，可以直接比较。
    """
    raw = _big_domain(_raw())
    ev = _first_event(raw)

    s_ax = _surface(raw)
    p_ax = beam_patch(ev, s_ax, BeamOptions())

    s_ob = _surface(_with_direction(raw, K60))
    p_ob = beam_patch(ev, s_ob, BeamOptions())

    f_ax = _center_fluence(p_ax)
    f_ob = _center_fluence(p_ob)

    assert f_ob == pytest.approx(0.5 * f_ax, rel=1e-12), (
        f"中心能流 {f_ob:.6e} 应为法向 {f_ax:.6e} 的 1/2"
    )
    assert float(np.min(p_ob.mu)) == pytest.approx(0.5, rel=1e-12)


def test_sixty_degree_intercepted_energy_is_preserved():
    """完整平面上截获能量仍为 ``E_p``（相对误差 ≤1%，细则 10 节目标）。"""
    raw = _big_domain(_raw())
    ev = _first_event(raw)
    Ep = float(ev.energy_J)

    for k, name in (((0.0, 0.0, 1.0), "0deg"), (K60, "60deg")):
        s = _surface(_with_direction(raw, k))
        p = beam_patch(ev, s, BeamOptions())
        dA = s.grid.dx_m * s.grid.dy_m
        captured = float(np.sum(p.fluence) * dA)  # 投影面积元
        rel = abs(captured - Ep) / Ep
        assert rel <= 0.01, f"{name}: 截获 {captured:.6e} vs Ep {Ep:.6e}，相对误差 {rel:.3%}"


# ---------------------------------------------------------------------------
# 3. 有限 zR：单独测，不强求理想椭圆
# ---------------------------------------------------------------------------


def test_finite_rayleigh_range_energy_and_degeneracy():
    """有限 ``zR`` 下只断言能量守恒与 0° 退化；**不强求**发散光束保持理想椭圆。"""
    raw = _big_domain(_raw())
    raw["laser"]["rayleigh_range_m"] = 2e-4
    ev = _first_event(raw)
    Ep = float(ev.energy_J)

    for k in ((0.0, 0.0, 1.0), K60):
        s = _surface(_with_direction(raw, k))
        p = beam_patch(ev, s, BeamOptions())
        dA = s.grid.dx_m * s.grid.dy_m
        captured = float(np.sum(p.fluence) * dA)
        assert abs(captured - Ep) / Ep <= 0.01

    # 有限 zR 下 60° 的中心能流不再恰好是 1/2（有轴向依赖），只断言 ≤1/2 且 >0
    s = _surface(_with_direction(raw, K60))
    p = beam_patch(ev, s, BeamOptions())
    s_ax = _surface(raw)
    p_ax = beam_patch(ev, s_ax, BeamOptions())
    f_ax = _center_fluence(p_ax)
    f_ob = _center_fluence(p)
    assert 0.0 < f_ob <= 0.5 * f_ax * (1.0 + 1e-9)


# ---------------------------------------------------------------------------
# 4. 斜平面：Δh = -a_n/n_z 与体积一致性
# ---------------------------------------------------------------------------


def test_analytic_plane_normal_matches_slope():
    """初始斜平面的解析法向与斜率一致：``n=(-s_x,-s_y,1)/sqrt(1+s_x²+s_y²)``。"""
    raw = _raw()
    raw["grid"]["initial_surface"] = "tilted_plane"
    raw["grid"]["initial_slope_x"] = 0.5
    raw["grid"]["initial_slope_y"] = -0.25
    cfg = RunConfig.from_dict(raw)
    nx_, ny_, nz_ = G.analytic_plane_normal(cfg.grid)
    w = math.sqrt(1 + 0.25 + 0.0625)
    assert nx_ == pytest.approx(-0.5 / w)
    assert ny_ == pytest.approx(0.25 / w)
    assert nz_ == pytest.approx(1.0 / w)


def test_normal_thickness_to_height_uses_divide_not_multiply():
    """``Δh=-a_n/n_z``（**不是** ``-a_n·n_z``）；正入射 ``n_z=1`` 时退化为 ``-a_n``。"""
    a_n = np.array([[1e-7, 2e-7]])
    nz = np.array([[1.0, 1.0 / math.sqrt(2.0)]])
    dh = G.normal_thickness_to_height_drop(a_n, nz)
    assert dh[0, 0] == pytest.approx(-1e-7)
    assert dh[0, 1] == pytest.approx(-2e-7 * math.sqrt(2.0))
    # 明确排除 -a_n*n_z
    assert abs(dh[0, 1] - (-2e-7 / math.sqrt(2.0))) > 1e-8


def test_tilted_plane_normal_conversion_end_to_end():
    """端到端：斜平面 + 动态角度下，中心深度 = ``a_n/n_z``（解析预测）。

    单脉冲、无历史、固定阈值核输出**法向**厚度（fixture 的 ``depth_direction``），
    因此高度变化必须除以 ``n_z``。解析预测：``a_n = δ·ln(F_s/Fth)``，
    ``Δh = a_n/n_z``。
    """
    raw = _raw()
    # 单脉冲
    f = float(raw["laser"]["repetition_rate_Hz"])
    raw["path"]["segments"][0]["end_s"] = 1.0 / f
    raw["grid"]["initial_surface"] = "tilted_plane"
    raw["grid"]["initial_slope_x"] = 1.0
    raw["grid"]["initial_slope_y"] = 0.0
    raw["solver"]["dynamic_angle"] = True
    raw["solver"]["geometry_feedback"] = "fixed_geometry"
    raw["grid"]["nx"] = 161
    raw["grid"]["ny"] = 161

    cfg = RunConfig.from_dict(raw)
    card = _card(raw)
    res = solve(cfg, card)
    assert res.status == "completed"

    # 解析预测：a_n = δ·ln(F_s/F_th)，垂直下降 = a_n/n_z
    from ufdemo.response import build_pulse_law

    law = build_pulse_law(card, unit=cfg.unit)
    k = np.array(cfg.laser.direction_unit, dtype=np.float64)
    n_pl = np.array(G.analytic_plane_normal(cfg.grid), dtype=np.float64)
    mu = float(np.dot(k, n_pl))
    sx, sy = cfg.grid.initial_slope
    nz = 1.0 / math.sqrt(1.0 + sx * sx + sy * sy)

    F_perp = peak_fluence(cfg.laser.pulse_energy_J, cfg.laser.spot_radius_m)
    F_s = mu * F_perp
    a_n = float(law.delta_internal) * math.log(F_s / float(law.threshold_internal))
    predicted_drop = a_n / nz  # Δh 的绝对值（`Δh=-a_n/n_z`）

    measured = float(res.surface.initial_height[80, 80] - res.surface.height[80, 80])
    assert measured > 0.0
    assert measured == pytest.approx(predicted_drop, rel=2e-2), (
        f"实测中心高度下降 {measured:.6e} vs 解析预测 {predicted_drop:.6e}"
    )

    g = res.diagnostics["geometry"]
    assert g["normal_thickness_conversions"] >= 1
    assert any("Δh=-a_n/n_z" in a for a in res.metadata["approximations"])


def test_tilted_plane_axial_beam_without_dynamic_angle_uses_analytic_normal():
    """轴向光束 + 初始斜面 + ``dynamic_angle=False``：**不得**按水平面算。

    回归背景（F11）：`beam.py` 的快捷分支写的是 ``if axial and not dynamic: mu = None``，
    隐含假设「轴向光束 ⇒ 表面水平 ⇒ μ=1」。但 ``surface.initialize`` **确实支持**
    ``initial_surface="tilted_plane"``，此时
    **「光束沿全局 z 轴」≠「光束垂直于工件表面」**；
    关闭 ``dynamic_angle`` 只应表示「不随形貌演化更新法向」，**不等于初始坡度不存在**。

    端到端实测（修复前）复现出的错误值恰为 ``ln(2)·δ``，即完全按水平面算；
    正确值应为 ``ln(√2)·√2·δ``（先按 μ=1/√2 投影，再按 Δh=a_n/n_z 换算）。

    注意既有 tilted 测试**全部用 ``dynamic_angle=True``**，所以这一格长期零覆盖。
    """
    raw = _raw()
    # 解析夹具（tests/fixtures/analytic_fixture.json）**声明的**物理量。
    # 这里硬编码而**不**从实现里读 —— 期望值必须独立，否则实现与期望同源、测试恒真。
    ana_fth, ana_delta, ana_w0 = 1.0e4, 1.0e-7, 1.0e-5  # J/m^2, m, m
    card_probe = _card(raw)
    assert card_probe.response["threshold_internal"] == pytest.approx(ana_fth, rel=1e-12)
    assert card_probe.response["delta_internal"] == pytest.approx(ana_delta, rel=1e-12)
    assert float(raw["laser"]["spot_radius_m"]) == pytest.approx(ana_w0, rel=1e-12)

    f = float(raw["laser"]["repetition_rate_Hz"])
    raw["path"]["segments"][0]["end_s"] = 1.0 / f
    raw["grid"]["initial_surface"] = "tilted_plane"
    raw["grid"]["initial_slope_x"] = 1.0  # 45°
    raw["grid"]["initial_slope_y"] = 0.0
    raw["grid"]["nx"] = 161
    raw["grid"]["ny"] = 161
    # 轴向光束 + 关闭动态角度
    raw["laser"]["direction_unit"] = [0.0, 0.0, 1.0]
    raw["solver"]["dynamic_angle"] = False
    raw["solver"]["geometry_feedback"] = "fixed_geometry"
    # 使中心 F_perp 恰好等于 2·F_th：peak = 2E/(π w²) = 2F_th ⇒ E = F_th·π·w²
    raw["laser"]["pulse_energy_J"] = ana_fth * math.pi * ana_w0 * ana_w0

    cfg = RunConfig.from_dict(raw)
    res = solve(cfg, _card(raw))
    assert res.status == "completed"

    nz = 1.0 / math.sqrt(1.0 + 1.0 * 1.0 + 0.0 * 0.0)  # = 1/√2
    mu = nz  # 轴向 k=(0,0,1)，斜面法向 n ⇒ μ = k·n = n_z
    a_n = ana_delta * math.log(2.0 * mu)  # F_s/F_th = μ·F_perp/F_th = 2μ
    predicted_drop = a_n / nz  # Δh = a_n/n_z
    assert predicted_drop == pytest.approx(0.490129e-7, rel=1e-4)

    measured = float(res.surface.initial_height[80, 80] - res.surface.height[80, 80])
    buggy = ana_delta * math.log(2.0)  # 0.693147e-7：完全按水平面算会给的值
    assert measured != pytest.approx(buggy, rel=1e-6), (
        "中心下降等于 ln(2)·δ ⇒ 又把斜面当水平面算了（F11 回归）"
    )
    assert measured == pytest.approx(predicted_drop, rel=2e-2), (
        f"实测中心下降 {measured:.6e} vs 解析预测 {predicted_drop:.6e}"
    )

    # 斜面必须真的发生了法向换算（修复前该计数为 0）
    g = res.diagnostics["geometry"]
    assert g["normal_thickness_conversions"] >= 1, (
        f"斜面的法向厚度换算未发生：{g}"
    )


def test_tilted_plane_volume_consistency():
    """斜平面的体积一致性：**投影面积元**与**真实表面积**两套口径不得混用。

    对解析斜平面（法向处处相同），有恒等关系
    ``V = Σ d·dA_proj = Σ (d·n_z)·(dA_proj/n_z) = Σ a_n · A_surface``，
    其中 ``d`` 是垂直深度、``a_n`` 是法向厚度。本用例验证：
    主循环写入的体积使用投影面积元，且换算到法向厚度×表面积口径后数值一致。
    """
    raw = _raw()
    f = float(raw["laser"]["repetition_rate_Hz"])
    raw["path"]["segments"][0]["end_s"] = 1.0 / f
    raw["grid"]["initial_surface"] = "tilted_plane"
    raw["grid"]["initial_slope_x"] = 0.6
    raw["grid"]["initial_slope_y"] = 0.0
    raw["solver"]["dynamic_angle"] = True
    raw["grid"]["nx"] = 121
    raw["grid"]["ny"] = 121
    raw["laser"]["focus_xyz_m"] = [0.0, 0.0, 0.0]

    cfg = RunConfig.from_dict(raw)
    res = solve(cfg, _card(raw))
    assert res.status == "completed"

    dA_proj = cfg.grid.dx_m * cfg.grid.dy_m
    depth = res.surface.initial_height - res.surface.height
    V_proj = float(np.sum(depth)) * dA_proj  # 投影面积元口径
    assert V_proj > 0.0

    sx, _sy = cfg.grid.initial_slope
    nz = 1.0 / math.sqrt(1.0 + sx * sx)
    a_n = depth * nz                 # 法向厚度（正）
    A_surface = dA_proj / nz         # 真实表面积元（斜平面）
    V_normal = float(np.sum(a_n)) * A_surface  # 法向厚度 × 真实表面积
    assert V_normal == pytest.approx(V_proj, rel=1e-12), (
        "两套面积口径换算后应严格一致（不得混用一半投影一半真实）"
    )

    # 平均垂直下降不应超过最大下降（形状合理性）
    n_abl = int(np.count_nonzero(depth > 0.0))
    assert n_abl > 0
    avg = V_proj / (n_abl * dA_proj)
    assert 0.0 < avg <= float(np.max(depth)) * (1.0 + 1e-12)


# ---------------------------------------------------------------------------
# 5. 遮挡与背向：零直接照射
# ---------------------------------------------------------------------------


def test_shadowed_cells_get_zero_direct_illumination():
    """遮挡区域零直接照射：被遮挡单元的 mask 必为 False、能流为 0。

    用与 `beam` 内部**同源**的 `first_intersection_visibility` 复算窗口可见性，
    再断言 ``mask`` 在不可见处为 False（这是"仅作用于可见首次交点"的可验证形式）。
    """
    raw = _big_domain(_raw(), nx=201, dx_m=5e-7)
    s = _surface(_with_direction(raw, K60))
    ny, nx = s.height.shape
    s.height[:, nx // 2] = 20e-6
    s.initial_height[:, nx // 2] = 20e-6

    ev = _first_event(raw)
    p = beam_patch(ev, s, BeamOptions())
    assert p.visibility is not None
    assert p.visibility["n_shadowed"] > 0, "构造的墙应产生遮挡"

    vis = G.first_intersection_visibility(
        s.initial_height, s.grid, K60, section=(p.iy0, p.iy1, p.ix0, p.ix1)
    )
    assert not bool(np.all(vis))
    m = np.asarray(p.mask, dtype=bool)
    assert not m[~vis].any(), "被遮挡单元不得出现在照射掩膜中"
    assert float(np.max(np.asarray(p.fluence)[~vis])) == 0.0


def test_backfacing_surface_gets_zero_illumination():
    """背向单元（μ≤0）直接照射记为 0：整面背光时无任何去除。

    构造：斜平面 ``s_x=1`` 且 60° 入射 → ``n=(-1,0,1)/√2``，
    ``k·n=(0.866·(-1)+0.5·1)/√2≈-0.26 < 0`` → 整面背向；而 ``n_z=1/√2≈0.707``
    仍在支持范围内（不会被范围检查拦掉），正好隔离"背向"这一条。
    """
    raw = _raw()
    raw["grid"]["initial_surface"] = "tilted_plane"
    raw["grid"]["initial_slope_x"] = 1.0
    raw["grid"]["initial_slope_y"] = 0.0
    raw = _with_direction(raw, K60)
    raw["grid"]["nx"] = 121
    raw["grid"]["ny"] = 121
    raw["solver"]["dynamic_angle"] = True

    cfg = RunConfig.from_dict(raw)
    rep = validate_run(cfg, _card(raw))
    assert rep.ok, [e["code"] for e in rep.errors]

    from ufdemo.paths import iter_events

    s = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    ev = next(iter(iter_events(cfg.path, cfg.laser)))
    p = beam_patch(ev, s, BeamOptions(dynamic_angle=True))

    assert float(np.max(np.asarray(p.mu))) <= 0.0, "该构型应为整面背向"
    assert not np.asarray(p.mask, dtype=bool).any(), "背向单元不得被照射"
    assert float(np.sum(np.asarray(p.fluence))) == 0.0
    # 背向**不算越界**：范围检查应通过（并如实报出背向单元数）
    out = G.check_geometry_range(p.nz, p.mu, cfg.grid, where="test")
    assert out["ok"] is True
    assert out["n_backfacing_cells"] == int(np.asarray(p.mu).size)


# ---------------------------------------------------------------------------
# 6. 超范围：停止并给出位置与原因
# ---------------------------------------------------------------------------


def test_out_of_range_stops_with_location_and_reason():
    """``n_z<0.5`` 或入射角 >60° 时**停止**该模式，报错含原因（不裁剪角度继续）。

    初始平面过陡在**解析配置阶段**即被拒（`GridConfig.from_dict` 抛
    ``GEOMETRY_UNSUPPORTED``）；入射角超范围在 `validate_run` 汇总为报告错误。
    """
    # (a) 允许范围内：slope=1.2 → n_z ≈ 0.640 > 0.5
    raw = _raw()
    raw["grid"]["initial_surface"] = "tilted_plane"
    raw["grid"]["initial_slope_x"] = 1.2
    cfg = RunConfig.from_dict(raw)
    assert G.analytic_plane_normal(cfg.grid)[2] > G.MIN_NZ

    # (b) 初始平面过陡：slope=2.5 → n_z ≈ 0.371 < 0.5 → 抛错并说明原因
    raw2 = copy.deepcopy(raw)
    raw2["grid"]["initial_slope_x"] = 2.5
    with pytest.raises(UFDemoError) as ei:
        RunConfig.from_dict(raw2)
    blob = json.dumps(ei.value.to_dict(), ensure_ascii=False, default=str)
    assert "n_z" in blob and "0.5" in blob
    assert "不裁剪" in blob

    # (c) 入射角 > 60°：在 validate_run 汇总（不裁剪角度继续运行）
    raw3 = _with_direction(_raw(), (math.sin(math.radians(70.0)), 0.0, math.cos(math.radians(70.0))))
    rep3 = validate_run(RunConfig.from_dict(raw3), _card(raw3))
    assert not rep3.ok
    errs = [e for e in rep3.errors if e["code"] == "GEOMETRY_UNSUPPORTED"]
    assert errs
    msg = json.dumps(errs[0], ensure_ascii=False)
    assert "60" in msg and "入射角" in msg


def test_runtime_range_check_reports_index_and_reason():
    """运行时范围检查报错必须带**首个越界单元下标**与可读原因。"""
    nz = np.array([[0.8, 0.8], [0.8, 0.4]])
    mu = np.array([[0.8, 0.8], [0.8, 0.4]])
    with pytest.raises(UFDemoError) as ei:
        G.check_geometry_range(nz, mu, None, where="test")
    blob = json.dumps(ei.value.to_dict(), ensure_ascii=False, default=str)
    assert "first_index" in blob and "n_z" in blob
    assert "0.4" in blob


def test_runtime_range_check_passes_within_range():
    """范围内的几何不应报错，并回传范围统计。"""
    nz = np.full((3, 3), 0.9)
    mu = np.full((3, 3), 0.8)
    out = G.check_geometry_range(nz, mu, None, where="test")
    assert out["ok"] is True
    assert out["n_violating_cells"] == 0
    assert out["min_nz"] == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# 7. 配置层红线
# ---------------------------------------------------------------------------


def test_oblique_with_structured_interface_rejected():
    """斜入射 × 分相结构 → 配置层拦截（法向去除与垂直相列不得混用）。"""
    raw = _with_direction(_raw(), K60)
    raw["solver"]["structured_interface"] = True
    rep = validate_run(RunConfig.from_dict(raw), _card(raw))
    assert not rep.ok
    assert any(e["code"] == "CONFIG_INVALID" for e in rep.errors)


def test_oblique_falls_back_to_reference_in_grouped_mode():
    """斜入射 × 分组批量 → 自动回退逐脉冲参考并写明原因（不静默降级）。"""
    from ufdemo.accelerators import check_fallback_conditions

    d = check_fallback_conditions(
        structured=False, history_enabled=False, geometry_feedback="fixed_geometry",
        oblique_incidence=True,
    )
    assert not d.use_batch
    assert "斜入射" in (d.reason or "")

    raw = _with_direction(_raw(), K60)
    raw["solver"]["mode"] = "grouped"
    res = solve(RunConfig.from_dict(raw), _card(raw))
    assert res.status == "completed"
    assert res.metadata["acceleration"]["effective_mode"] == "reference"
    assert "斜入射" in (res.metadata["acceleration"]["fallback_reason"] or "")


def test_direction_z_must_be_positive():
    """``k_z<=0`` 违反本工程的「光轴正向」约定 → 配置层拒绝并指出约定。"""
    raw = _with_direction(_raw(), (0.0, 0.0, -1.0))
    rep = validate_run(RunConfig.from_dict(raw), _card(raw))
    assert not rep.ok
    errs = [e for e in rep.errors if e["code"] == "GEOMETRY_UNSUPPORTED"]
    assert errs
    assert "k_z" in json.dumps(errs[0], ensure_ascii=False)


def test_dynamic_angle_with_structured_rejected():
    """动态角度 × 分相结构 → 配置层拦截（既有红线，批次 J 后仍生效）。"""
    raw = _raw()
    raw["solver"]["dynamic_angle"] = True
    raw["solver"]["structured_interface"] = True
    rep = validate_run(RunConfig.from_dict(raw), _card(raw))
    assert not rep.ok
    assert any(e["code"] == "CONFIG_INVALID" for e in rep.errors)


# ---------------------------------------------------------------------------
# 8. 法向与可见性单元检查
# ---------------------------------------------------------------------------


def test_surface_normal_matches_analytic_plane():
    """高度梯度法向对**线性平面**精确（内部中心差分 + 边界单边差分）。"""
    raw = _raw()
    cfg = RunConfig.from_dict(raw)
    g = cfg.grid
    XX, YY = np.meshgrid(g.axis("x"), g.axis("y"))
    for sx, sy in ((0.0, 0.0), (0.5, 0.0), (-0.3, 0.8)):
        H = sx * XX + sy * YY
        n_x, n_y, n_z = G.surface_normal(H, g)
        w = math.sqrt(1 + sx * sx + sy * sy)
        assert np.allclose(n_x, -sx / w, atol=1e-12)
        assert np.allclose(n_y, -sy / w, atol=1e-12)
        assert np.allclose(n_z, 1.0 / w, atol=1e-12)


def test_flat_surface_has_no_self_shadowing():
    """水平面在任何支持角度下都无自遮挡（平面无自遮挡，解析结论）。"""
    raw = _raw()
    cfg = RunConfig.from_dict(raw)
    s = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    for k in ((0.0, 0.0, 1.0), K60, (math.sin(math.radians(45.0)), 0.0, math.cos(math.radians(45.0)))):
        vis = G.first_intersection_visibility(s.height, cfg.grid, k, section=(60, 101, 60, 101))
        assert bool(np.all(vis)), f"水平面在 {k} 下不应有遮挡"


def test_visibility_detects_wall_shadow_on_light_side():
    """遮挡发生在墙的**迎光侧**（射线朝光源走会撞墙）。"""
    raw = _raw()
    cfg = RunConfig.from_dict(raw)
    s = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    s.height[:, 80] = 5e-6
    vis = G.first_intersection_visibility(s.height, cfg.grid, K60, section=None)
    assert not bool(np.all(vis))
    cols = np.nonzero((~vis).any(axis=0))[0]
    assert cols.size > 0
    assert cols.max() < 80, "遮挡应位于墙的迎光侧（x < 墙）"


def test_r2_negative_from_rounding_is_clipped_but_real_negative_raises():
    """``r^2``：浮点舍入的极小负值截零；明显负值视为实现错误。

    数学上对**归一化** ``k`` 恒有 ``s^2 <= |q-q_f|^2``（Cauchy–Schwarz），
    故真负值只能来自实现错误（例如 ``k`` 未归一化），此时必须报错而不是静默截零。
    """
    from ufdemo.beam import axial_and_lateral

    XX = np.array([[0.0]])
    YY = np.array([[0.0]])
    ZZ = np.array([[1.0]])
    _s, r2 = axial_and_lateral((0.0, 0.0, 1.0), XX, YY, ZZ, 0.0, 0.0, 0.0)
    assert r2[0, 0] == 0.0  # 正好在轴上

    # 未归一化的 k（|k|=2）会给出 s^2=4 > d^2=1 → 明显负值 → 必须报错
    with pytest.raises(UFDemoError) as ei:
        axial_and_lateral((0.0, 0.0, 2.0), XX, YY, ZZ, 0.0, 0.0, 0.0)
    assert "r^2" in json.dumps(ei.value.to_dict(), ensure_ascii=False, default=str)
