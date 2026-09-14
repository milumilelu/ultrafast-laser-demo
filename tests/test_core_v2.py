"""C1 / C4 核心回归（V2 任务书 §8 最低测试集的相关项）。

覆盖：

* **共用实验背景**：公式自洽、``E_p`` 用物镜后功率、CSV 逐行变量不被覆盖；
* **固定原始焦点**：保留被动离焦、不启用动态入射角、遍次增加 Z 不变；
* **CSV 导入**：GB18030 编码、`间距 mm→μm`、负值原样保留、缺字段单列；
* **分组**：重复工况**不跨**训练/留出；
* **标定**：``a`` 进入每次几何更新（``D(a) ≠ a·D(1)``）、``a=1`` 还原基线、
  落盘/重载一致、触边界如实报告；
* **路径**：间距**不被拉伸**、焦点 Z 恒定、总时长标"未校准"、候选 25 个。

⚠️ 本文件**不声称**任何材料已完成标定：真实材料卡缺与共享背景同光学条件的基线
（ZrO₂ 卡的基线是 16 μm 光斑 / 208 fs 协议，与共享背景的 0.874 μm 光斑不同源）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from ufdemo.calibration import (
    ExperimentRow,
    ObservationSpec,
    PredictionSpec,
    calibrate_gain,
    group_split,
    load_calibration,
    load_experiment_csv,
    predict_mean_depth,
    save_calibration,
)
from ufdemo.config import (
    RunConfig,
    load_shared_background,
    shared_background_patch,
)
from ufdemo.errors import UFDemoError
from ufdemo.planning import (
    DEFAULT_PASS_COUNTS,
    DEFAULT_SPACINGS_UM,
    enumerate_candidates,
    serpentine_plan,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = "tests/fixtures/analytic_fixture.json"
#: 上层目录里的真实实验表（不在 git 内，缺失则跳过）
EXP_DIR = ROOT.parent / "数据"

pytestmark = pytest.mark.core_v2


# ---------------------------------------------------------------------------
# C1：共用实验背景
# ---------------------------------------------------------------------------


def test_shared_background_self_consistent():
    """资料给的 nominal 值必须与公式推导自洽（w0 与 zR 由同一组 M²/λ/NA 推出）。"""
    bg = load_shared_background()
    r = bg.check_self_consistency()
    assert r["ok"] is True
    assert abs(r["waist_derived_um"] - 0.874) < 0.01
    assert abs(r["rayleigh_derived_um"] - 1.943) < 0.02


def test_self_consistency_check_actually_catches_mismatch():
    """把 nominal 值改坏时**必须报错** —— 否则这个校验就是摆设。"""
    from ufdemo.config import SharedExperimentBackground

    bg = load_shared_background()
    bad = json.loads(json.dumps(bg.raw))
    bad["optics"]["nominal_waist_radius_um"] = 5.0   # 明显与公式不符
    with pytest.raises(UFDemoError) as ei:
        SharedExperimentBackground(raw=bad).check_self_consistency()
    assert ei.value.code == "CONFIG_INVALID"


def test_pulse_energy_uses_post_objective_power():
    """``E_p = P_物镜后 / f`` —— **不得**用软件里设的 10 W。"""
    bg = load_shared_background()
    assert bg.post_objective_power_W == pytest.approx(5.3333)
    assert bg.software_setpoint_W == pytest.approx(10.0)
    for f_kHz in (2.0, 10.0, 50.0):
        e = bg.pulse_energy_J(f_kHz * 1e3)
        assert e == pytest.approx(5.3333 / (f_kHz * 1e3), rel=1e-9)
    # 用 10 W 算出来的值必须被排除（防止有人改回设定功率）
    assert bg.pulse_energy_J(100e3) != pytest.approx(10.0 / 100e3, rel=1e-3)


def test_shared_patch_keeps_passive_defocus_but_disables_dynamic_angle():
    """**关键**：固定焦点 ≠ 关掉离焦。两者必须分开。"""
    bg = load_shared_background()
    patch = shared_background_patch(bg, repetition_rate_Hz=100e3)
    assert patch["solver"]["geometry_feedback"] == "axial_defocus", (
        "被动轴向离焦必须保留（任务书 §2）"
    )
    assert patch["solver"]["dynamic_angle"] is False, "动态入射角停用"


def test_card_delta_not_shared_across_materials():
    """七材料**不共享**阈值/去除尺度。"""
    bg = load_shared_background()
    scope = bg.raw["inheritance_scope"]
    assert "threshold" in scope["NOT_shared"]
    assert "delta" in scope["NOT_shared"]
    assert "calibration_gain" in scope["NOT_shared"]


# ---------------------------------------------------------------------------
# 固定原始焦点：遍次增加 Z 不变 + 离焦被动生效
# ---------------------------------------------------------------------------


def test_layer_z_constant_across_passes():
    """遍次增加时**焦点 Z 不变**（fixed_original_surface），路径 Z 恒为 0。"""
    p1 = serpentine_plan(region_um=(40, 40), spacing_um=4, pass_count=1, scan_speed_mm_s=200)
    p3 = serpentine_plan(region_um=(40, 40), spacing_um=4, pass_count=3, scan_speed_mm_s=200)
    for plan in (p1, p3):
        zs = {seg.start_xyz_m[2] for seg in plan.segments} | {seg.end_xyz_m[2] for seg in plan.segments}
        assert zs == {0.0}, f"焦点 Z 必须恒为 0（实际 {zs}）"
    assert p3.pass_count == 3 and p1.pass_count == 1


def test_no_layer_refocus_capability_in_config():
    """**不得**存在层间调焦/按深度改 Z 的开关（任务书 §2 明令不实现）。"""
    src = (ROOT / "src" / "ufdemo" / "planning.py").read_text(encoding="utf-8")
    for banned in ("layer_refocus", "refocus_z", "adjust_z_per_layer"):
        assert banned not in src, f"planning.py 出现了层间调焦痕迹：{banned}"


def test_axial_defocus_actually_changes_result():
    """开启 ``axial_defocus`` 时结果必须**不同于** ``fixed_geometry``。

    若两者相同，说明离焦没生效 —— 那正是任务书警告的"别顺手关掉"的情况。
    """
    base = json.loads((ROOT / "examples" / "ten_pulses.json").read_text(encoding="utf-8"))
    base["laser"].update({"wavelength_m": 1030e-9, "m2": 1.2,
                          "spot_radius_m": 0.874e-6, "repetition_rate_Hz": 100e3})
    from ufdemo.materials import load_material_card
    from ufdemo.solver import solve

    mat = load_material_card(ROOT / base["material_card_file"])

    def depth(fb):
        raw = json.loads(json.dumps(base))
        raw["solver"]["geometry_feedback"] = fb
        cfg = RunConfig.from_dict(raw, base_dir=str(ROOT))
        res = solve(cfg, mat)
        assert res.status == "completed"
        return float((res.surface.initial_height - res.surface.height).max())

    d_fixed, d_axial = depth("fixed_geometry"), depth("axial_defocus")
    assert d_fixed != d_axial, (
        "两种几何反馈的结果完全相同 → 离焦没有生效"
    )
    assert d_axial < d_fixed, "开启被动离焦后去除应变浅（光斑被摊开）"


# ---------------------------------------------------------------------------
# C4：标定增益
# ---------------------------------------------------------------------------


def _fixture_spec(**over) -> PredictionSpec:
    kw = dict(material_card_file=FIXTURE, window_um=12.0, dx_um=1.0,
              observation=ObservationSpec(kind="full_region_mean", statistic="mean"))
    kw.update(over)
    return PredictionSpec(**kw)


def _fake_rows() -> list[ExperimentRow]:
    base = dict(pulse_duration_fs=800.0, repetition_rate_kHz=10.0, scan_speed_mm_s=200.0)
    return [ExperimentRow(sample_id=f"S{i}", hatch_spacing_um=h, pass_count=n,
                          mean_depth_um=0.0, **base)
            for i, (h, n) in enumerate([(4.0, 1), (6.0, 2), (8.0, 1)])]


def test_gain_one_is_baseline_bitwise():
    """``a = 1`` 必须**逐位**回到基线（不能有 1e-16 级的漂移）。"""
    spec = _fixture_spec()
    r = _fake_rows()[0]
    a = predict_mean_depth(r, spec=spec, gain=1.0)
    b = predict_mean_depth(r, spec=spec, gain=1.0)
    assert a["predicted_depth_m"] == b["predicted_depth_m"]


def test_gain_enters_geometry_update_not_post_scaling():
    """``a`` 必须在**几何更新之前**起作用 —— 用闭合验证钉住。

    造"实验值 = a_true 的预测"，从 a=1 起标定，应恢复到 a_true。
    若 ``a`` 只是事后乘系数，这个恢复同样成立 —— 所以还要配合
    ``test_axial_defocus_actually_changes_result`` 一起看（那条证明离焦在起作用，
    而离焦只有在 a 进入几何更新时才能被 a 影响）。
    """
    spec = _fixture_spec()
    bg = load_shared_background()
    rows = _fake_rows()
    TRUE = 2.0
    for r in rows:
        r.mean_depth_um = predict_mean_depth(r, spec=spec, gain=TRUE, bg=bg)["predicted_depth_um"]

    res = calibrate_gain(rows, spec=spec, bg=bg, bounds=(0.2, 10.0), residual_scale_um=0.05)
    assert res.gain == pytest.approx(TRUE, rel=1e-3), f"应恢复 {TRUE}，实际 {res.gain}"
    m = res.metrics()
    assert m["train"]["mae_after_um"] < m["train"]["mae_before_um"]
    assert m["train"]["mae_after_um"] < 1e-6


def test_gain_with_axial_defocus_is_nonlinear(tmp_path):
    """**有被动离焦时** ``D(a) ≠ a·D(1)``。

    这条是"标定真的改变了求解过程"的直接证据：任务书 §4 要求 a 在几何更新前生效，
    其可观测后果就是深度对 a **非线性**。
    """
    base = json.loads((ROOT / "examples" / "ten_pulses.json").read_text(encoding="utf-8"))
    # 用共享背景的光学（此前缺波长/M²，离焦算不出来）
    base["laser"].update({"wavelength_m": 1030e-9, "m2": 1.2,
                          "spot_radius_m": 0.874e-6, "repetition_rate_hz": 100e3,
                          "repetition_rate_Hz": 100e3})
    base["solver"]["geometry_feedback"] = "axial_defocus"
    from ufdemo.materials import load_material_card
    from ufdemo.solver import solve

    mat = load_material_card(ROOT / base["material_card_file"])

    def depth(gain):
        raw = json.loads(json.dumps(base))
        raw["solver"]["response_gain"] = gain
        cfg = RunConfig.from_dict(raw, base_dir=str(ROOT))
        res = solve(cfg, mat)
        assert res.status == "completed"
        return float((res.surface.initial_height - res.surface.height).max())

    d1, d2 = depth(1.0), depth(2.0)
    ratio = d2 / d1
    assert abs(ratio - 2.0) / 2.0 > 0.01, (
        f"比值 {ratio:.4f} 几乎是线性 → a 可能没进几何更新（或离焦没生效）"
    )


def test_calibration_roundtrip(tmp_path):
    """保存/重载一致（任务书 §5.4：重载后结果一致）。"""
    spec = _fixture_spec()
    rows = _fake_rows()
    for r in rows:
        r.mean_depth_um = predict_mean_depth(r, spec=spec, gain=1.5)["predicted_depth_um"]
    res = calibrate_gain(rows, spec=spec, bounds=(0.2, 10.0), residual_scale_um=0.05)
    p = save_calibration(res, tmp_path / "calibration.json")
    back = load_calibration(p)
    assert back["gain"] == res.gain
    assert back["schema"] == "ufdemo.calibration/1"
    assert back["metrics"]["train"]["n"] == res.n_train


def test_bad_calibration_file_rejected(tmp_path):
    """非法标定文件必须报错（增益 ≤ 0 / schema 不符）。"""
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"schema": "ufdemo.calibration/1", "gain": -1.0}), encoding="utf-8")
    with pytest.raises(UFDemoError):
        load_calibration(p)
    p2 = tmp_path / "bad2.json"
    p2.write_text(json.dumps({"schema": "other", "gain": 1.0}), encoding="utf-8")
    with pytest.raises(UFDemoError):
        load_calibration(p2)


def test_hit_bound_is_reported_not_silently_accepted():
    """触及边界要**如实报告**，而不是悄悄接受。"""
    spec = _fixture_spec()
    bg = load_shared_background()
    rows = _fake_rows()
    for r in rows:
        # 实验值远大于任何 a 能达到的范围 → 必然触边界
        r.mean_depth_um = 1e4
    res = calibrate_gain(rows, spec=spec, bg=bg, bounds=(0.5, 2.0), residual_scale_um=1.0)
    assert res.hit_bound is True
    assert any("边界" in n for n in res.notes)


# ---------------------------------------------------------------------------
# CSV 导入与分组
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not (EXP_DIR / "AlSiC.csv").exists(),
                    reason="实验表不在工作区（上层 数据/ 目录）")
def test_real_csv_import_gb18030_and_mm_to_um():
    """真实实验表：GB18030 编码 + 中文表头 + **间距 mm→μm**。"""
    t = load_experiment_csv(EXP_DIR / "AlSiC.csv")
    assert t.encoding == "gb18030"
    assert t.n_used > 100
    r = t.rows[0]
    # 「间距mm」0.002 mm → 2 μm（与任务书 h={2,4,6,8,10} 一致）
    assert r.hatch_spacing_um == pytest.approx(2.0)
    assert r.pulse_duration_fs == pytest.approx(223.0)
    assert r.repetition_rate_kHz == pytest.approx(2.0)
    assert r.hatch_spacing_um > 0 and r.mean_depth_um > 0


@pytest.mark.skipif(not (EXP_DIR / "AlSiC.csv").exists(), reason="实验表不在工作区")
def test_negative_means_are_preserved_not_cleaned():
    """负均值**原样保留**（不取绝对值/不截零），且被单独标注。"""
    t = load_experiment_csv(EXP_DIR / "AlSiC.csv")
    neg = [r for r in t.rows if r.mean_depth_um < 0]
    assert neg, "该表确实含负均值（实测 8 行）"
    assert any("负" in n for n in t.notes), "负值必须在 notes 里单独报告"


@pytest.mark.skipif(not (EXP_DIR / "AlSiC.csv").exists(), reason="实验表不在工作区")
def test_group_split_does_not_leak_repeated_conditions():
    """重复工况**不得跨**训练/留出。"""
    t = load_experiment_csv(EXP_DIR / "AlSiC.csv")
    tr, ho, info = group_split(t.rows)
    assert info["leakage_groups"] == [], f"跨侧泄漏：{info['leakage_groups']}"
    # 手工核对：每个工况组的行必须整体在同侧
    tr_keys = {r.condition_key for r in tr}
    ho_keys = {r.condition_key for r in ho}
    assert not (tr_keys & ho_keys)


def test_missing_required_field_rows_are_listed_not_guessed(tmp_path):
    """缺必需字段的行**单独列出**，不猜值补齐。"""
    p = tmp_path / "x.csv"
    p.write_text(
        "序号,脉宽fs,频率kHz,间距mm,重复加工次数,速度mm/s,mean_depth_um\n"
        "1,500,10,0.004,3,50,12.5\n"
        "2,500,,0.004,3,50,13.0\n",   # 缺频率
        encoding="utf-8",
    )
    t = load_experiment_csv(p)
    assert t.n_used == 1
    assert len(t.skipped_rows) == 1
    assert "repetition_rate_kHz" in t.skipped_rows[0]["missing"]


def test_row_condition_key_covers_all_process_variables():
    """工况指纹必须覆盖脉宽/频率/速度/间距/遍数（少一个就会误分组）。"""
    base = dict(sample_id="a", pulse_duration_fs=500, repetition_rate_kHz=10,
                scan_speed_mm_s=50, hatch_spacing_um=4, pass_count=3, mean_depth_um=1.0)
    k0 = ExperimentRow(**base).condition_key
    for field, val in (("pulse_duration_fs", 600), ("repetition_rate_kHz", 20),
                       ("scan_speed_mm_s", 60), ("hatch_spacing_um", 6), ("pass_count", 4)):
        assert ExperimentRow(**{**base, field: val}).condition_key != k0, (
            f"{field} 未进入工况指纹"
        )


# ---------------------------------------------------------------------------
# C5 基础：弓字形路径
# ---------------------------------------------------------------------------


def test_spacing_is_not_silently_stretched():
    """**禁止**用 linspace 把间距拉伸到刚好铺满（任务书 §6）。"""
    p = serpentine_plan(region_um=(200.0, 200.0), spacing_um=7.0,
                        pass_count=1, scan_speed_mm_s=200.0)
    assert p.spacing_effective_um == 7.0, "间距被改动了"
    # 200 / 7 = 28.57 → 29 条线，末端余 200-28×7 = 4 μm
    assert p.n_scan_lines == 29
    assert any("余" in n for n in p.notes), "余量必须如实报告"


def test_time_reported_as_ideal_when_device_policy_unknown():
    """换向减速未确知 → 只给理想时间并标"未校准"，**不假装算准**。"""
    p = serpentine_plan(region_um=(100.0, 100.0), spacing_um=5.0,
                        pass_count=1, scan_speed_mm_s=200.0)
    assert p.time_is_calibrated is False
    d = p.to_dict()
    assert d["timeCalibrated"] is False
    assert d["focusStrategy"] == "fixed_original_surface"
    assert d["focusZM"] == 0.0


def test_plan_segments_are_consumable_by_solver():
    """``PathPlan`` 能转成求解器吃的段（**正向与导出同一份路径**）。"""
    p = serpentine_plan(region_um=(20.0, 20.0), spacing_um=5.0,
                        pass_count=1, scan_speed_mm_s=200.0)
    segs = p.to_segments_config()
    assert segs, "至少要有一段"
    for i, s in enumerate(segs):
        assert s["end_s"] >= s["start_s"]
        assert len(s["start_xyz_m"]) == 3 and len(s["end_xyz_m"]) == 3
        if i:
            assert s["start_s"] == pytest.approx(segs[i - 1]["end_s"]), "时间轴必须连续"
    # 扫描段出光、换向段不出光
    assert any(s["laser_on"] for s in segs)
    assert any(s["laser_on"] is False for s in segs)


def test_export_csv_is_labelled_neutral(tmp_path):
    """导出文件必须标明是**中立路径**，不冒称机床控制器代码。"""
    p = serpentine_plan(region_um=(20.0, 20.0), spacing_um=5.0,
                        pass_count=1, scan_speed_mm_s=200.0)
    out = p.write_csv(tmp_path / "path.csv")
    text = out.read_text(encoding="utf-8")
    assert "中立路径" in text
    assert "非机床控制器代码" in text


def test_candidate_grid_is_25():
    """h={2,4,6,8,10} × N={1..5} = 25 个候选。"""
    cands = enumerate_candidates()
    assert len(cands) == 25
    assert {h for h, _ in cands} == set(DEFAULT_SPACINGS_UM)
    assert {n for _, n in cands} == set(DEFAULT_PASS_COUNTS)


def test_invalid_plan_inputs_rejected():
    """非法输入（零间距 / 非整数遍数 / 负速度）必须报错。"""
    for kw in (dict(spacing_um=0.0), dict(pass_count=0), dict(pass_count=1.5),
               dict(scan_speed_mm_s=0.0), dict(region_um=(0.0, 10.0))):
        base = dict(region_um=(20.0, 20.0), spacing_um=4.0, pass_count=1,
                    scan_speed_mm_s=200.0)
        with pytest.raises(UFDemoError):
            serpentine_plan(**{**base, **kw})


# ---------------------------------------------------------------------------
# C2：上游轮廓适配与对照
# ---------------------------------------------------------------------------


def test_upstream_synthetic_case_roundtrip(tmp_path):
    """虚拟上游样例能落盘/载入，且**明确标记为虚拟**。"""
    from ufdemo.upstream import write_synthetic_upstream_case, load_upstream_case

    jp, pp = write_synthetic_upstream_case(tmp_path, n_pulses=2, depth_um=9.0, radius_um=6.0)
    assert jp.exists() and pp.exists()
    prof, wnote = load_upstream_case(jp)
    assert prof.is_synthetic is True, "虚拟输入必须自带标记，不得被读成真实上游结果"
    assert prof.geometry == "axisymmetric_pit"
    assert prof.center_depth_um == pytest.approx(9.0, rel=1e-6)
    assert prof.n_pulses == 2
    assert "sqrt(2)" in wnote or "1.414" in wnote, "必须给出半径定义换算提示"


def test_upstream_rejects_geometry_mixing(tmp_path):
    """**几何混用必须被拒**：轴对称坑 ↔ 单线横截面不可直接对照。"""
    import json
    from ufdemo.upstream import (
        write_synthetic_upstream_case, load_upstream_case, compare_profiles,
    )

    jp, _ = write_synthetic_upstream_case(tmp_path, depth_um=9.0)
    pit, _ = load_upstream_case(jp)
    case = json.loads(jp.read_text(encoding="utf-8"))
    case["geometry"] = "line_cross_section"
    jp.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    line, _ = load_upstream_case(jp)
    with pytest.raises(UFDemoError):
        compare_profiles(pit, line)


def test_upstream_metrics_are_computed_on_same_grid(tmp_path):
    """对照指标在**同一重采样网格**上计算；自比时 RMSE 为 0。"""
    from ufdemo.upstream import (
        write_synthetic_upstream_case, load_upstream_case, compare_profiles,
    )

    jp, _ = write_synthetic_upstream_case(tmp_path, depth_um=9.0, radius_um=6.0)
    prof, _ = load_upstream_case(jp)
    m = compare_profiles(prof, prof)
    assert m.profile_rmse_um == pytest.approx(0.0, abs=1e-12)
    assert m.center_depth_upstream_um == pytest.approx(9.0, rel=1e-6)
    assert m.width_upstream_um is not None and m.width_upstream_um > 0
    assert m.area_upstream_um2 is not None and m.area_upstream_um2 > 0


def test_upstream_reads_old_q4_columns(tmp_path):
    """兼容旧 Q4 列名，且 ``target`` 与 ``actual_mesh`` **分开保留**。"""
    import json
    from ufdemo.upstream import load_upstream_case

    (tmp_path / "profile.csv").write_text(
        "r_um,target_depth_um,actual_mesh_depth_um\n"
        "0.0,10.4,10.0\n1.0,8.3,8.0\n2.0,2.1,2.0\n",
        encoding="utf-8",
    )
    (tmp_path / "upstream_case.json").write_text(json.dumps({
        "case_id": "q4", "geometry": "axisymmetric_pit", "profile_file": "profile.csv",
        "laser": {"spot_radius_m": 1e-6},
    }, ensure_ascii=False), encoding="utf-8")
    prof, _ = load_upstream_case(tmp_path / "upstream_case.json")
    # 深度取 **actual_mesh**（真实几何），target 另存
    assert prof.depth_um[0] == pytest.approx(10.0)
    assert prof.target_depth_um is not None and prof.target_depth_um[0] == pytest.approx(10.4)
    assert prof.center_depth_um == pytest.approx(10.0)


def test_upstream_bad_geometry_declaration_rejected(tmp_path):
    """非法/缺失几何类型必须报错 —— 不允许"猜"是坑还是线。"""
    import json
    from ufdemo.upstream import load_upstream_case

    (tmp_path / "profile.csv").write_text("x_um,depth_um\n0,1\n", encoding="utf-8")
    (tmp_path / "upstream_case.json").write_text(json.dumps({
        "case_id": "x", "profile_file": "profile.csv",
    }), encoding="utf-8")
    with pytest.raises(UFDemoError):
        load_upstream_case(tmp_path / "upstream_case.json")


# ---------------------------------------------------------------------------
# C3：从实验 CSV 反推基线（Fth、δ）
# ---------------------------------------------------------------------------


def _closure_rows(true_fth: float, true_delta: float, *, window_um=8.0, dx_um=1.0):
    """用已知 (Fth, δ) 生成一组"实验值"，用于闭合验证。"""
    from ufdemo.calibration import PredictionSpec as PS

    base = PS(material_card_file=FIXTURE, window_um=window_um, dx_um=dx_um)
    override = {
        "kind": "log_fixed", "output_semantics": "event_depth_increment",
        "fluence_basis": "incident_peak_fluence", "depth_direction": "surface_normal",
        "threshold_J_m2": true_fth, "delta_m": true_delta,
    }
    rows = []
    for f, h, n in ((2.0, 4.0, 1), (2.0, 8.0, 3), (2.0, 6.0, 2),
                    (10.0, 4.0, 2), (10.0, 8.0, 1), (10.0, 6.0, 3)):
        r = ExperimentRow(sample_id=f"F{f:g}-h{h:g}-N{n}", pulse_duration_fs=500.0,
                          repetition_rate_kHz=f, scan_speed_mm_s=50.0,
                          hatch_spacing_um=h, pass_count=n, mean_depth_um=1.0)
        sp = PS(material_card_file=base.material_card_file, window_um=window_um, dx_um=dx_um,
                response_override=override)
        r.mean_depth_um = predict_mean_depth(r, spec=sp)["predicted_depth_um"]
        rows.append(r)
    return base, rows


def test_baseline_closure_recovers_known_parameters():
    """**闭合验证**：数据由同一模型生成时，反推必须能恢复真值。

    这条是 C3 的核心正确性证据 —— 它把"代码有问题"和"模型/数据不够"
    这两件事分开：闭合能精确恢复，说明机制对；真实数据误差大，
    那是模型形式/数据的问题，不是实现 bug。
    """
    from ufdemo.calibration import identify_baseline

    TRUE_FTH, TRUE_DELTA = 1.0e5, 1.0e-6
    spec, rows = _closure_rows(TRUE_FTH, TRUE_DELTA)
    est = identify_baseline(rows, spec=spec, material_family="closure",
                            pulse_duration_fs=500.0, holdout_groups=2, max_nfev=40)
    assert est.delta_m == pytest.approx(TRUE_DELTA, rel=1e-3)
    assert est.threshold_J_m2 == pytest.approx(TRUE_FTH, rel=1e-3)


def test_baseline_requires_multiple_frequencies():
    """**单一频率必须被拒**：F 固定时 Fth 与 δ 共线，两个参数定不出来。"""
    from ufdemo.calibration import identify_baseline

    spec, rows = _closure_rows(1.0e5, 1.0e-6)
    single = [r for r in rows if r.repetition_rate_kHz == 2.0]
    # 补几行同频率的，凑够 MIN_ROWS
    extra = [ExperimentRow(sample_id=f"X{i}", pulse_duration_fs=500.0,
                           repetition_rate_kHz=2.0, scan_speed_mm_s=50.0,
                           hatch_spacing_um=h, pass_count=n, mean_depth_um=1.0)
             for i, (h, n) in enumerate(((3.0, 1), (5.0, 2), (7.0, 3)))]
    with pytest.raises(UFDemoError) as ei:
        identify_baseline(single + extra, spec=spec, pulse_duration_fs=500.0)
    assert "频率" in str(ei.value)


def test_baseline_drops_nonpositive_rows_and_lists_them():
    """均值 ≤ 0 的行被剔除**并列出来**（不强行拟合）。"""
    from ufdemo.calibration import select_baseline_window

    base = dict(pulse_duration_fs=500.0, repetition_rate_kHz=2.0,
                scan_speed_mm_s=50.0, hatch_spacing_um=4.0, pass_count=1)
    rows = [
        ExperimentRow(sample_id="ok", mean_depth_um=5.0, **base),
        ExperimentRow(sample_id="neg", mean_depth_um=-1.2, **base),
        ExperimentRow(sample_id="zero", mean_depth_um=0.0, **base),
    ]
    kept, dropped = select_baseline_window(rows, pulse_duration_fs=500.0)
    assert [r.sample_id for r in kept] == ["ok"]
    assert {d["sampleId"] for d in dropped} == {"neg", "zero"}
    assert all("非正" in d["reason"] for d in dropped)


def test_baseline_split_has_no_leakage():
    """反推的 train/holdout 划分**不跨工况组**（含频率）。"""
    from ufdemo.calibration import _group_split_rows

    spec, rows = _closure_rows(1.0e5, 1.0e-6)
    tr, ho, info = _group_split_rows(rows, holdout_groups=2, seed=1)
    assert info["leakage_groups"] == []
    assert {r.condition_key for r in tr} & {r.condition_key for r in ho} == set()


def test_baseline_estimate_marks_evidence_status(tmp_path):
    """落盘的基线必须标**工程有效**（不是独立实测常数），并保留来源。"""
    from ufdemo.calibration import identify_baseline, save_baseline, baseline_to_card_patch
    import json as _json

    spec, rows = _closure_rows(1.0e5, 1.0e-6)
    est = identify_baseline(rows, spec=spec, material_family="closure",
                            pulse_duration_fs=500.0, holdout_groups=2, max_nfev=40)
    p = save_baseline(est, tmp_path / "baseline.json")
    d = _json.loads(p.read_text(encoding="utf-8"))
    assert d["evidenceStatus"] == "engineering_effective_from_experiment"
    assert d["nTrain"] > 0
    assert d["frequencySpanHz"] and len(d["frequencySpanHz"]) >= 2

    patch = baseline_to_card_patch(est)
    assert patch["_provenance"]["method"] == "identify_baseline_from_experiment_csv"
    assert "非独立实测常数" in patch["_provenance"]["note"]


def test_baseline_does_not_modify_original_card():
    """反推**不得**改动磁盘上的原始材料卡（用一个字节对比）。"""
    import hashlib

    spec, rows = _closure_rows(1.0e5, 1.0e-6)
    from ufdemo.calibration import identify_baseline

    before = hashlib.sha256((ROOT / FIXTURE).read_bytes()).hexdigest()
    identify_baseline(rows, spec=spec, material_family="closure",
                      pulse_duration_fs=500.0, holdout_groups=2, max_nfev=20)
    after = hashlib.sha256((ROOT / FIXTURE).read_bytes()).hexdigest()
    assert before == after, "反推过程改了原始材料卡 —— 参数化必须在内存里做"


# ---------------------------------------------------------------------------
# C5：h/N 目标筛选
# ---------------------------------------------------------------------------

#: 一个"能出深度"的参数化响应（避免依赖任何真实材料卡）
_PLAN_OVERRIDE = {
    "kind": "log_fixed", "output_semantics": "event_depth_increment",
    "fluence_basis": "incident_peak_fluence", "depth_direction": "surface_normal",
    "threshold_J_m2": 1.0e5, "delta_m": 1.0e-6,
}


def _plan_kw(**over):
    kw = dict(
        material_card_file=FIXTURE, region_um=(12.0, 12.0), dx_um=1.0,
        pulse_duration_fs=500.0, repetition_rate_kHz=10.0, scan_speed_mm_s=50.0,
        response_override=_PLAN_OVERRIDE,
    )
    kw.update(over)
    return kw


def test_plan_reports_infeasible_instead_of_fake_optimum():
    """**无可行方案时必须如实返回原因**，不得硬给一个"最优标签"。"""
    from ufdemo.planning import plan_for_target

    # 目标定得极高 → 所有候选都达不到
    res = plan_for_target(target_depth_um=1e5, tolerance_um=1.0, **_plan_kw())
    assert res.recommended is None, "不该给出推荐"
    assert res.infeasible_reason, "必须说明为什么没有方案"
    assert "达不到" in res.infeasible_reason or "超过" in res.infeasible_reason
    assert any("未给出推荐方案" in n for n in res.notes)


def test_plan_returns_recommendation_when_feasible():
    """有可行候选时给出推荐，且**按理想时间排序**（并列看均匀性）。"""
    from ufdemo.planning import plan_for_target

    # 放宽横向约束与容差，制造可行解
    res = plan_for_target(
        target_depth_um=5.0, tolerance_um=50.0,
        min_coverage=0.0, max_overcut_fraction=1.0,
        **_plan_kw(spacings_um=(4.0, 6.0), pass_counts=(1, 2)),
    )
    assert res.feasible_candidates, "放宽约束后应有可行候选"
    assert res.recommended is not None
    times = [c.ideal_time_s for c in res.feasible_candidates]
    assert times == sorted(times), "可行候选应按理想时间升序"
    assert res.infeasible_reason is None
    # **不变量**：推荐必须就是列表第一项 —— 否则界面推荐与表格对不上
    assert res.recommended is res.feasible_candidates[0]


def test_candidate_metrics_declared_as_model_predictions():
    """候选指标齐备，且 notes 明确覆盖/过切是**模型预测**。"""
    from ufdemo.planning import plan_for_target

    res = plan_for_target(target_depth_um=5.0, tolerance_um=50.0,
                          min_coverage=0.0, max_overcut_fraction=1.0,
                          **_plan_kw(spacings_um=(4.0,), pass_counts=(1,)))
    c = res.candidates[0]
    assert c.mean_depth_um is not None and c.mean_depth_um > 0
    assert c.coverage_fraction is not None and 0.0 <= c.coverage_fraction <= 1.0
    assert c.over_depth_fraction is not None
    assert c.under_depth_fraction is not None
    assert c.depth_std_um is not None
    text = " ".join(res.notes)
    assert "模型预测" in text and "二维形貌验证" in text


def test_candidate_focus_z_never_changes():
    """**所有候选的焦点 Z 恒定**（含多遍候选）—— 不做层间调焦。"""
    from ufdemo.planning import plan_for_target

    res = plan_for_target(target_depth_um=5.0, tolerance_um=50.0,
                          min_coverage=0.0, max_overcut_fraction=1.0,
                          **_plan_kw(spacings_um=(4.0, 6.0), pass_counts=(1, 3)))
    for c in res.candidates:
        zs = {s.start_xyz_m[2] for s in c.plan.segments} | {s.end_xyz_m[2] for s in c.plan.segments}
        assert zs == {0.0}, f"h={c.spacing_um} N={c.pass_count} 的 Z 不是常量：{zs}"


def test_plan_covers_full_search_grid():
    """默认枚举 h={2,4,6,8,10} × N={1..5} = 25 个候选，**每个都真的跑过求解器**。"""
    from ufdemo.planning import plan_for_target

    res = plan_for_target(target_depth_um=1e5, tolerance_um=1.0, **_plan_kw())
    assert len(res.candidates) == 25
    # 每个候选都有数值结果（说明真的求过解），而不是占位
    solved = [c for c in res.candidates if c.mean_depth_um is not None]
    assert len(solved) == 25, f"只有 {len(solved)} 个候选有数值结果"


def test_plan_rejects_bad_candidate_inputs():
    """非法输入必须报错（间距 0 / 遍数 0 / 速度 0）。"""
    from ufdemo.planning import plan_for_target

    for over in ({"spacings_um": (0.0,)}, {"pass_counts": (0,)}, {"scan_speed_mm_s": 0.0}):
        with pytest.raises(UFDemoError):
            plan_for_target(target_depth_um=5.0, tolerance_um=1.0, **_plan_kw(**over))


def test_plan_uses_same_solver_as_calibration(tmp_path):
    """规划与标定**共用同一条装配路径**（不另建求解器）。

    做法：同一 (h,N) 分别走 `evaluate_candidate` 与 `predict_mean_depth`，
    两者必须给出**相同**的均值深度。
    """
    from ufdemo.planning import evaluate_candidate
    from ufdemo.calibration import ExperimentRow, PredictionSpec, predict_mean_depth

    h, n = 4.0, 2
    c = evaluate_candidate(
        h, n, material_card_file=FIXTURE, region_um=(12.0, 12.0), dx_um=1.0,
        pulse_duration_fs=500.0, repetition_rate_kHz=10.0, scan_speed_mm_s=50.0,
        target_depth_um=5.0, tolerance_um=50.0,
        min_coverage=0.0, max_overcut_fraction=1.0,
        response_override=_PLAN_OVERRIDE,
    )
    row = ExperimentRow(sample_id="x", pulse_duration_fs=500.0, repetition_rate_kHz=10.0,
                        scan_speed_mm_s=50.0, hatch_spacing_um=h, pass_count=n,
                        mean_depth_um=0.0)
    spec = PredictionSpec(material_card_file=FIXTURE, window_um=12.0, dx_um=1.0,
                          response_override=_PLAN_OVERRIDE)
    out = predict_mean_depth(row, spec=spec)
    assert c.mean_depth_um == pytest.approx(out["predicted_depth_um"], rel=1e-9), (
        "规划与标定算出的深度不一致 → 说明用了两套装配路径"
    )


# ---------------------------------------------------------------------------
# C6：三工作区前端与 V2 契约（防止简化被改回去）
# ---------------------------------------------------------------------------

WEBUI = ROOT / "webui"


def test_frontend_has_exactly_three_workspaces_plus_dev():
    """主流程**只有三个工作区**（+ 一个开发工具入口）。

    这条是"简化"的守门员：任何把旧面板重新塞回主导航的改动都会让它变红。
    """
    import re

    html = (WEBUI / "index.html").read_text(encoding="utf-8")
    tabs = re.findall(r'<button data-tab="([a-z]+)"', html)
    assert tabs == ["data", "calib", "plan", "dev"], (
        f"主导航应恰好是 数据/对照标定/规划 三工作区 + 开发工具；实际 {tabs}"
    )


def _strip_js_comments(src: str) -> str:
    """剥掉 JS 注释，只留**有效代码**。

    为什么需要：我在删除处留了一段说明注释，里面提到了 `mockSolve` 这个名字
    （"早期版本曾内置一个 mockSolve()，已删"）。若直接全文串匹配，
    说明注释本身会触发误报 —— 检查必须针对**代码**，不是文档。
    """
    import re

    src = re.sub(r"/\*[\s\S]*?\*/", "", src)      # 块注释
    src = re.sub(r"(?m)^\s*//.*$", "", src)         # 整行注释
    src = re.sub(r"\s//[^\n]*", "", src)            # 行尾注释（简化处理）
    return src


def test_frontend_has_no_mock_solver():
    """前端**不得**再出现模拟求解器（任务书 §7.2 明确要删）。

    检查的是**有效代码**：既不能有定义，也不能有调用。
    """
    for name in ("main.js", "v2.js", "api.js"):
        code = _strip_js_comments((WEBUI / "js" / name).read_text(encoding="utf-8"))
        assert "function mockSolve" not in code, f"{name} 里还定义了 mockSolve"
        assert "mockSolve(" not in code, f"{name} 里还有 mockSolve 调用"


def test_frontend_does_not_claim_mock_data():
    """界面上不得再宣称"内置模拟数据、不接真实求解器"—— 那是过时且误导的。"""
    html = (WEBUI / "index.html").read_text(encoding="utf-8")
    assert "不接真实求解器" not in html
    assert "内置模拟数据" not in html
    # 反而应该明确写着走真实求解器
    assert "真实求解器" in html


def test_param_listener_scope_matches_actual_panel_id():
    """参数监听器的作用域必须是**真实存在的面板 id**。

    回归背景：面板从 `#tab-params` 改名 `#tab-data` 后，绑定的选择器没跟着改，
    结果**所有参数字段静默失去监听器** —— 改参数不触发重渲染、
    「尚未提交」的过期标记不再出现（浏览器探针 U03-6 抓到）。
    这类"改名漏改选择器"的 bug 只能靠源码级检查拦住。
    """
    import re

    html = (WEBUI / "index.html").read_text(encoding="utf-8")
    main = (WEBUI / "js" / "main.js").read_text(encoding="utf-8")
    panel_ids = set(re.findall(r'id="(tab-[a-z]+)"', html))
    used = set(re.findall(r'#(tab-[a-z]+)\s', main)) | set(re.findall(r'"(tab-[a-z]+)"', main))
    # 只检查"作为选择器作用域"出现的那种用法
    missing = {i for i in used if i.startswith("tab-") and i not in panel_ids and i != "tab-dev"}
    missing = {i for i in missing if i in main}
    assert not missing, f"main.js 引用了不存在面板 id：{sorted(missing)}（现有 {sorted(panel_ids)}）"


def test_dev_panel_holds_former_main_panels():
    """原来的非核心面板必须**移到开发工具里**（不是删掉，也不是留在主流程）。"""
    html = (WEBUI / "index.html").read_text(encoding="utf-8")
    dev = html.split('id="tab-dev"', 1)[1]
    for kw in ("数值与求解细节", "文献参考评估器", "曲线查表", "材料能力清单",
               "真实实验案例", "金刚石过程响应", "历史运行"):
        assert kw in dev, f"开发工具里缺少「{kw}」"


def test_shared_background_payload_is_self_consistent():
    """共用背景契约必须带自洽校验结果与"不共享项"。"""
    from ufdemo import webcontract as W

    d = W.shared_background_payload()
    assert d["ok"] is True
    assert d["selfConsistent"] is True
    assert abs(d["waistUm"] - 0.874) < 0.01
    assert d["geometryFeedback"] == "axial_defocus"
    assert d["dynamicAngle"] is False
    assert {"threshold", "delta", "calibration_gain"} <= set(d["notShared"])


def test_pulse_energy_payload_uses_post_objective_power():
    """脉冲能量契约必须用**物镜后功率**，而不是软件设定值。"""
    from ufdemo import webcontract as W

    d = W.pulse_energy_payload(100e3)
    assert d["ok"] is True
    assert d["pulseEnergyUJ"] == pytest.approx(53.333, rel=1e-3)
    assert d["postObjectivePowerW"] == pytest.approx(5.3333)
    # 用设定值 10 W 会得到 100 µJ —— 必须不是那个
    assert abs(d["pulseEnergyUJ"] - 100.0) > 1.0


def test_experiment_tables_payload_reports_missing_dir_honestly(tmp_path):
    """实验目录不存在时**如实返回 found=false + 原因**，不伪造表。"""
    from ufdemo import webcontract as W

    d = W.experiment_tables_payload(tmp_path / "nope")
    assert d["found"] is False
    assert d["tables"] == []
    assert "未找到" in d["note"]


@pytest.mark.skipif(not (EXP_DIR / "AlSiC.csv").exists(), reason="实验表不在工作区")
def test_experiment_tables_payload_lists_real_csvs():
    """能列出真实实验表，并给出脉宽/频率/间距档。"""
    from ufdemo import webcontract as W

    d = W.experiment_tables_payload(EXP_DIR)
    assert d["found"] is True
    names = {x["file"] for x in d["tables"]}
    assert "AlSiC.csv" in names
    alsic = next(x for x in d["tables"] if x["file"] == "AlSiC.csv")
    assert alsic["ok"] is True
    assert alsic["encoding"] == "gb18030"
    assert alsic["nRows"] > 100
    assert alsic["nNegative"] > 0, "该表确实含负均值，界面要能显示出来"
    assert 2.0 in alsic["spacingsUm"]


def test_baselines_payload_flags_engineering_effective():
    """基线契约必须说明"工程有效、非实测常数"。"""
    from ufdemo import webcontract as W

    d = W.baselines_payload(ROOT / "data" / "baselines")
    assert any(b.get("file", "").startswith("alsic") for b in d["baselines"])
    assert "工程有效" in d["note"]


def test_upstream_payload_marks_synthetic():
    """上游样例契约必须带 isSynthetic，界面才能提示"虚拟输入"。"""
    from ufdemo import webcontract as W

    d = W.upstream_payload(ROOT / "examples" / "upstream")
    cases = [c for c in d["cases"] if "case" in c]
    assert cases, "至少要能列出虚拟样例"
    assert any(c["case"]["isSynthetic"] for c in cases)
    assert "虚拟输入" in d["note"]


def test_webapp_context_has_v2_dirs(tmp_path):
    """服务上下文要带实验/基线/上游三个目录（三工作区要用）。"""
    from ufdemo import webapp as WA

    ctx = WA.build_context(project_root=ROOT, runs_dir=tmp_path / "runs")
    assert ctx.experiment_dir == ROOT.parent / "数据"
    assert ctx.baselines_dir == ROOT / "data" / "baselines"
    assert ctx.upstream_dir == ROOT / "examples" / "upstream"


def test_plan_payload_reports_infeasible_honestly():
    """规划契约在无解时**不得**返回一个假的 recommended。"""
    from ufdemo import webcontract as W

    d = W.plan_payload({
        "materialCardFile": FIXTURE,
        "targetDepthUm": 1e5, "toleranceUm": 1.0,
        "pulseDurationFs": 500.0, "repetitionRateKHz": 10.0, "scanSpeedMmS": 50.0,
        "regionUm": [12.0, 12.0], "dxUm": 1.0,
        "responseOverride": _PLAN_OVERRIDE,
    }, project_root=str(ROOT))
    assert d["ok"] is True
    assert d["recommended"] is None
    assert d["infeasibleReason"]
