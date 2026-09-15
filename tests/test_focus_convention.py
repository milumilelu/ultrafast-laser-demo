"""焦平面约定：**恒为加工前的原始上表面**（`fixed_original_surface`）。

为什么单独立一组测试：这条约定原先只写在 `data/config/shared_experiment_background.json`
里**当作文档**，代码里没有任何地方校验它 —— 而 `SolverConfig.geometry_feedback` 的
**默认值是 `fixed_geometry`**，那个模式的语义是「离焦恒为 0、光斑跟着表面走」，
**等价于逐层重新对焦**，正好与约定相反。用户 2026-09-15 明确：

> 焦点就固定在上表面。

于是把约定做成**三重约束**：

1. **声明层**：`solver.focus_strategy` 只接受 `fixed_original_surface`，别的值一律拒绝
   （本项目**不提供**逐层 Z 调整 / 焦点跟随表面 / 动态补偿，任务书 §6）；
2. **构造层**：`PredictionSpec.initial_height_m` 同时写进 `grid.initial_height_m`
   与路径每一段的 z ⇒ 「焦平面 = 原始表面」**按构造成立**，不依赖它是 0；
3. **校验层**：`validate_run` 复核「每一段端点的 z 都等于 `grid.initial_height_m`」，
   不等就**失败关闭**（挡住"声明对、路径却把焦点放别处"）。

⚠️ 与 `geometry_feedback` 是**两个不同的开关**，不得混为一谈：
`focus_strategy` 定焦平面**位置**；`axial_defocus` 提供**被动离焦**
`w(d) = w0·sqrt(1+(d/zR)²)`。停用动态入射角**不得**连带停用轴向离焦。
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from conftest import ROOT, load_example, load_material_card, make_config
from ufdemo.config import SolverConfig, validate_run
from ufdemo.errors import CONFIG_INVALID, UFDemoError


# --- 1. 声明层：只开放 fixed_original_surface -------------------------------

def test_default_focus_strategy_is_fixed_original_surface():
    cfg = SolverConfig.from_dict({})
    assert cfg.focus_strategy == "fixed_original_surface"
    assert cfg.to_dict()["focus_strategy"] == "fixed_original_surface"


@pytest.mark.parametrize(
    "bad",
    ["layer_refocus", "follow_surface", "dynamic_compensation", "refocus_per_layer", ""],
)
def test_other_focus_strategies_are_rejected(bad):
    """逐层对焦/焦点跟随表面**未实现**：声明即拒，不做静默降级。"""
    with pytest.raises(UFDemoError) as exc:
        SolverConfig.from_dict({"focus_strategy": bad})
    assert exc.value.code == CONFIG_INVALID
    msg = json.dumps(exc.value.to_dict(), ensure_ascii=False)
    assert "fixed_original_surface" in msg


# --- 2. 校验层：路径 z 必须等于原始表面 -------------------------------------

def _example_with_z(z_m: float):
    raw = load_example("analytic_single_pulse.json")
    cfg = make_config(raw)
    seg0 = cfg.path.segments[0]
    object.__setattr__(seg0, "start_xyz_m", (seg0.start_xyz_m[0], seg0.start_xyz_m[1], z_m))
    object.__setattr__(seg0, "end_xyz_m", (seg0.end_xyz_m[0], seg0.end_xyz_m[1], z_m))
    card = load_material_card(ROOT / raw["material_card_file"])
    return cfg, card


def test_path_z_equal_to_initial_surface_passes_and_is_noted():
    cfg, card = _example_with_z(0.0)
    report = validate_run(cfg, card)
    assert report.ok, report.errors
    assert any("焦平面" in n for n in report.notes), report.notes


def test_path_z_below_initial_surface_fails_closed():
    """把焦点放到槽底（z ≠ 原始表面）必须**失败关闭**，不能静默算。"""
    cfg, card = _example_with_z(-20e-6)          # 焦点下移 20 µm
    report = validate_run(cfg, card)
    assert not report.ok
    codes = {e["code"] for e in report.errors}
    assert CONFIG_INVALID in codes
    text = json.dumps(report.errors, ensure_ascii=False)
    assert "焦平面" in text and "原始上表面" in text
    # 报错要说清是**哪一段**、实际值多少 —— 否则用户没法改
    assert "path.segments" in text and "-2e-05" in text


# --- 3. 构造层：装配路径按构造成立 -------------------------------------------

def _planning_cfg(initial_height_m: float = 0.0):
    from ufdemo.calibration import ExperimentRow, PredictionSpec, build_row_config

    row = ExperimentRow(sample_id="focus", pulse_duration_fs=223.0, repetition_rate_kHz=20.0,
                        scan_speed_mm_s=50.0, hatch_spacing_um=4.0, pass_count=2, mean_depth_um=1.0)
    spec = PredictionSpec(material_card_file="tests/fixtures/analytic_fixture.json",
                          window_um=60.0, dx_um=1.0, machining_region_um=30.0,
                          initial_height_m=float(initial_height_m))
    return build_row_config(row, spec=spec)


def test_planning_path_builds_focus_on_the_original_surface():
    """非零原始表面也必须被跟随：焦平面 = 原始表面，而不是硬编码的 0。"""
    z0 = -3.5e-6                                  # 工件表面比基准低 3.5 µm
    cfg = _planning_cfg(z0)
    assert float(cfg.grid.initial_height_m) == pytest.approx(z0)
    assert len(cfg.path.segments) > 1             # 弓字形 + 换向段
    for seg in cfg.path.segments:
        assert float(seg.start_xyz_m[2]) == pytest.approx(z0, abs=1e-15)
        assert float(seg.end_xyz_m[2]) == pytest.approx(z0, abs=1e-15)
    # 多段（含多层）时焦平面**不随层数变化** —— 没有逐层 Z 调整
    zs = {round(float(s.start_xyz_m[2]), 15) for s in cfg.path.segments}
    assert zs == {round(z0, 15)}


def test_flat_initial_surface_still_uses_zero_plane():
    cfg = _planning_cfg(0.0)
    assert float(cfg.grid.initial_height_m) == 0.0
    assert {float(s.start_xyz_m[2]) for s in cfg.path.segments} == {0.0}


# --- 4. 行为层：焦点固定 ⇒ 存在**被动离焦** ---------------------------------

def test_focus_fixed_at_original_surface_produces_passive_defocus():
    """焦点固定在上表面 ⇒ 表面下降应使光斑变大（被动离焦），而不是光斑恒定。

    两个断言合起来才说明问题：
    * 初始平表面上 `axial_distance ≈ 0` —— 焦点**就落在**原始表面上；
    * 打深之后窗口内最大光斑 **> w0** —— 出现被动离焦（`axial_defocus`）。
    """
    import ufdemo.beam as B
    import ufdemo.solver as S
    from ufdemo.calibration import ExperimentRow, PredictionSpec, build_row_config
    from ufdemo.materials import MaterialSpec
    from ufdemo.solver import solve

    bl = json.loads(
        (ROOT / "data" / "baselines" / "alsic_223fs_candidate.json").read_text(encoding="utf-8")
    )
    ov = {"kind": "log_fixed", "output_semantics": "event_depth_increment",
          "fluence_basis": "incident_peak_fluence", "depth_direction": "surface_normal",
          "threshold_J_m2": bl["thresholdJm2"], "delta_m": bl["deltaM"]}
    raw = json.loads((ROOT / "tests/fixtures/analytic_fixture.json").read_text(encoding="utf-8"))
    raw["response"] = {**(raw.get("response") or {}), **ov}
    mat = MaterialSpec.from_dict(raw)

    first = {}
    ratios: list[float] = []
    _orig = B.beam_patch

    def patched(ev, su, op):
        q = _orig(ev, su, op)
        if ev.index == 0:
            first["axial_distance"] = q.axial_distance
            first["spot_radius"] = q.spot_radius
        w0 = su.laser.spot_radius_m
        ratios.append(float(q.spot_radius) / float(w0))
        return q

    row = ExperimentRow(sample_id="focus", pulse_duration_fs=223.0, repetition_rate_kHz=20.0,
                        scan_speed_mm_s=50.0, hatch_spacing_um=4.0, pass_count=1, mean_depth_um=1.0)
    spec = PredictionSpec(material_card_file="tests/fixtures/analytic_fixture.json",
                          window_um=200.0, dx_um=1.0, machining_region_um=100.0,
                          response_override=ov)
    cfg = build_row_config(row, spec=spec)
    assert cfg.solver.focus_strategy == "fixed_original_surface"
    assert cfg.solver.geometry_feedback == "axial_defocus"

    B.beam_patch = patched
    S.beam_patch = patched
    res = solve(cfg, mat)
    B.beam_patch = _orig
    S.beam_patch = _orig
    assert res.ok

    # ① 焦点在原始表面上：初始平表面的轴向统计距离 ≈ 0
    assert first["axial_distance"] == pytest.approx(0.0, abs=1e-12)
    assert first["spot_radius"] == pytest.approx(cfg.laser.spot_radius_m, rel=1e-12)
    # ② 打深后出现被动离焦：窗口内最大光斑显著大于 w0
    assert max(ratios) > 1.5, f"未观察到被动离焦（max w/w0 = {max(ratios):.3f}）"


# --- 5. 元数据可审计 ---------------------------------------------------------

def test_metadata_records_focus_convention():
    from ufdemo.solver import solve
    from conftest import run_example

    _, res = run_example("analytic_single_pulse.json")
    focus = res.metadata["focus"]
    assert focus["strategy"] == "fixed_original_surface"
    assert focus["layer_refocus"] is False
    assert focus["z_m"] == 0.0
    assert focus["geometry_feedback"] in ("fixed_geometry", "axial_defocus")
    assert focus["passive_defocus"] is (focus["geometry_feedback"] == "axial_defocus")
    assert "原始上表面" in focus["note"]
