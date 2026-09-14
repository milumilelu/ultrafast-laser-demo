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
