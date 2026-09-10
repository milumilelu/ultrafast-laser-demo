"""批次 E / T09 —— 界面逻辑层单元测试（``ufdemo.ui_service``）。

覆盖执行细则第 3、11.3 节与任务书第 13 节的界面约束。**本文件不导入 Streamlit**：
逻辑层的正确性必须能在没有界面时独立验证。

关键不变量（G09）：

* 只有 :func:`ufdemo.ui_service.submit` 会调用求解器；
* 回放 / 切换图层 / 旋转视图 / 取截面 **不增加** ``solve_count``；
* 提交后参数再改动 → 结果标记为「上一次运行」；
* 读取历史运行只增加 ``read_count``，不求解。
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import numpy as np
import pytest

from ufdemo import ui_service as U
from ufdemo.config import RunConfig, validate_run
from ufdemo.errors import UFDemoError
from ufdemo.materials import load_material_card

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
FIXTURE_CARD = ROOT / "tests" / "fixtures" / "analytic_fixture.json"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def fixture_material():
    return load_material_card(FIXTURE_CARD)


@pytest.fixture()
def params():
    return json.loads((EXAMPLES / "analytic_single_pulse.json").read_text(encoding="utf-8"))


@pytest.fixture()
def session(params):
    return U.new_session(params)


def _submit(session, material, tmp_path, **kw):
    return U.submit(
        session,
        material,
        out_base=tmp_path / "runs",
        project_root=ROOT,
        label="uitest",
        **kw,
    )


# ---------------------------------------------------------------------------
# 1. 求解计数：只发生在提交
# ---------------------------------------------------------------------------


def test_new_session_starts_at_zero(session):
    assert session.solve_count == 0
    assert session.read_count == 0
    assert session.frozen is None
    assert session.has_result is False
    assert session.counters() == {
        "solve_count": 0,
        "read_existing_count": 0,
        "frozen_results": 0,
    }


def test_submit_is_the_only_solver_call(session, fixture_material, tmp_path):
    assert session.solve_count == 0
    frozen = _submit(session, fixture_material, tmp_path)
    assert session.solve_count == 1
    assert session.counters()["frozen_results"] == 1
    assert frozen.status == "completed"
    assert frozen.run_id
    # 结果目录里应当有 metadata，但参考/解析核不写最终表面
    assert (Path(frozen.run_dir) / "metadata.json").exists()
    assert session.history[-1]["solve_index"] == 1


def test_validation_failure_does_not_count_as_solve(session, fixture_material, tmp_path):
    bad = copy.deepcopy(session.params)
    bad["grid"]["nx"] = 1  # 低于下限 3
    U.set_pending_params(session, bad)
    with pytest.raises(UFDemoError):
        _submit(session, fixture_material, tmp_path)
    assert session.solve_count == 0, "校验失败不得计入求解次数"
    assert session.frozen is None
    assert session.last_error is not None


def test_set_pending_params_does_not_solve(session, fixture_material, tmp_path):
    _submit(session, fixture_material, tmp_path)
    assert session.solve_count == 1
    changed = copy.deepcopy(session.params)
    changed["laser"]["pulse_energy_J"] *= 1.5
    U.set_pending_params(session, changed)
    assert session.solve_count == 1, "写编辑态不等于求解"


def test_preview_validate_does_not_solve(session, fixture_material):
    U.preview_validate(session, fixture_material)
    assert session.solve_count == 0
    assert session.preview_ok is True
    assert session.preview_errors == []


def test_preview_validate_reports_errors_without_raising(session, fixture_material):
    bad = copy.deepcopy(session.params)
    bad["grid"]["nx"] = 2
    U.set_pending_params(session, bad)
    errs = U.preview_validate(session, fixture_material)
    assert errs, "非法网格应当产生预览错误"
    assert session.preview_ok is False
    assert session.solve_count == 0


# ---------------------------------------------------------------------------
# 2. 参数编辑态 与 结果态 分离 + 过期标记
# ---------------------------------------------------------------------------


def test_fresh_result_is_not_stale(session, fixture_material, tmp_path):
    _submit(session, fixture_material, tmp_path)
    assert session.is_stale() is False
    assert session.result_label().startswith("本次运行")


def test_editing_after_submit_marks_previous_run(session, fixture_material, tmp_path):
    _submit(session, fixture_material, tmp_path)
    changed = copy.deepcopy(session.params)
    changed["laser"]["spot_radius_m"] *= 1.1
    U.set_pending_params(session, changed)
    assert session.is_stale() is True
    assert session.result_label().startswith("上一次运行")
    # 旧结果本身必须保持冻结（不被新参数污染）
    assert session.frozen is not None
    assert session.frozen.input_hash == session.submitted_params_hash


def test_result_arrays_come_from_frozen_not_current_params(session, fixture_material, tmp_path):
    frozen = _submit(session, fixture_material, tmp_path)
    before = np.array(frozen.layer("height"), copy=True)
    changed = copy.deepcopy(session.params)
    changed["grid"]["nx"] = 41  # 完全不同的网格
    U.set_pending_params(session, changed)
    after = frozen.layer("height")
    assert after.shape == before.shape, "结果数组不得随表单值变化"


def test_revert_to_original_params_clears_stale(session, fixture_material, tmp_path):
    original = copy.deepcopy(session.params)
    _submit(session, fixture_material, tmp_path)
    changed = copy.deepcopy(original)
    changed["laser"]["pulse_energy_J"] *= 2
    U.set_pending_params(session, changed)
    assert session.is_stale() is True
    U.set_pending_params(session, original)
    assert session.is_stale() is False, "改回原值后不应再标为上一次运行"


# ---------------------------------------------------------------------------
# 3. 回放 / 视图 / 图层 / 截面 —— 绝不求解
# ---------------------------------------------------------------------------


def test_playback_and_views_never_solve(session, fixture_material, tmp_path):
    frozen = _submit(session, fixture_material, tmp_path)
    n = session.solve_count
    # 快照读取
    for i in range(len(frozen.snapshots)):
        U.snapshot_arrays(session, i)
    # 图层切换
    for name in frozen.renderable_layers():
        U.layer_view(session, name, None)
    # 视图旋转/翻转
    arr = U.layer_view(session, "height", None)
    U.orient_view(arr, transpose=True)
    U.orient_view(arr, flip_x=True)
    U.orient_view(arr, flip_y=True)
    U.orient_view(arr, transpose=True, flip_x=True, flip_y=True)
    # 截面
    depth = frozen.depth_of()
    if depth is not None:
        U.cross_section(depth, axis="x", index=depth.shape[1] // 2)
        U.cross_section(depth, axis="y", index=depth.shape[0] // 2)
    assert session.solve_count == n == 1, "回放/视图/图层/截面不得触发求解"


def test_snapshot_policy_none_yields_only_final_snapshot(params, fixture_material, tmp_path):
    raw = copy.deepcopy(params)
    raw["output"]["snapshot_policy"] = "none"
    sess = U.new_session(raw)
    frozen = _submit(sess, fixture_material, tmp_path)
    # 求解器始终记录一个「最终」快照；none 策略只是不记中间快照。
    assert len(frozen.snapshots) == 1
    assert frozen.snapshots[0]["final"] is True
    assert U.snapshot_arrays(sess, None)  # 当前表面仍可读
    assert sess.solve_count == 1


# ---------------------------------------------------------------------------
# 4. 图层可用性：如实报告，不伪造
# ---------------------------------------------------------------------------


def test_threshold_mask_reported_unavailable(session, fixture_material, tmp_path):
    frozen = _submit(session, fixture_material, tmp_path)
    avail = frozen.available_layers()
    assert "threshold_mask" in avail
    assert avail["threshold_mask"] is not None, "本批次必须如实报告不可用"
    assert "threshold_mask" not in frozen.renderable_layers()
    assert frozen.layer("threshold_mask") is None


def test_depth_derived_from_height_minus_initial(session, fixture_material, tmp_path):
    frozen = _submit(session, fixture_material, tmp_path)
    depth = frozen.depth_of()
    h = frozen.layer("height")
    h0 = frozen.arrays_from(None)["initial_height"]
    assert depth is not None
    np.testing.assert_allclose(depth, h0 - h)


def test_unknown_layer_raises(session, fixture_material, tmp_path):
    frozen = _submit(session, fixture_material, tmp_path)
    with pytest.raises(UFDemoError):
        frozen.layer("temperature_field")


def test_available_layers_returns_all_known_labels(session, fixture_material, tmp_path):
    frozen = _submit(session, fixture_material, tmp_path)
    assert set(frozen.available_layers()) == set(U.LAYER_LABELS)


# ---------------------------------------------------------------------------
# 5. 措辞守卫与单位标签
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("term", ["热影响区", "HAZ", "温度场", "温度分布", "热输入"])
def test_forbidden_wording_rejected(term):
    with pytest.raises(UFDemoError) as ei:
        U.assert_safe_wording(f"图中显示{term}")
    assert "禁用措辞" in str(ei.value)


def test_safe_layer_labels_pass_wording_guard():
    for label in U.LAYER_LABELS.values():
        U.assert_safe_wording(label, where="layer_label")


def test_layer_labels_avoid_all_forbidden_terms():
    """严格口径：标签（含否定式）不得出现任何被禁词。"""
    for name, label in U.LAYER_LABELS.items():
        for term in U.FORBIDDEN_TERMS:
            assert term not in label, f"图层 {name} 的标签含被禁词 {term!r}：{label}"


def test_unavailable_reasons_avoid_forbidden_terms():
    for name, reason in U.UNAVAILABLE_LAYERS.items():
        U.assert_safe_wording(reason, where=f"unavailable.{name}")


def test_threshold_mask_does_not_claim_haz():
    """阈值标记不得被称作热影响区/HAZ —— 标签与不可用原因都必须干净。"""
    label = U.LAYER_LABELS["threshold_mask"]
    reason = U.UNAVAILABLE_LAYERS["threshold_mask"]
    for term in ("热影响区", "HAZ"):
        assert term not in label
        assert term not in reason


def test_fluence_label_does_not_say_temperature():
    label = U.LAYER_LABELS["cumulative_fluence"]
    for term in ("温度", "温度场", "温度分布", "热输入"):
        assert term not in label


def test_forbidden_terms_cover_spec_requirements():
    # 执行细则 11.3：阈值图不写“热影响区”；照射剂量不写“温度”。
    assert "热影响区" in U.FORBIDDEN_TERMS
    assert "HAZ" in U.FORBIDDEN_TERMS
    assert "温度" in U.FORBIDDEN_TERMS


def test_dimensionless_labels_for_synthetic(tmp_path):
    raw = json.loads((EXAMPLES / "synthetic_demo_point.json").read_text(encoding="utf-8"))
    unit = U.unit_context_of(raw)
    assert unit.mode == "dimensionless"
    assert unit.allows_physical_depth_export is False
    assert unit.depth_label != "um"
    label = U.depth_export_label(unit)
    assert "depth_um" not in label
    with pytest.raises(UFDemoError):
        U.guard_depth_export(unit, "result_depth_um.csv")
    assert U.guard_depth_export(unit, f"result_{label}.csv")


def test_si_mode_allows_physical_depth_export(params):
    unit = U.unit_context_of(params)
    assert unit.mode == "SI"
    assert unit.allows_physical_depth_export is True
    assert U.guard_depth_export(unit, "result_depth_um.csv")


# ---------------------------------------------------------------------------
# 6. 能力门槛与合成模式（不自动切换）
# ---------------------------------------------------------------------------


def test_capability_gate_analytic_fixture_reference(fixture_material):
    ok, reason = U.capability_gate(fixture_material, "reference_case")
    assert ok is True
    assert "fixture" in reason or "非物理" in reason


def test_capability_gate_rejects_unopened_mode(fixture_material):
    ok, reason = U.capability_gate(fixture_material, "calibrated_case")
    assert ok is False
    assert "calibrated_case" in reason


def test_capability_gate_rejects_unknown_mode(fixture_material):
    ok, _ = U.capability_gate(fixture_material, "no_such_mode")
    assert ok is False


def test_capability_gate_rejects_unauthorized_mode_on_real_card():
    card = load_material_card(ROOT / "data" / "materials" / "cfrp_t700_yb01_800nm.json")
    # CFRP 卡不允许 reference_case（无事件核能力/未开放该模式）
    ok, reason = U.capability_gate(card, "reference_case")
    if "reference_case" not in card.allowed_run_modes:
        assert ok is False
        assert "未开放" in reason


def test_suggest_synthetic_offers_but_does_not_auto_switch(fixture_material):
    # calibrated_case 不可用 → 提供显式切换提示
    offer = U.suggest_synthetic(fixture_material, "calibrated_case")
    assert offer is not None
    assert "合成" in offer["offer"]
    assert "不是该材料的物理预测" in offer["warning"]
    # 可用模式 → 不提示
    assert U.suggest_synthetic(fixture_material, "synthetic_demo") is None


def test_synthetic_choices_have_dimensionless_note():
    choices = U.synthetic_choices()
    assert choices
    c = choices[0]
    assert c["id"] == "synthetic_demo_point"
    assert "无量纲" in c["title"]
    assert "禁止导出物理" in c["note"]


# ---------------------------------------------------------------------------
# 7. 读取历史运行：不求解
# ---------------------------------------------------------------------------


def test_read_existing_run_does_not_solve(session, fixture_material, tmp_path):
    frozen = _submit(session, fixture_material, tmp_path)
    fresh = U.new_session(session.params)
    loaded = U.read_existing_run(fresh, frozen.run_dir)
    assert fresh.solve_count == 0, "读取历史不得触发求解"
    assert fresh.read_count == 1
    assert loaded.run_id == frozen.run_id
    assert loaded.status == "completed"
    assert loaded.input_hash == frozen.input_hash
    assert fresh.is_stale() is False, "刚读取完不应被标为上一次运行"


def test_read_existing_run_exposes_disk_arrays(session, fixture_material, tmp_path):
    frozen = _submit(session, fixture_material, tmp_path)
    fresh = U.new_session(session.params)
    loaded = U.read_existing_run(fresh, frozen.run_dir)
    # 内存里没有 result，但磁盘数组必须可读
    assert loaded.result is None
    h = loaded.layer("height")
    assert h is not None and np.asarray(h).size > 0
    depth = loaded.depth_of()
    assert depth is not None
    assert "height" in loaded.renderable_layers()


def test_read_existing_run_keeps_solve_count_zero_after_views(session, fixture_material, tmp_path):
    frozen = _submit(session, fixture_material, tmp_path)
    fresh = U.new_session(session.params)
    loaded = U.read_existing_run(fresh, frozen.run_dir)
    for name in loaded.renderable_layers():
        U.layer_view(fresh, name)
    U.snapshot_arrays(fresh, None)
    assert fresh.solve_count == 0
    assert fresh.read_count == 1


# ---------------------------------------------------------------------------
# 8. 参数哈希可复现（G09）
# ---------------------------------------------------------------------------


def test_params_hash_is_reproducible(params):
    assert U.params_hash(params) == U.params_hash(copy.deepcopy(params))
    assert U.params_hash_is_reproducible(params)

    # 键顺序不影响哈希
    reordered = {k: params[k] for k in reversed(list(params.keys()))}
    assert U.params_hash(reordered) == U.params_hash(params)


def test_params_hash_changes_with_value(params):
    changed = copy.deepcopy(params)
    changed["seed"] = params["seed"] + 1
    assert U.params_hash(changed) != U.params_hash(params)


def test_stable_params_json_is_stable(params):
    assert U.stable_params_json(params) == U.stable_params_json(copy.deepcopy(params))


def test_submitted_hash_equals_frozen_hash(session, fixture_material, tmp_path):
    frozen = _submit(session, fixture_material, tmp_path)
    assert frozen.input_hash == session.submitted_params_hash
    assert frozen.input_hash == U.params_hash(session.params)


# ---------------------------------------------------------------------------
# 9. 模板与参数装配
# ---------------------------------------------------------------------------


def test_list_examples_and_load_template():
    names = U.list_examples(EXAMPLES)
    assert "analytic_single_pulse.json" in names
    raw = U.load_template(EXAMPLES, "analytic_single_pulse.json")
    assert raw["run_mode"] == "reference_case"


def test_get_path_nested_and_list():
    raw = U.load_template(EXAMPLES, "analytic_single_pulse.json")
    assert U.get_path(raw, "grid.nx") == 161
    assert U.get_path(raw, "path.segments.0.speed_m_s") == 0.0
    assert U.get_path(raw, "path.segments.0.start_xyz_m.0") == 0
    assert U.get_path(raw, "no.such.path", default="X") == "X"
    assert U.get_path(raw, "path.segments.9", default="X") == "X"


def test_apply_overrides_scalar_list_and_creation():
    raw = U.load_template(EXAMPLES, "analytic_single_pulse.json")
    out = U.apply_overrides(
        raw,
        {
            "grid.nx": 81,
            "laser.pulse_energy_J": 2e-5,
            "path.segments.0.speed_m_s": 0.5,
            "path.segments.0.start_xyz_m.0": 1e-6,
            "output.snapshot_policy": "none",
            "output.new_option.where": 1,  # 模板中不存在 → 按需创建
        },
    )
    assert out["grid"]["nx"] == 81
    assert raw["grid"]["nx"] == 161, "不得修改原模板"
    assert out["laser"]["pulse_energy_J"] == 2e-5
    assert out["path"]["segments"][0]["speed_m_s"] == 0.5
    assert out["path"]["segments"][0]["start_xyz_m"][0] == 1e-6
    assert out["output"]["snapshot_policy"] == "none"
    assert out["output"]["new_option"]["where"] == 1


def test_apply_overrides_expands_list_index():
    raw = {"path": {"segments": [{"speed_m_s": 0.0}]}}
    out = U.apply_overrides(raw, {"path.segments.1.speed_m_s": 2.0})
    assert out["path"]["segments"][1]["speed_m_s"] == 2.0


def test_apply_overrides_skips_none():
    raw = {"grid": {"nx": 5}}
    out = U.apply_overrides(raw, {"grid.nx": None})
    assert out["grid"]["nx"] == 5


def test_apply_overrides_rejects_non_index_list_key():
    raw = {"path": {"segments": [{"speed_m_s": 0.0}]}}
    with pytest.raises(UFDemoError):
        U.apply_overrides(raw, {"path.segments.speed_m_s": 1.0})


def test_iter_layer_names_matches_labels():
    assert list(U.iter_layer_names()) == list(U.LAYER_LABELS.keys())


# ---------------------------------------------------------------------------
# 10. 视图变换与截面（纯数组）
# ---------------------------------------------------------------------------


def test_orient_view_transforms():
    arr = np.arange(12).reshape(3, 4)
    assert U.orient_view(arr, transpose=True).shape == (4, 3)
    np.testing.assert_array_equal(U.orient_view(arr, flip_x=True), arr[:, ::-1])
    np.testing.assert_array_equal(U.orient_view(arr, flip_y=True), arr[::-1, :])
    assert U.orient_view(None) is None


def test_cross_section_axes_and_errors():
    arr = np.arange(12).reshape(3, 4)
    sec_x = U.cross_section(arr, axis="x", index=1, coords=[1.0, 2.0, 3.0, 4.0])
    assert sec_x["axis"] == "x"
    assert sec_x["value"] == list(arr[1, :])
    assert sec_x["coord"] == [1.0, 2.0, 3.0, 4.0]
    sec_y = U.cross_section(arr, axis="y", index=2)
    assert sec_y["value"] == list(arr[:, 2])
    with pytest.raises(UFDemoError):
        U.cross_section(arr, axis="z", index=0)
    empty = U.cross_section(None, axis="x", index=0)
    assert empty["value"] == []


# ---------------------------------------------------------------------------
# 11. 水印与运行列表
# ---------------------------------------------------------------------------


def test_watermark_contains_identity_and_mode(session, fixture_material, tmp_path):
    frozen = _submit(session, fixture_material, tmp_path)
    wm = frozen.material_watermark
    assert wm["material_id"] == "analytic_fixture_not_a_material"
    assert wm["run_mode"] == "reference_case"
    assert wm["unit_system"] == "SI"
    assert "非物理预测" in U.format_watermark(wm)
    U.assert_safe_wording(U.format_watermark(wm), where="watermark")


def test_list_runs_finds_created_run(session, fixture_material, tmp_path):
    frozen = _submit(session, fixture_material, tmp_path)
    runs = U.list_runs(tmp_path / "runs")
    assert any(r["run_id"] == frozen.run_id for r in runs)
    row = next(r for r in runs if r["run_id"] == frozen.run_id)
    assert row["status"] == "completed"
    assert row["status_zh"] == "已完成"


def test_list_runs_on_missing_dir_returns_empty(tmp_path):
    assert U.list_runs(tmp_path / "nope") == []


def test_summary_rows_reference_unit_keys(session, fixture_material, tmp_path):
    frozen = _submit(session, fixture_material, tmp_path)
    rows = frozen.summary_rows()
    assert {r["metric"] for r in rows} >= {
        "removal_volume_internal",
        "unit_system",
        "run_mode",
    }
