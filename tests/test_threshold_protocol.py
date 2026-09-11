"""G09：受限阈值协议（批次 H / T14）与阈值图层的界面口径。

依据执行细则第 11.3 节、任务书 6.3/7 节与批次 H 交付边界。

被测的不是「阈值数值」而是**协议边界**，因为超阈标记是本项目最容易出错的观测量：

1. **只按本事件入射能流判超阈**——累计剂量绝不参与比较。用一个可判别的构造证明：
   同一位置连打两发、每发都是 ``0.6 Fth``，累计剂量 ``1.2 Fth`` 已超阈，掩膜必须
   保持"未超阈"；若实现退化成拿累计量比较，本测试立刻失败。
2. **配置层拦截**——非法能流基准（累计/平均）在 ``RunConfig.from_dict`` 阶段报错，
   不靠界面禁用。
3. **多候选不静默取默认**——SiC 的改性阈值（2.35 J/cm²）与结构变化阈值
   （4.97 J/cm²）是两回事，未显式给 ``candidate_index`` 时必须报不可用而不是挑一个。
4. **不产生深度**——协议不改高度场，``used_for_depth`` 恒为 False，未开启时不返回
   全 0 假数组。
5. **落盘与回放同源**——观测量随快照/表面落盘，历史读取走同一条诊断构造路径。
"""

from __future__ import annotations

import numpy as np
import pytest

from conftest import DATA_MATERIALS, ROOT, make_config
from ufdemo.config import RunConfig, ThresholdProtocolConfig
from ufdemo.errors import CONFIG_INVALID, RESOURCE_BUDGET_EXCEEDED, UFDemoError
from ufdemo.materials import load_material_card
from ufdemo.solver import solve
from ufdemo.surface import SurfaceState
from ufdemo.thresholds import (
    THRESHOLD_FLUENCE_BASES,
    assert_basis_is_event,
    assert_not_removal,
    build_threshold_protocol,
    classify_exceedance,
    reject_cumulative_flux_arg,
)

pytestmark = pytest.mark.g09

SIC = "sic_4h_cface_1035nm_multishot.json"
YSZ_STATIC = "zirconia_ysz_static_aps8ysz.json"
DIAMOND_400FS = "diamond_scd_cvd_1030nm_400fs.json"
CFRP = "cfrp_t700_yb01_800nm.json"
INCONEL_N10 = "inconel718_1030nm_n10.json"
GLASS_CERAMIC = "glass_ceramic_unbranded_1030nm.json"

SIC_MODIFICATION_J_M2 = 23500.0
SIC_STRUCTURAL_J_M2 = 49700.0


def _card(name: str):
    return load_material_card(DATA_MATERIALS / name)


def _sic_threshold_raw(**overrides) -> dict:
    """一份 SI 的 threshold_only 运行配置（SiC，200 kHz，5 个事件）。

    能流峰值按 ``2E/(pi w0^2)`` 约 6.4e4 J/m²，高于两个候选阈值，确保掩膜非空。
    """
    raw = {
        "schema_version": "1.0",
        "case_id": "g09_sic_threshold_only",
        "label": "g09_sic_threshold",
        "run_mode": "threshold_only",
        "unit_system": "SI",
        "material_id": "sic_4h_cface_1035nm_multishot",
        "material_card_file": "data/materials/sic_4h_cface_1035nm_multishot.json",
        "seed": 1,
        "grid": {
            "nx": 21,
            "ny": 21,
            "dx_m": 2e-6,
            "dy_m": 2e-6,
            "center_x_m": 0.0,
            "center_y_m": 0.0,
            "origin": "cell_center",
            "initial_surface": "flat",
            "initial_height_m": 0.0,
        },
        "laser": {
            "wavelength_m": 1.035e-6,
            "pulse_duration_s": 3e-13,
            "pulse_energy_J": 1e-5,
            "repetition_rate_Hz": 200000.0,
            "spot_radius_m": 1e-5,
            "focus_xyz_m": [0.0, 0.0, 0.0],
            "direction_unit": [0.0, 0.0, 1.0],
            "power_measurement_location": "sample_surface",
            "parameter_sources": ["synthetic_definition"],
        },
        "path": {
            "t0_s": 0.0,
            "time_tolerance_s": 1e-12,
            "segments": [
                {
                    "segment_id": 0,
                    "pass_id": 0,
                    "start_s": 0.0,
                    "end_s": 2.5e-5,
                    "start_xyz_m": [0.0, 0.0, 0.0],
                    "end_xyz_m": [0.0, 0.0, 0.0],
                    "speed_m_s": 0.0,
                    "laser_on": True,
                    "label": "point",
                }
            ],
        },
        "solver": {
            "mode": "reference",
            "geometry_feedback": "fixed_geometry",
            "history_enabled": False,
            "tail_epsilon": 1e-8,
            "memory_budget_bytes": 1073741824,
            "budget_safety_factor": 1.5,
            "cancel_check_interval": 256,
            "acceleration": "off",
            "multiline_incubation": False,
            "structured_interface": False,
        },
        "output": {
            "snapshot_policy": "events",
            "snapshot_events": [0],
            "max_snapshots": 4,
            "max_snapshot_bytes": 134217728,
        },
    }
    for dotted, value in overrides.items():
        target = raw
        parts = dotted.split(".")
        for p in parts[:-1]:
            target = target[p]
        target[parts[-1]] = value
    return raw


# ---------------------------------------------------------------------------
# 1. 配置层：能流基准与默认关闭
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "basis", ["cumulative_fluence", "cumulative_dose", "mean_fluence", "total_fluence"]
)
def test_config_layer_rejects_cumulative_fluence_basis(basis):
    """累计/平均基准必须在配置层被拒，而不是界面禁用。"""
    with pytest.raises(UFDemoError) as ei:
        ThresholdProtocolConfig.from_dict({"enabled": True, "fluence_basis": basis})
    assert ei.value.code == CONFIG_INVALID
    assert "fluence_basis" in str(ei.value)


def test_config_layer_rejects_unregistered_basis():
    with pytest.raises(UFDemoError) as ei:
        ThresholdProtocolConfig.from_dict({"enabled": True, "fluence_basis": "per_shot_dose"})
    assert ei.value.code == CONFIG_INVALID


def test_cumulative_basis_rejected_through_runconfig():
    raw = _sic_threshold_raw()
    raw["threshold_protocol"] = {"enabled": True, "fluence_basis": "cumulative_fluence"}
    with pytest.raises(UFDemoError) as ei:
        RunConfig.from_dict(raw)
    assert ei.value.code == CONFIG_INVALID


def test_threshold_protocol_defaults_off_and_roundtrips():
    cfg = RunConfig.from_dict(_sic_threshold_raw())
    assert cfg.threshold.enabled is False
    assert cfg.threshold.fluence_basis == "per_event_incident"
    assert cfg.threshold.candidate_index is None
    # 内部口径快照必须带上协议选择（raw 回放只回显用户输入，故取 include_internal）
    dumped = cfg.to_dict(include_internal=True)["threshold_protocol"]
    assert dumped["enabled"] is False
    assert dumped["fluence_basis"] == "per_event_incident"
    # 用户确实写了 threshold_protocol 时，raw 回放必须原样保留
    raw = _sic_threshold_raw()
    raw["threshold_protocol"] = {"enabled": True, "candidate_index": 1}
    assert RunConfig.from_dict(raw).to_dict()["threshold_protocol"]["candidate_index"] == 1


def test_event_basis_is_the_only_registered_basis():
    assert THRESHOLD_FLUENCE_BASES == ("per_event_incident",)
    assert assert_basis_is_event("per_event_incident") == "per_event_incident"
    for bad in ("cumulative_fluence", "mean_fluence", "total_fluence"):
        with pytest.raises(UFDemoError):
            assert_basis_is_event(bad)
    with pytest.raises(UFDemoError) as ei:
        reject_cumulative_flux_arg("cumulative_fluence")
    assert ei.value.code == CONFIG_INVALID


# ---------------------------------------------------------------------------
# 2. 协议构造：默认关闭、多候选、缺阈值
# ---------------------------------------------------------------------------


def test_disabled_protocol_reports_unavailable_not_zeroed():
    """未开启时如实报不可用：不给掩膜，也不返回全 0 假数组。"""
    p = build_threshold_protocol(_card(SIC), enabled=False, candidate_index=0)
    assert p.available is False
    assert "未开启" in p.reason
    assert classify_exceedance(p, np.full((3, 3), 1e9), np.ones((3, 3), dtype=bool)) is None
    assert_not_removal(p)
    d = p.to_dict()
    assert d["used_for_depth"] is False
    assert d["fluence_basis"] == "per_event_incident"
    assert d["output_semantics"] == "threshold_only"


def test_multi_candidate_requires_explicit_index():
    """SiC 的改性/结构变化两个候选不得静默取默认。"""
    card = _card(SIC)
    p = build_threshold_protocol(card, enabled=True)
    assert p.available is False
    assert "candidate_index" in p.reason
    assert p.n_candidates == 2

    p0 = build_threshold_protocol(card, enabled=True, candidate_index=0)
    p1 = build_threshold_protocol(card, enabled=True, candidate_index=1)
    assert p0.available and p1.available
    assert p0.threshold_internal == pytest.approx(SIC_MODIFICATION_J_M2)
    assert p1.threshold_internal == pytest.approx(SIC_STRUCTURAL_J_M2)
    assert p0.observable_name == "single_pulse_modification_threshold"
    assert p1.observable_name == "single_pulse_structural_transformation_threshold"
    assert p0.source_kind == "threshold_candidate" and p0.source_index == 0
    assert p1.source_index == 1
    assert p0.used_for_depth is False


def test_candidate_index_out_of_range_rejected():
    with pytest.raises(UFDemoError) as ei:
        build_threshold_protocol(_card(SIC), enabled=True, candidate_index=5)
    assert ei.value.code == CONFIG_INVALID
    with pytest.raises(UFDemoError):
        build_threshold_protocol(_card(SIC), enabled=True, candidate_index=True)


def test_single_candidate_and_single_card_threshold():
    """单候选（YSZ 静态）走候选；仅卡内 response 阈值（金刚石）走 response。"""
    ysz = build_threshold_protocol(_card(YSZ_STATIC), enabled=True)
    assert ysz.available is True
    assert ysz.source_kind == "threshold_candidate"
    assert ysz.n_candidates == 1
    assert ysz.threshold_internal == pytest.approx(14830.0)
    assert ysz.observable_name == "single_pulse_threshold"

    dia = build_threshold_protocol(_card(DIAMOND_400FS), enabled=True)
    assert dia.available is True
    assert dia.source_kind == "response"
    assert dia.n_candidates == 0
    assert dia.threshold_internal == pytest.approx(82000.0)
    assert dia.threshold_kind == "single_pulse"


def test_material_without_threshold_is_unavailable_not_zero():
    """缺阈值保持 null：既不按 0 处理，也不借用相近材料补值。"""
    p = build_threshold_protocol(_card(GLASS_CERAMIC), enabled=True)
    assert p.available is False
    assert "null" in p.reason
    assert p.threshold_internal is None


def test_multipulse_fixed_point_threshold_is_blocked():
    """红线：高温合金 N=10 定点阈值不得当作单脉冲阈值。"""
    p = build_threshold_protocol(_card(INCONEL_N10), enabled=True)
    assert p.available is False
    assert "多脉冲" in p.reason
    assert p.threshold_internal is None  # 不退回 response 里的 1400 J/m²
    p0 = build_threshold_protocol(_card(INCONEL_N10), enabled=True, candidate_index=0)
    assert p0.available is False and "多脉冲" in p0.reason


def test_multipulse_fitted_fth1_is_first_pulse_endpoint():
    """``multipulse_fitted_Fth1`` 是 N=1 端点，属单脉冲口径，不得被误拦。"""
    p = build_threshold_protocol(_card(CFRP), enabled=True)
    assert p.available is True
    assert p.threshold_kind == "multipulse_fitted_Fth1"
    assert p.threshold_internal == pytest.approx(8400.0)
    assert p.used_for_depth is False


# ---------------------------------------------------------------------------
# 3. 硬约束：只按本事件能流分类、且不产生深度
# ---------------------------------------------------------------------------


def test_classification_uses_this_event_fluence_not_cumulative():
    """可判别构造：两发各 0.6 Fth（累计 1.2 Fth）不得被判为超阈。"""
    cfg = make_config(_sic_threshold_raw())
    surface = SurfaceState.initialize(
        cfg.grid, cfg.laser, history_enabled=False, threshold_protocol=True
    )
    p = build_threshold_protocol(_card(SIC), enabled=True, candidate_index=0)
    thr = p.threshold_internal
    section = (0, 5, 0, 5)
    mask = np.ones((5, 5), dtype=bool)
    below = np.full((5, 5), 0.6 * thr)

    h_before = surface.height.copy()
    for _ in range(2):
        surface.accumulate_illumination(section, below, mask)
        exceed = classify_exceedance(p, below, mask)
        assert exceed is not None and not exceed.any()
        assert surface.accumulate_threshold(section, exceed) == 0

    # 累计剂量确实已超过阈值——若实现拿累计量比较，下面的断言就会失败
    assert np.all(surface.cumulative_fluence[:5, :5] > thr)
    assert not surface.threshold_exceeded_mask.any()
    assert int(surface.threshold_exceedance_count.sum()) == 0
    # 协议不产生去除量：高度场逐位不变
    np.testing.assert_array_equal(surface.height, h_before)

    # 单发超过阈值时才点亮，且计数按「单元·事件」累加
    above = np.full((5, 5), 1.2 * thr)
    surface.accumulate_illumination(section, above, mask)
    exceed = classify_exceedance(p, above, mask)
    assert surface.accumulate_threshold(section, exceed) == 25
    assert surface.threshold_exceeded_mask[:5, :5].all()
    assert int(surface.threshold_exceedance_count.sum()) == 25
    np.testing.assert_array_equal(surface.height, h_before)


def test_classify_ignores_cells_outside_mask():
    cfg = make_config(_sic_threshold_raw())
    p = build_threshold_protocol(_card(SIC), enabled=True, candidate_index=0)
    fluence = np.full((4, 4), 10.0 * p.threshold_internal)
    mask = np.zeros((4, 4), dtype=bool)
    mask[0, 0] = True
    out = classify_exceedance(p, fluence, mask)
    assert int(np.count_nonzero(out)) == 1


def test_accumulate_threshold_without_protocol_raises():
    """协议未开启时不得"悄悄丢弃"：数组为 None 且累计必须报错。"""
    cfg = make_config(_sic_threshold_raw())
    surface = SurfaceState.initialize(
        cfg.grid, cfg.laser, history_enabled=False, threshold_protocol=False
    )
    assert surface.threshold_exceeded_mask is None
    assert surface.threshold_exceedance_count is None
    with pytest.raises(UFDemoError) as ei:
        surface.accumulate_threshold((0, 3, 0, 3), np.ones((3, 3), dtype=bool))
    assert ei.value.code == RESOURCE_BUDGET_EXCEEDED


def test_snapshot_omits_threshold_arrays_when_disabled():
    cfg = make_config(_sic_threshold_raw())
    off = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    assert "threshold_exceeded_mask" not in off.to_snapshot(event_index=0, time_s=0.0)
    on = SurfaceState.initialize(
        cfg.grid, cfg.laser, history_enabled=False, threshold_protocol=True
    )
    snap = on.to_snapshot(event_index=0, time_s=0.0)
    assert "threshold_exceeded_mask" in snap and "threshold_exceedance_count" in snap


# ---------------------------------------------------------------------------
# 4. 端到端：求解器、落盘、回读
# ---------------------------------------------------------------------------


def test_solver_records_threshold_observation_when_enabled():
    raw = _sic_threshold_raw()
    raw["threshold_protocol"] = {"enabled": True, "candidate_index": 0}
    cfg = make_config(raw)
    res = solve(cfg, _card(SIC))

    assert res.status == "completed"
    assert res.removal_available is False  # threshold_only 不给去除量
    thr = res.diagnostics["threshold"]
    assert thr["protocol_enabled"] is True
    assert thr["protocol_available"] is True
    assert thr["fluence_basis"] == "per_event_incident"
    assert thr["threshold_internal"] == pytest.approx(SIC_MODIFICATION_J_M2)
    assert thr["used_for_depth"] is False
    assert thr["n_exceeded_cell_events"] > 0
    assert thr["exceeded_cells_final"] > 0

    mask = res.surface.threshold_exceeded_mask
    assert mask is not None
    assert int(np.count_nonzero(mask)) == thr["exceeded_cells_final"]
    final_snap = res.snapshots[-1]
    assert "threshold_exceeded_mask" in final_snap
    assert "threshold_exceedance_count" in final_snap
    assert int(np.count_nonzero(final_snap["threshold_exceeded_mask"])) == thr["exceeded_cells_final"]

    # threshold_only 的体积/深度仍写 null，不受阈值观测影响
    st = res.statistics
    assert st["removal_volume_internal"] is None
    assert st["max_depth_internal"] is None


def test_solver_disabled_does_not_fabricate_arrays():
    cfg = make_config(_sic_threshold_raw())
    res = solve(cfg, _card(SIC))
    thr = res.diagnostics["threshold"]
    assert thr["protocol_enabled"] is False
    assert thr["protocol_available"] is False
    assert thr["protocol_reason"]
    assert res.surface.threshold_exceeded_mask is None
    assert "threshold_exceeded_mask" not in res.snapshots[-1]
    assert thr["exceeded_cells_final"] == 0


def test_solver_protocol_survives_save_and_reload(tmp_path):
    import ufdemo.ui_service as U

    raw = _sic_threshold_raw()
    raw["threshold_protocol"] = {"enabled": True, "candidate_index": 1}
    cfg = make_config(raw)
    res = solve(cfg, _card(SIC))
    from ufdemo.io import load_run, save_run

    saved = save_run(res, tmp_path / "run", project_root=ROOT, code_info={"version": "test"})
    assert saved.status == "completed"
    loaded = load_run(saved.run_dir)

    # 落盘后仍可独立读出：观测量与协议身份都在
    assert "threshold_exceeded_mask" in loaded.surface
    thr = loaded.diagnostics["diagnostics"]["threshold"]
    assert thr["threshold_internal"] == pytest.approx(SIC_STRUCTURAL_J_M2)
    assert thr["fluence_basis"] == "per_event_incident"

    state = U.new_session(raw)
    frozen = U.read_existing_run(state, saved.run_dir)
    assert frozen.threshold_diagnostics["available"] is True
    assert frozen.threshold_diagnostics["used_for_depth"] is False
    assert frozen.available_layers()["threshold_mask"] is None
    assert "threshold_mask" in frozen.renderable_layers()
    assert frozen.layer("threshold_mask") is not None
    assert frozen.threshold_rows()


def test_ui_layer_availability_follows_protocol(tmp_path):
    import ufdemo.ui_service as U

    card = _card(SIC)

    off_raw = _sic_threshold_raw()
    off = U.submit(
        U.new_session(off_raw), card, out_base=tmp_path / "off", project_root=ROOT
    )
    assert off.threshold_diagnostics == {}
    assert off.available_layers()["threshold_mask"] is not None
    assert off.layer("threshold_mask") is None
    assert "threshold_mask" not in off.renderable_layers()
    assert off.threshold_rows() == []

    on_raw = _sic_threshold_raw()
    on_raw["threshold_protocol"] = {"enabled": True, "candidate_index": 0}
    on = U.submit(
        U.new_session(on_raw), card, out_base=tmp_path / "on", project_root=ROOT
    )
    assert on.threshold_diagnostics["observable"] == "single_pulse_modification_threshold"
    assert on.available_layers()["threshold_mask"] is None
    arr = on.layer("threshold_mask")
    assert arr is not None and arr.dtype == bool
    # 文案守卫：新面板的每一行都不得含被禁词
    for row in on.threshold_rows():
        U.assert_safe_wording(str(row["metric"]), where="threshold_rows")
    U.assert_safe_wording(U.LAYER_LABELS["threshold_mask"], where="layer_label")
    U.assert_safe_wording(U.UNAVAILABLE_LAYERS["threshold_mask"], where="unavailable")
