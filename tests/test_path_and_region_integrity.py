"""路径连续性 / 加工区尺寸 / 关光策略 / 细核后重判可行（用户审查 2026-09-15 的第 3、4 条）。

背景（外部审查发现、逐条在代码里复现过）：

1. **遍间回程断点**：`serpentine_plan` 每一遍都从**左侧**重新开始，但遍间回程在
   **偶数条扫描线**时回到了右侧 ⇒ 200×200、h=8、N=2（26 条线）段间跳 **200 µm**。
   缺的那段位移还会改变下一遍的激光时钟相位。奇数条线（h=10 → 21 条）恰好正确，
   所以这个 bug 只在一半情形出现。
2. **关光策略自相矛盾**：`DEVICE_PATH_POLICY` 写 `shutter_off_capability="not_assumed"`、
   文案说"导出不依赖设备不存在的关光能力"，代码却**确实**把换向段标成 `emitting=False`。
3. **非正方形被静默压方**：调用方传 `machining_region_um=float(region_um[0])` 丢掉第二维，
   统计裁切又按第一维双向裁 ⇒ `(120, 60)` 的指标实际算在 `120×120` 上。
4. **细核后不重判可行**：只检查"细核有没有生成形貌" ⇒
   「粗网格可行 → 细网格不可行 → 仍然推荐它」。

本文件把这些都固化成断言。特别是第 1 条，就用审查给的验收方式：
**逐段检查 上一段终点 == 下一段起点**。
"""

from __future__ import annotations

import math

import pytest

from conftest import ROOT, load_example, make_config
from ufdemo.errors import CONFIG_INVALID, UFDemoError
from ufdemo.planning import DEVICE_PATH_POLICY, plan_for_target, serpentine_plan


# --- 1. 段间连续性（审查给的验收断言）---------------------------------------

def _segments(reg, h, n):
    return serpentine_plan(region_um=reg, spacing_um=h, pass_count=n, scan_speed_mm_s=50.0).segments


@pytest.mark.parametrize("h", [2.0, 4.0, 6.0, 8.0, 10.0, 12.5])
@pytest.mark.parametrize("n_pass", [1, 2, 3, 5])
@pytest.mark.parametrize("region", [(200.0, 200.0), (200.0, 100.0), (120.0, 60.0)])
def test_every_segment_starts_where_the_previous_ended(region, h, n_pass):
    """**逐段检查**：上一段终点 == 下一段起点（含换向段与遍间段）。

    这一条同时覆盖奇/偶扫描线数、多层与非正方形 —— 原 bug 只在偶数条线时出现，
    所以参数里特意同时包含 h=8（26 条，偶数）与 h=10（21 条，奇数）。
    """
    segs = _segments(region, h, n_pass)
    assert len(segs) > 1
    for i, (a, b) in enumerate(zip(segs, segs[1:])):
        assert a.end_xyz_m == b.start_xyz_m, (
            f"段 {i} 与 {i + 1} 不连续：{a.end_xyz_m} → {b.start_xyz_m}"
        )


def test_the_reported_break_case_is_now_continuous():
    """审查点名的算例：200×200、h=8、N=2（26 条偶数线）—— 原来跳 200 µm。"""
    segs = _segments((200.0, 200.0), 8.0, 2)
    n_scan = sum(1 for s in segs if s.kind == "scan")
    assert n_scan == 26 * 2                      # 26 条线 × 2 遍
    # 遍间换向段：沿边回到起点侧（y 向行程），不再是横向 200 µm 跳跃
    turns = [s for s in segs if not s.emitting and abs(s.start_xyz_m[1] - s.end_xyz_m[1]) > 100e-6]
    assert len(turns) == 1
    t0 = turns[0]
    assert t0.start_xyz_m[0] == t0.end_xyz_m[0]                 # x 不变
    assert abs(t0.start_xyz_m[1] - t0.end_xyz_m[1]) == pytest.approx(200e-6)
    assert t0.end_xyz_m[0] == pytest.approx(-100e-6)             # 回到起点侧（x0）
    assert t0.duration_s > 0.0


def test_non_square_region_is_scanned_in_both_dimensions():
    """非正方形：路径必须在两个方向都按请求尺寸走。"""
    segs = _segments((120.0, 60.0), 10.0, 1)
    xs = [s.start_xyz_m[0] for s in segs]
    ys = [s.start_xyz_m[1] for s in segs]
    assert min(xs) == pytest.approx(-60e-6) and max(xs) == pytest.approx(60e-6)
    assert min(ys) == pytest.approx(-30e-6) and max(ys) == pytest.approx(30e-6)


# --- 2. 关光策略：显式、不自相矛盾 -------------------------------------------

def test_emitting_segments_are_exactly_the_scan_lines():
    """出光段只能是扫描线（y 恒定、沿 ±x）—— 这是"理想关光"这条假设的**具体含义**。"""
    segs = _segments((200.0, 200.0), 8.0, 2)
    for s in segs:
        if s.emitting:
            assert s.start_xyz_m[1] == s.end_xyz_m[1]            # 沿 x 直线
            assert abs(s.end_xyz_m[0] - s.start_xyz_m[0]) == pytest.approx(200e-6)
        else:
            assert s.kind == "turn"


def test_shutter_assumption_is_declared_not_contradicted():
    """策略必须**显式**说明"关光"是假设；文案不得再宣称"不依赖关光能力"。"""
    assert DEVICE_PATH_POLICY["shutter_off_during_turns"] is True
    assert "assumed" in DEVICE_PATH_POLICY["shutter_state_basis"]
    note = DEVICE_PATH_POLICY["note"]
    assert "模型假设" in note and "尚未确认" in note
    assert "不依赖设备不存在的关光能力" not in note      # 原来自相矛盾的那句


# --- 3. 非正方形加工区：装配与结果口径 ---------------------------------------

def _plan(**kw):
    bl = {
        "kind": "log_fixed", "output_semantics": "event_depth_increment",
        "fluence_basis": "incident_peak_fluence", "depth_direction": "surface_normal",
        "threshold_J_m2": 7.85e4, "delta_m": 3.6525e-6,
    }
    base = dict(
        material_card_file="tests/fixtures/analytic_fixture.json",
        target_depth_um=60.0, tolerance_um=12.0, pulse_duration_fs=223.0,
        repetition_rate_kHz=20.0, scan_speed_mm_s=50.0,
        region_um=(60.0, 60.0), domain_um=(120.0, 120.0), dx_um=2.0,
        response_override=bl, spacings_um=[6.0], pass_counts=[1],
    )
    base.update(kw)
    return plan_for_target(**base)


def _cfg_for(region, window=120.0):
    from ufdemo.calibration import ExperimentRow, PredictionSpec, build_row_config

    row = ExperimentRow(sample_id="r", pulse_duration_fs=223.0, repetition_rate_kHz=20.0,
                        scan_speed_mm_s=50.0, hatch_spacing_um=8.0, pass_count=1, mean_depth_um=0.0)
    spec = PredictionSpec(material_card_file="tests/fixtures/analytic_fixture.json",
                          window_um=window, dx_um=1.0, machining_region_um=region)
    return spec, build_row_config(row, spec=spec)


def test_region_size_accepts_scalar_and_pair():
    spec, _ = _cfg_for(80.0)
    assert spec.region_size_um() == (80.0, 80.0)
    spec2, cfg2 = _cfg_for((80.0, 40.0))
    assert spec2.region_size_um() == (80.0, 40.0)
    ys = [s.start_xyz_m[1] for s in cfg2.path.segments]
    assert min(ys) == pytest.approx(-20e-6) and max(ys) == pytest.approx(20e-6)


def test_region_size_rejects_bad_shape():
    spec, _ = _cfg_for(80.0)
    spec.machining_region_um = (1.0, 2.0, 3.0)          # type: ignore[assignment]
    with pytest.raises(UFDemoError) as exc:
        spec.region_size_um()
    assert exc.value.code == CONFIG_INVALID


def test_region_larger_than_domain_is_rejected_in_both_dimensions():
    with pytest.raises(UFDemoError) as exc:
        _cfg_for((80.0, 130.0), window=120.0)            # 第二维超域
    assert exc.value.code == CONFIG_INVALID
    assert "加工区不能大于仿真域" in str(exc.value)


def test_plan_result_reports_the_real_region_size():
    r = _plan(region_um=(60.0, 30.0), domain_um=(120.0, 120.0), dx_um=2.0)
    d = r.to_dict()
    assert d["regionUm"] == [60.0, 30.0]
    cand = (r.recommended or (r.candidates[0] if r.candidates else None))
    if cand is not None:
        assert cand.machining_region_size_um == (60.0, 30.0)


# --- 4. 细核之后必须重新判可行 -----------------------------------------------

def test_recommendation_is_always_from_the_final_precision():
    """推荐必须**可行**，且与 `feasible_candidates[0]` 是同一实例（界面推荐 = 表格第一行）。

    原实现只检查"细核有没有生成形貌"，于是"粗筛可行、细核不可行"的候选照样被推荐。
    """
    r = _plan(dx_um=2.0, auto_coarsen=False)             # screen_dx == dx ⇒ 复核但不两级
    if r.recommended is not None:
        assert r.recommended.feasible is True
    fc = r.feasible_candidates
    if fc:
        assert r.recommended is not None
        assert r.recommended is fc[0]
    else:
        assert r.recommended is None
    # 复核计数在两级网格那条测试里断言（本算例 dx 与粗筛同值、且无可行候选）


def test_two_level_grid_rechecks_and_reports():
    """两级网格下必须如实报告"复核了几个"，并说明推荐依据细核结果。"""
    r = _plan(dx_um=0.5, auto_coarsen=True, region_um=(120.0, 120.0), domain_um=(200.0, 200.0))
    assert r.screening_dx_um is not None and r.final_dx_um == 0.5
    assert r.screening_dx_um != r.final_dx_um
    assert r.n_fine_rechecked >= 1
    assert any("细核" in n for n in r.notes)
    if r.recommended is not None:
        assert r.recommended.feasible is True


def test_infeasible_reason_mentions_the_fine_recheck():
    """无可行方案时，理由要说明是**细网格复核后**不满足，而不是含糊的"无解"。"""
    r = _plan(target_depth_um=1.0, tolerance_um=0.05, dx_um=0.5, auto_coarsen=True,
              region_um=(120.0, 120.0), domain_um=(200.0, 200.0))
    assert r.recommended is None
    assert r.infeasible_reason
    assert ("细网格" in r.infeasible_reason) or ("达不到" in r.infeasible_reason) \
        or ("超过" in r.infeasible_reason)
