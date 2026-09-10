"""G02：十个相同脉冲（执行细则 10 节 / 任务书 10 节）。

关闭离焦和所有历史更新：中心深度 2 μm，体积 314.159265358979 μm³。
打开离焦/孵化后不再要求线性，因此本文件不把不同模型条件的结果互相验收。
"""

from __future__ import annotations

import pytest

from conftest import PEAK_DEPTH, V_ANALYTIC, load_example, make_config
from ufdemo.materials import load_material_card
from ufdemo.solver import solve

pytestmark = pytest.mark.g02

N_PULSES = 10


def _run():
    raw = load_example("ten_pulses.json")
    cfg = make_config(raw)
    material = load_material_card(raw["material_card_file"])
    return solve(cfg, material)


def test_event_count_is_ten():
    res = _run()
    assert res.status == "completed"
    assert res.events_processed == N_PULSES
    assert len(res.events_rows) == N_PULSES
    times = [float(r["time_s"]) for r in res.events_rows]
    assert times == pytest.approx([j / 1000.0 for j in range(N_PULSES)], rel=1e-12)
    # 定点驻留：位置全部相同
    assert {r["focus_x_m"] for r in res.events_rows} == {0.0}


def test_center_depth_is_2um():
    res = _run()
    assert res.statistics["center_depth_internal"] == pytest.approx(N_PULSES * PEAK_DEPTH, rel=1e-12)
    assert N_PULSES * PEAK_DEPTH == pytest.approx(2.0e-6, rel=1e-12)


def test_volume_is_ten_times_single_pulse():
    res = _run()
    expected = N_PULSES * V_ANALYTIC
    assert expected * 1e18 == pytest.approx(314.159265358979, rel=1e-12)
    assert res.statistics["removal_volume_internal"] == pytest.approx(expected, rel=1e-2)


def test_history_stays_disabled():
    res = _run()
    assert res.metadata["enabled_features"]["history_enabled"] is False
    assert res.metadata["enabled_features"]["geometry_feedback"] == "fixed_geometry"
    # 关闭历史时，局部计数仍记录，但不得作为孵化输入
    assert res.statistics["max_exposure_count"] == N_PULSES
