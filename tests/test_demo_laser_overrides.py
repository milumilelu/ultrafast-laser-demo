"""演示端点的激光/工艺参数覆盖：**可自由设置**，不再是"必须匹配某个工况点"。

背景（ADR-0022）：材料卡原先存的是"加工有效阈值"= 文献模型
``Fth(N) = Fth1 · N^(S-1)`` 在 N=3 处的**取值**（12890），于是 τ/f/v 被焊死在一点上。
现在卡里存的是**单脉冲常数 Fth1 = 14830** 与模型本身，求解器逐点追踪累积次数 N
并算阈值 —— 工艺参数因此自由。

本文件守两件事：
1. 覆盖**能生效**（值确实进了配置，不是被忽略）；
2. 仍然**不能越出参数自身的定义域**（λ/τ 是 Fth1 与 S 的标定条件）。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from conftest import ROOT

from ufdemo.errors import CONDITION_MISMATCH
from ufdemo.materials import load_material_card
from ufdemo.webcontract import demo_rect_payload

CARD = "data/materials/zirconia_ysz_machining_effective_n3.json"
#: 小区域 + 单遍，让每条用例几秒内跑完（本文件只关心门禁与参数传递）
BASE = {"materialCardFile": CARD, "regionUm": 60.0, "hatchUm": 4.0,
        "passes": 1, "dxUm": 2.0}


def _spec():
    return load_material_card(ROOT / CARD)


def _run(tmp_path, **over):
    return demo_rect_payload({**BASE, **over}, out_base=tmp_path,
                             project_root=ROOT)


def _ok(tmp_path, **over):
    out = _run(tmp_path, **over)
    assert out["status"] == "completed", out.get("errors")
    return out


def test_estimate_only_returns_plan_without_creating_a_run(tmp_path):
    """预估阶段复用同一配置校验，但不得执行求解或写入运行结果。"""
    out = demo_rect_payload(
        {**BASE, "processMode": "actual", "pulseDurationFs": 208.0,
         "repetitionRateKHz": 40.0, "speedMmS": 20.0},
        out_base=tmp_path,
        project_root=ROOT,
        estimate_only=True,
    )
    assert out["status"] == "estimated"
    assert out["estimatedEvents"] > 0
    assert out["estimatedSeconds"] > 0
    assert not list(tmp_path.rglob("metadata.json"))


# ---------------------------------------------------------------------------
# 1. 覆盖能生效
# ---------------------------------------------------------------------------


def test_default_uses_protocol_values(tmp_path):
    """不传工艺参数时按协议走（回归保护）。"""
    dm = _ok(tmp_path)["demo"]
    base = dm["baseline"]
    assert dm["pulseDurationFs"] == pytest.approx(base["pulseDurationFs"])
    assert dm["repetitionRateKHz"] == pytest.approx(base["repetitionRateKHz"])
    assert dm["speedMmS"] == pytest.approx(base["speedMmS"], rel=1e-9)


@pytest.mark.parametrize("v_mm_s", [150.0, 400.0, 600.0])
def test_speed_is_free(tmp_path, v_mm_s):
    """**扫描速度自由设置**：不再与频率联动，也不再看 N_eff 脸色。"""
    dm = _ok(tmp_path, speedMmS=v_mm_s)["demo"]
    assert dm["speedMmS"] == pytest.approx(v_mm_s, rel=1e-9)


def test_frequency_is_free(tmp_path):
    """**重复频率自由设置**：改 f 不再带动速度（旧实现会联动）。"""
    dm30 = _ok(tmp_path, repetitionRateKHz=30.0)["demo"]
    dm80 = _ok(tmp_path, repetitionRateKHz=80.0)["demo"]
    assert dm30["repetitionRateKHz"] == pytest.approx(30.0)
    assert dm80["repetitionRateKHz"] == pytest.approx(80.0)
    # 速度没被动过 ⇒ 两次都一样（都取协议基准）
    assert dm30["speedMmS"] == pytest.approx(dm80["speedMmS"], rel=1e-9)


def test_pulse_duration_is_free_within_tolerance(tmp_path):
    spec = _spec()
    req = spec.reference_protocol["required_laser"]["pulse_duration_s"]
    tol = req["rel_tol"]
    for scale in (0.97, 1.0, 1.03):
        dm = _ok(tmp_path, pulseDurationFs=req["value"] * scale * 1e15)["demo"]
        assert dm["pulseDurationFs"] == pytest.approx(req["value"] * scale * 1e15)
    assert tol > 0


def test_process_parameters_change_the_result(tmp_path):
    """工艺参数真的进了物理：扫得越慢（每点脉冲越多）越深，反之越浅。"""
    slow = _ok(tmp_path, speedMmS=150.0)
    fast = _ok(tmp_path, speedMmS=600.0)
    d_slow = slow["stats"]["center_depth_internal"]
    d_fast = fast["stats"]["center_depth_internal"]
    assert slow["eventsProcessed"] > fast["eventsProcessed"]
    assert d_slow > d_fast, f"慢扫应更深：{d_slow:.3e} vs {d_fast:.3e}"


# ---------------------------------------------------------------------------
# 2. 阈值模型必须被如实回显（界面靠它说明"为什么工艺自由"）
# ---------------------------------------------------------------------------


def test_incubation_model_is_reported(tmp_path):
    """返回体要给出文献模型与 Fth1 —— 否则界面无法解释阈值的来源。"""
    spec = _spec()
    dm = _ok(tmp_path)["demo"]
    inc = dm["incubation"]
    assert inc and inc["enabled"]
    assert inc["Fth1Jm2"] == pytest.approx(spec.response["threshold_J_m2"])
    assert inc["Fth1Jm2"] == pytest.approx(14830.0)
    assert "N^(S-1)" in inc["model"]
    # S(f) = 0.969 − 0.0029·f[kHz]
    f_khz = dm["repetitionRateKHz"]
    assert inc["S"] == pytest.approx(0.969 - 0.0029 * f_khz, rel=1e-9)


def test_baseline_is_protocol_reference_not_the_override(tmp_path):
    """`demo.baseline` 必须是**协议基准**，不能被本次覆盖污染。"""
    dm = _ok(tmp_path, repetitionRateKHz=45.0, pulseDurationFs=205.0)["demo"]
    b, spec = dm["baseline"], _spec()
    sb = spec.reference_protocol["source_beam"]
    assert b["pulseDurationFs"] == pytest.approx(
        spec.reference_protocol["required_laser"]["pulse_duration_s"]["value"] * 1e15)
    assert b["repetitionRateKHz"] == pytest.approx(sb["repetition_rate_Hz"] / 1e3)
    assert dm["repetitionRateKHz"] == pytest.approx(45.0)      # 覆盖生效
    assert b["repetitionRateKHz"] != pytest.approx(45.0)       # 基准未污染


# ---------------------------------------------------------------------------
# 3. 仍不能越出**参数自身的定义域**
# ---------------------------------------------------------------------------


def test_pulse_duration_outside_tolerance_is_rejected(tmp_path):
    """τ 超出协议 ±5% ⇒ `CONDITION_MISMATCH`。

    λ/τ 是 Fth1 与 S(f) 的**标定条件**（换波长/脉宽要重新标定），
    这属于参数自身的定义域，不是对工艺自由度的限制。

    注：`demo_rect_payload` **不抛异常** —— 校验失败放进返回体（`status=failed`）。
    """
    req = _spec().reference_protocol["required_laser"]["pulse_duration_s"]
    bad = req["value"] * (1 + req["rel_tol"]) * 1.5
    out = _run(tmp_path, pulseDurationFs=bad * 1e15)
    assert out["status"] == "failed"
    assert CONDITION_MISMATCH in [e.get("code") for e in (out.get("errors") or [])]


def test_wavelength_outside_tolerance_is_rejected(tmp_path):
    """λ 同理（协议声明在 required_laser 里，容差 ±1%）。"""
    from ufdemo.config import RunConfig, validate_run

    spec = _spec()
    req = spec.reference_protocol["required_laser"]
    assert "wavelength_m" in req, "λ 应仍是协议要求项"
    # 直接用 validate_run 检查（demo 端点不让传 λ，它是设备量）
    raw = {
        "schema_version": "1.0", "run_mode": "reference_case", "unit_system": "SI",
        "material_id": spec.id, "material_card_file": CARD, "seed": 0,
        "grid": {"nx": 41, "ny": 41, "dx_m": 1e-6, "dy_m": 1e-6,
                 "center_x_m": 0.0, "center_y_m": 0.0, "origin": "cell_center",
                 "initial_surface": "flat", "initial_height_m": 0.0},
        "laser": {"wavelength_m": req["wavelength_m"]["value"] * 1.3,
                  "pulse_duration_s": req["pulse_duration_s"]["value"],
                  "pulse_energy_J": 2.0e-4, "repetition_rate_Hz": 33300.0,
                  "spot_radius_m": 1.6e-5, "focus_xyz_m": [0.0, 0.0, 0.0],
                  "direction_unit": [0.0, 0.0, 1.0]},
        "path": {"t0_s": 0.0, "segments": [
            {"segment_id": 0, "pass_id": 0, "start_s": 0.0, "end_s": 1 / 33300.0,
             "start_xyz_m": [0, 0, 0], "end_xyz_m": [0, 0, 0], "laser_on": True}]},
        "solver": {"mode": "reference", "geometry_feedback": "fixed_geometry",
                   "history_enabled": False},
        "output": {"snapshot_policy": "none"},
        "reference_conditions": {"protocol_id": spec.reference_protocol["protocol_id"],
                                 "fluence_basis": "incident_peak_fluence"},
    }
    rep = validate_run(RunConfig.from_dict(raw), spec)
    assert not rep.ok
    assert any(e.get("code") == CONDITION_MISMATCH for e in rep.errors)


def test_arbitrary_effective_count_is_no_longer_a_gate(tmp_path):
    """**N_eff 不再是门禁条件**（ADR-0022）。

    过去传一个不等于卡值的 effective_count 会被拒；现在它根本不被检查 ——
    阈值由模型逐点算，N 是多少由路径决定。
    """
    out = _ok(tmp_path, speedMmS=400.0)     # 该速度对应 N_eff ≈ 2.09，过去必被拒
    assert out["status"] == "completed"
    assert out["eventsProcessed"] > 0
