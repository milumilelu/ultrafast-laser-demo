"""G06：分相查询、颗粒/铺层几何与跨相界面截断。

依据执行细则第 8 节（合成复合材料实现细则）、第 10 节 G06 行与任务书 3.3 节。

四条核心断言（细则 10 节 G06 行）：

* **同相合并等价**——相邻同相铺层预先合并，合并前后几何等价；
* **不同相不跳过**——``next_different_interface`` 返回第一个真实相界面，既不
  越过界面，也不在"相没变"的人为层界上假截断；
* **不拆伪脉冲**——一个真实脉冲对每个单元只调用**当前暴露相**的一个响应核；
* **固定种子重现**——同种子几何与统计逐位一致，异种子给出不同几何。

三条红线（细则 7 节 CFPR/铝基 SiC 行、8 节末）：

* 相不得借用其它材料卡（块体 4H-SiC 卡不当颗粒相标定）；
* 整体等效阈值不得拆给分相（CFRP 的 Fth1 ≠ 树脂/纤维各自阈值）；
* 分相截断与动态角度互斥，且二者的一致性校验在**配置层**拦截。

截断语义（细则 8 节规则 4）：被截断的候选量名称必须是「未应用候选去除体积」，
**不是**剩余能量；目标体积分数与实际体积分数分别报告。
"""

from __future__ import annotations

import copy
import json

import numpy as np
import pytest

from conftest import ROOT, load_example, make_config
from ufdemo.beam import BeamOptions, beam_patch
from ufdemo.config import validate_run
from ufdemo.errors import (
    CONDITION_MISMATCH,
    CONFIG_INVALID,
    MATERIAL_CAPABILITY_MISSING,
    RESPONSE_SEMANTICS_INVALID,
    UFDemoError,
)
from ufdemo.materials import load_material_card
from ufdemo.paths import iter_events
from ufdemo.response import HistoryState
from ufdemo.solver import solve
from ufdemo.structure import (
    LayeredStructure,
    ParticleStructure,
    PhaseSpec,
    PlyLayer,
    assert_phase_threshold_not_split,
    build_phase,
    load_structure,
)
from ufdemo.surface import SurfaceState

pytestmark = pytest.mark.g06

ALSIC = "alsic_particle_composite.json"
CFRP = "cfrp_laminated_ply.json"

# 两张示例卡的整体等效阈值（J/m^2）与合成参考能流 F_ref
CFRP_OVERALL_THRESHOLD_F_REF = 8400.0 / 1.0e4  # = 0.84 F_ref


def _phase(name: str, pid: int, thr: float, delta: float, role: str = "layer") -> PhaseSpec:
    return PhaseSpec(name=name, phase_id=pid, role=role, threshold_internal=thr, delta_internal=delta)


def _card(name: str):
    raw = load_example(name)
    return raw, load_material_card(ROOT / raw["material_card_file"])


# ---------------------------------------------------------------------------
# 1. 同相合并等价
# ---------------------------------------------------------------------------


def test_same_phase_merge_is_equivalent():
    """相邻同相铺层合并后，几何必须与"预合并的单层"逐位一致。"""
    phases = [_phase("A", 1, 1.0, 0.5), _phase("B", 2, 2.0, 0.2)]
    layers = [
        PlyLayer(thickness=1.0, matrix_phase="A"),
        PlyLayer(thickness=2.0, matrix_phase="A"),  # 与上一层同指纹 → 合并
        PlyLayer(thickness=1.5, matrix_phase="B"),
    ]
    st = LayeredStructure(layers, phases, z_top=0.0, seed=7)
    assert st.original_layer_count == 3
    assert st.merged_layer_count == 2
    assert st.total_thickness == pytest.approx(4.5)  # 合并只并计数，不改总厚

    ref = LayeredStructure(
        [PlyLayer(thickness=3.0, matrix_phase="A"), PlyLayer(thickness=1.5, matrix_phase="B")],
        phases,
        z_top=0.0,
        seed=7,
    )
    zs = np.array([0.0, -0.5, -1.0, -2.9, -3.1, -4.4])
    xs = np.zeros_like(zs)
    np.testing.assert_array_equal(st.phase_at(xs, xs, zs), ref.phase_at(xs, xs, zs))
    np.testing.assert_allclose(
        st.next_different_interface(xs, xs, zs), ref.next_different_interface(xs, xs, zs)
    )
    assert any("预先合并" in n for n in st.notes)


def test_same_phase_seam_does_not_truncate_but_real_interface_does():
    """0°/90° 双层：同相列不因人为层界截断；异相列返回真实界面距离。"""
    phases = [_phase("A", 1, 1.0, 0.5), _phase("B", 2, 2.0, 0.2)]
    layers = [
        PlyLayer(
            thickness=1.0, matrix_phase="A", fiber_phase="B",
            fiber_angle_deg=0.0, fiber_volume_fraction=0.5, fiber_width=0.4,
        ),
        PlyLayer(
            thickness=1.0, matrix_phase="A", fiber_phase="B",
            fiber_angle_deg=90.0, fiber_volume_fraction=0.5, fiber_width=0.4,
        ),
    ]
    st = LayeredStructure(layers, phases, z_top=0.0, seed=3)
    # 指纹不同（铺层角不同）→ 不合并，因此"同一 (x,y) 上下两相相同"的场景必须由
    # next_different_interface 自己识别，而不是靠预合并兜底。
    assert st.merged_layer_count == 2

    pid_b = st.phase_id_of("B")
    # (0.5, 0.0)：0°→t=y=0 纤维；90°→t=-x=-0.5→mod 0.3 纤维 ⇒ 上下同相
    assert st.phase_at(0.5, 0.0, 0.0) == pid_b
    assert st.phase_at(0.5, 0.0, -1.0 - 1e-9) == pid_b
    assert st.next_different_interface(0.5, 0.0, 0.0) == pytest.approx(float("inf"))

    # (0.5, 0.5)：0°→t=0.5 基体；90°→t=-0.5 纤维 ⇒ 层底是真实相界面
    pid_a = st.phase_id_of("A")
    assert st.phase_at(0.5, 0.5, 0.0) == pid_a
    assert st.phase_at(0.5, 0.5, -1.0 - 1e-6) == pid_b
    assert st.next_different_interface(0.5, 0.5, 0.0) == pytest.approx(1.0, rel=1e-9)
    # 不跳过：界面之上任意深度都仍是当前相
    for z in np.linspace(0.0, -1.0 + 1e-6, 33):
        assert st.phase_at(0.5, 0.5, z) == pid_a


def test_particle_interface_is_analytic_and_not_skipped():
    """颗粒沿 -z 的进入/离开界面都必须解析命中，不越过、不跳过。"""
    phases = [_phase("matrix", 1, 1.0, 0.5, role="matrix"), _phase("part", 2, 2.0, 0.2, role="particle")]
    st = ParticleStructure(
        matrix_phase="matrix", particle_phase="part", phases=phases,
        radius=0.5, z_top=0.0, x_range=(-2.0, 2.0), y_range=(-2.0, 2.0), depth=4.0,
        seed=123, target_volume_fraction=0.05,
    )
    assert st.n_particles > 0
    cx, cy, cz, r = float(st._cx[0]), float(st._cy[0]), float(st._cz[0]), float(st._r[0])
    pid_p = st.phase_id_of("part")
    pid_m = st.phase_id_of("matrix")

    assert st.phase_at(cx, cy, cz) == pid_p
    # 从球顶上方入射 → 到"进入颗粒"的界面距离
    assert st.next_different_interface(cx, cy, cz + r + 0.3) == pytest.approx(0.3, rel=1e-9)
    # 球内 → 到"离开颗粒"的界面距离
    assert st.next_different_interface(cx, cy, cz) == pytest.approx(r, rel=1e-9)
    # 相变位置与 next_different_interface 自洽（不跳过）
    assert st.phase_at(cx, cy, cz + r + 1e-6) == pid_m
    assert st.phase_at(cx, cy, cz - r + 1e-9) == pid_p
    assert st.phase_at(cx, cy, cz - r - 1e-6) == pid_m
    # 远离所有颗粒的列：无界面
    assert st.next_different_interface(1.99, 1.99, 0.0) == pytest.approx(float("inf"))
    assert st.phase_at(1.99, 1.99, 0.0) == pid_m


# ---------------------------------------------------------------------------
# 2. 固定种子重现
# ---------------------------------------------------------------------------


def test_fixed_seed_reproduces_particle_geometry():
    phases = [_phase("matrix", 1, 1.0, 0.5, role="matrix"), _phase("part", 2, 2.0, 0.2, role="particle")]
    kw = dict(
        matrix_phase="matrix", particle_phase="part", phases=phases, radius=0.5,
        z_top=0.0, x_range=(-2.0, 2.0), y_range=(-2.0, 2.0), depth=4.0,
        target_volume_fraction=0.06,
    )
    a = ParticleStructure(seed=2026, **kw)
    b = ParticleStructure(seed=2026, **kw)
    c = ParticleStructure(seed=2027, **kw)

    np.testing.assert_array_equal(a._cx, b._cx)
    np.testing.assert_array_equal(a._cy, b._cy)
    np.testing.assert_array_equal(a._cz, b._cz)
    assert a.summary()["n_particles_placed"] == b.summary()["n_particles_placed"]
    assert a.summary()["nominal_volume_fraction"] == b.summary()["nominal_volume_fraction"]
    assert a.summary()["algorithm_version"] == b.summary()["algorithm_version"]
    # 固定种子必须有约束力：换种子应给出不同几何
    assert a.n_particles != c.n_particles or bool(np.any(c._cx != a._cx))


def test_fixed_seed_run_is_reproducible():
    raw, card = _card(ALSIC)
    r1 = solve(make_config(raw), card)
    r2 = solve(make_config(copy.deepcopy(raw)), card)
    assert r1.ok and r2.ok, (r1.errors, r2.errors)

    s1, s2 = r1.metadata["structure"], r2.metadata["structure"]
    assert s1["seed"] == s2["seed"] == raw["structure"]["seed"]
    assert s1["n_particles_placed"] == s2["n_particles_placed"]
    assert s1["volume_fraction"]["actual_volume_fraction"] == s2["volume_fraction"]["actual_volume_fraction"]
    assert (
        r1.diagnostics["phase"]["initial_phase_cell_counts"]
        == r2.diagnostics["phase"]["initial_phase_cell_counts"]
    )
    assert r1.statistics["center_depth_internal"] == pytest.approx(r2.statistics["center_depth_internal"])


# ---------------------------------------------------------------------------
# 3. 不拆伪脉冲：一个真实脉冲只调用当前相的一个核
# ---------------------------------------------------------------------------


def _one_event_cfrp():
    """构造"仅一个事件"的 CFRP 铺层算例，返回 (cfg, card, 结构)。"""
    raw, card = _card(CFRP)
    raw = copy.deepcopy(raw)
    raw["path"]["segments"][0]["end_s"] = 5.0e-4  # f=1000 → 只有 t=0 一个事件
    cfg = make_config(raw)
    assert validate_run(cfg, card).ok
    structure = load_structure(cfg, card)
    return cfg, card, structure


def test_single_event_uses_only_the_current_phase_law():
    """中心列（全纤维）一次应用的去除量必须**恰好等于**单一纤维核的候选量。

    若实现把同一个物理脉冲拆成两相各施加一次（伪脉冲），该列会额外叠加树脂核的
    增量，与单一核的预期值不再相等。
    """
    cfg, card, structure = _one_event_cfrp()
    result = solve(cfg, card)
    assert result.ok, result.errors
    assert result.diagnostics["events"]["n_events"] == 1

    laws = structure.laws(unit_mode=cfg.unit.mode)
    fid = structure.phase_id_of("fiber")
    rid = structure.phase_id_of("resin")
    # 两相核必须不同，否则本测试没有区分力
    assert laws[fid].delta_internal != laws[rid].delta_internal

    # 在**初始表面**上复算光斑与候选量（与求解器同一条光束路径）
    surface0 = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    surface0.initialize_phases(structure)
    event = next(iter(iter_events(cfg.path, cfg.laser)))
    patch = beam_patch(
        event, surface0,
        BeamOptions(geometry_feedback=cfg.solver.geometry_feedback, tail_epsilon=cfg.solver.tail_epsilon),
    )

    cy = cx = cfg.grid.ny // 2
    row = cy - patch.iy0
    assert surface0.phase_id[cy, cx] == fid
    F_center = float(patch.fluence[row, cx - patch.ix0])
    expected = float(
        laws[fid].increment(np.array([[F_center]]), HistoryState(exposure_count=None), card).values[0, 0]
    )
    assert expected > 0.0
    applied = float(result.surface.initial_height[cy, cx] - result.surface.height[cy, cx])
    assert applied == pytest.approx(expected, rel=1e-9)


def test_each_phase_column_applies_only_its_own_law():
    """同一脉冲内，基体列与纤维列各自只应用自己的核，互不叠加。"""
    cfg, card, structure = _one_event_cfrp()
    result = solve(cfg, card)
    laws = structure.laws(unit_mode=cfg.unit.mode)
    rid = structure.phase_id_of("resin")
    fid = structure.phase_id_of("fiber")

    surface0 = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    surface0.initialize_phases(structure)
    event = next(iter(iter_events(cfg.path, cfg.laser)))
    patch = beam_patch(event, surface0, BeamOptions())

    # 顶层为 0° 铺层：沿 y 的条纹使 y∈[width, period) 的列为基体。
    # 取 y = 0.5 L_ref（中心行下 5 个单元，dy = 0.1 L_ref）落到基体条纹。
    cy = cfg.grid.ny // 2
    ry = cy + 5
    cx = cfg.grid.nx // 2
    assert surface0.phase_id[ry, cx] == rid, "选定行在光斑中心处应是基体相"

    row = ry - patch.iy0
    frow = patch.fluence[row]
    pid_row = surface0.phase_id[ry, patch.ix0:patch.ix1]
    cand_idx = np.where((pid_row == rid) & (frow > 0.0))[0]
    assert cand_idx.size > 0, "光斑内应同时存在被照射的基体列"
    k = int(cand_idx[len(cand_idx) // 2])
    col = patch.ix0 + k

    F_resin = float(frow[k])
    expected_resin = float(
        laws[rid].increment(np.array([[F_resin]]), HistoryState(exposure_count=None), card).values[0, 0]
    )
    # 该基体列的到下一相界面距离 > 候选量 ⇒ 无截断，应用量 = 单一基体核候选量
    assert structure.next_different_interface(
        float(surface0.x[col]), float(surface0.y[ry]), float(surface0.height[ry, col])
    ) > expected_resin
    applied_resin = float(result.surface.initial_height[ry, col] - result.surface.height[ry, col])
    assert applied_resin == pytest.approx(expected_resin, rel=1e-9)

    # 对照：中心行 y=0 处顶层 0° 条纹 t=y=0 ⇒ 全行纤维相
    assert surface0.phase_id[cy, cx] == fid
    frow_center = patch.fluence[cy - patch.iy0]
    F_center = float(frow_center[cx - patch.ix0])
    expected_fiber = float(
        laws[fid].increment(np.array([[F_center]]), HistoryState(exposure_count=None), card).values[0, 0]
    )
    applied_fiber = float(result.surface.initial_height[cy, cx] - result.surface.height[cy, cx])
    assert applied_fiber == pytest.approx(expected_fiber, rel=1e-9)
    # 两相结果不同，说明分派确实按相区分而非"全用同一个核"
    assert applied_resin != pytest.approx(applied_fiber)


# ---------------------------------------------------------------------------
# 4. 截断语义与诊断
# ---------------------------------------------------------------------------


def test_cross_phase_truncation_is_named_unapplied_candidate_not_residual_energy():
    raw, card = _card(CFRP)
    result = solve(make_config(raw), card)
    assert result.ok, result.errors

    rem = result.diagnostics["removal"]
    assert "unapplied_candidate_removal_volume_internal" in rem
    # 不得把它表述成"剩余能量/残余量"
    keys = " ".join(rem.keys())
    assert "residual" not in keys and "energy" not in keys

    # 细则 8 节规则 4：四个统计量都要记录
    for key in ("clipped_events", "n_clipped_cells", "phase_switch_cells", "phase_switch_events"):
        assert key in rem
    assert rem["clipped_events"] > 0, "该铺层算例应确实发生跨相截断"
    assert rem["unapplied_fraction_of_candidate"] > 0.0
    assert rem["applied_volume_internal"] <= rem["candidate_volume_internal"] + 1e-12

    note = result.metadata["structure"]["truncation_note"]
    assert "未应用候选去除体积" in note
    assert "剩余" in note  # "不是剩余热量" 的明确否定
    # 相标签切换的措辞必须说"下一个真实脉冲"，不得暗示本脉冲对新相重复施加
    joined = " ".join(result.warnings)
    assert "下一个真实脉冲" in joined
    assert result.statistics.get("structured_interface") is True


def test_target_and_actual_volume_fraction_reported_separately():
    raw, card = _card(ALSIC)
    result = solve(make_config(raw), card)
    assert result.ok, result.errors

    summary = result.metadata["structure"]
    assert summary["target_volume_fraction"] == pytest.approx(0.32)
    vf = summary["volume_fraction"]
    assert vf["target_volume_fraction"] == pytest.approx(0.32)
    assert vf["actual_volume_fraction"] is not None
    assert vf["volume_fraction_method"] == "grid_sample"
    assert vf["n_samples"] > 0
    # 实际比例单独给出，且不强制等于目标
    assert set(vf["actual_volume_fraction"]) == {"Al_matrix", "SiC_particle"}
    assert 0.0 < sum(vf["actual_volume_fraction"].values()) <= 1.0 + 1e-12
    assert "不必然" in vf["note"] or "分别报告" in vf["note"]
    # 名义体积分数与实际体积分数是**两个**字段
    assert "nominal_volume_fraction" in vf


def test_layered_volume_fraction_is_analytic_and_separate_from_target():
    raw, card = _card(CFRP)
    result = solve(make_config(raw), card)
    assert result.ok, result.errors
    vf = result.metadata["structure"]["volume_fraction"]
    assert vf["volume_fraction_method"] == "analytic_from_ply_pattern"
    assert vf["target_volume_fraction"] is None  # 铺层示例未声明目标比例
    assert vf["actual_volume_fraction"] == {"fiber": pytest.approx(0.55)}


# ---------------------------------------------------------------------------
# 5. 红线：相不得借用其它材料卡 / 不得沿用整体阈值来源
# ---------------------------------------------------------------------------


def test_red_line_phase_cannot_borrow_another_material_card():
    raw, _ = _card(ALSIC)
    cfg = make_config(raw)
    with pytest.raises(UFDemoError) as ei:
        build_phase(
            {
                "name": "SiC_particle", "role": "particle",
                "threshold_over_F_ref": 2.35, "delta_over_L_ref": 0.2,
                "material_id": "sic_4h_cface_1035nm_multishot",
            },
            phase_id=2, unit=cfg.unit,
        )
    assert ei.value.code == MATERIAL_CAPABILITY_MISSING
    assert "材料卡" in ei.value.message


def test_red_line_forbidden_phase_response_source_rejected():
    raw, _ = _card(CFRP)
    cfg = make_config(raw)
    with pytest.raises(UFDemoError) as ei:
        build_phase(
            {
                "name": "resin", "role": "matrix",
                "threshold_over_F_ref": 0.7, "delta_over_L_ref": 0.35,
                "response_source": "parent_card_threshold",
            },
            phase_id=1, unit=cfg.unit,
        )
    assert ei.value.code == MATERIAL_CAPABILITY_MISSING
    assert "内联" in (ei.value.requirement or "")


def test_red_line_legacy_delta_over_delta_ref_is_rejected():
    """旧字段 ``delta_over_delta_ref`` 必须被拒绝：它会把深度整体放大 100 倍。"""
    raw, _ = _card(CFRP)
    cfg = make_config(raw)
    with pytest.raises(UFDemoError) as ei:
        build_phase(
            {"name": "x", "role": "matrix", "threshold_over_F_ref": 0.7, "delta_over_delta_ref": 1.0},
            phase_id=1, unit=cfg.unit,
        )
    assert ei.value.code == RESPONSE_SEMANTICS_INVALID
    assert "delta_over_L_ref" in (ei.value.requirement or "")

    ok = build_phase(
        {"name": "x", "role": "matrix", "threshold_over_F_ref": 0.7, "delta_over_L_ref": 0.35},
        phase_id=1, unit=cfg.unit,
    )
    assert ok.delta_internal == pytest.approx(0.35)


def test_red_line_synthetic_material_card_uses_delta_over_L_ref():
    """合成材料卡不得残留旧字段（否则卡与配置会出现 100 倍深度差）。"""
    card = json.loads(
        (ROOT / "data" / "materials" / "_synthetic_demo_isotropic.json").read_text(encoding="utf-8")
    )
    resp = card["response"]
    assert "delta_over_L_ref" in resp
    assert "delta_over_delta_ref" not in resp


def test_red_line_overall_threshold_cannot_be_split_to_phases():
    raw, card = _card(CFRP)
    cfg = make_config(raw)
    # 父卡整体阈值 8400 J/m^2 → 0.84 F_ref（必须先换算到同一尺度才比得出来）
    split = _phase("resin", 1, CFRP_OVERALL_THRESHOLD_F_REF, 0.35, role="matrix")
    with pytest.raises(UFDemoError) as ei:
        assert_phase_threshold_not_split(split, card, cfg.unit)
    assert ei.value.code == MATERIAL_CAPABILITY_MISSING
    # 各自标定的分相阈值不受影响
    assert_phase_threshold_not_split(_phase("resin", 1, 0.70, 0.35, role="matrix"), card, cfg.unit)


def test_red_line_overall_threshold_split_rejected_at_config_layer():
    raw, card = _card(CFRP)
    raw = copy.deepcopy(raw)
    raw["structure"]["phases"][0]["threshold_over_F_ref"] = CFRP_OVERALL_THRESHOLD_F_REF
    rep = validate_run(make_config(raw), card)
    assert not rep.ok
    assert MATERIAL_CAPABILITY_MISSING in {e["code"] for e in rep.errors}


# ---------------------------------------------------------------------------
# 6. 红线：配置层拦截（互斥 / 开关一致 / 结构类型匹配）
# ---------------------------------------------------------------------------


def test_red_line_structured_and_dynamic_angle_are_mutually_exclusive():
    raw, card = _card(CFRP)
    raw = copy.deepcopy(raw)
    raw["solver"]["dynamic_angle"] = True
    rep = validate_run(make_config(raw), card)
    assert not rep.ok
    joined = " ".join(e["message"] for e in rep.errors)
    assert "不得同时启用" in joined
    assert CONFIG_INVALID in {e["code"] for e in rep.errors}


def test_structure_and_toggle_must_be_consistent():
    raw, card = _card(CFRP)
    # 配了结构却关闭开关 → 拒绝（不允许静默忽略）
    raw_a = copy.deepcopy(raw)
    raw_a["solver"]["structured_interface"] = False
    rep_a = validate_run(make_config(raw_a), card)
    assert not rep_a.ok
    assert any("structured_interface" in e["message"] for e in rep_a.errors)

    # 打开开关却没有结构 → 拒绝
    raw_b = copy.deepcopy(raw)
    raw_b["structure"] = {"structure_type": "homogeneous"}
    rep_b = validate_run(make_config(raw_b), card)
    assert not rep_b.ok
    assert CONFIG_INVALID in {e["code"] for e in rep_b.errors}


def test_structure_type_must_match_card_declaration():
    raw, card = _card(CFRP)
    raw = copy.deepcopy(raw)
    raw["structure"] = {
        "structure_type": "particle_composite",
        "seed": 1,
        "target_volume_fraction": 0.2,
        "phases": raw["structure"]["phases"],
        "particles": {
            "matrix_phase": "resin", "particle_phase": "fiber",
            "mean_diameter_m": 3e-5, "depth_m": 5e-5,
        },
    }
    rep = validate_run(make_config(raw), card)
    assert not rep.ok
    assert CONDITION_MISMATCH in {e["code"] for e in rep.errors}


# ---------------------------------------------------------------------------
# 7. 两个交付示例端到端
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [ALSIC, CFRP])
def test_delivered_structure_examples_validate_and_solve(name):
    raw, card = _card(name)
    rep = validate_run(make_config(raw), card)
    assert rep.ok, rep.errors
    result = solve(make_config(raw), card)
    assert result.ok, result.errors
    assert result.diagnostics["phase"]["structured_interface"] is True
    assert result.diagnostics["phase"]["final_phase_cell_counts"]
    assert result.metadata["structure"]["seed"] == raw["structure"]["seed"]
    assert result.metadata["structure"]["algorithm_version"]
