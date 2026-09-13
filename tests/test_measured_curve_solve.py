"""U07：**真实数据驱动真实求解**的端到端回归。

这是本项最重要的测试 —— 它守住「求解器真的能吃实测数据」这条通路。

背景（这条通路此前是**断的**）：
* `data/curves/` 里曾经只有 fixture（公式/解析/合成）→ 求解器只能"合成演示"；
* `solver.py` 调 `build_pulse_law(material, unit=...)` **从不传 curve**；
* `ui_service.submit` **不传 curves_dir** → 界面即便选了曲线也加载不到；
* 曲线 x 用惯用单位（J/cm²），核收到的是内部 SI（J/m²）—— **没有换算层**，
  于是实测曲线一律"高于上界"被拒，一条都用不了。

本文件把上述四点全部钉住，任何一处退化都会红。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from ufdemo import ui_service as U
from ufdemo.config import RunConfig, validate_run
from ufdemo.errors import UFDemoError
from ufdemo.materials import load_material_card
from ufdemo.response import TabulatedEventLaw, build_pulse_law
from ufdemo.solver import solve

ROOT = Path(__file__).resolve().parents[1]
CURVES = ROOT / "data" / "curves"
EXAMPLES = ROOT / "examples"
MEASURED_CURVE = "sic4h_measured_single_pulse_crater.curve.json"
EXAMPLE = "sic4h_measured_single_pulse.json"
SIC_CARD = ROOT / "data" / "materials" / "sic_4h_cface_1035nm_multishot.json"

pytestmark = pytest.mark.u07

#: 实测 4 点（能流 J/cm² → 坑深 μm）—— 与论文实验值一致
MEASURED_POINTS = [(6.92, 0.35), (8.10, 0.51), (9.99, 0.66), (10.82, 0.72)]


@pytest.fixture(scope="module")
def curve():
    if not (CURVES / MEASURED_CURVE).exists():
        pytest.skip("实测曲线卡不存在（先跑 tools/make_measured_curves.py）")
    return U.load_curve_card(CURVES, MEASURED_CURVE)


# ---------------------------------------------------------------------------
# 1. 曲线卡本身：真的来自实测，且语义可进主循环
# ---------------------------------------------------------------------------


def test_measured_card_is_real_measurement(curve):
    """这张卡必须是**实测**来源，不是解析/合成。"""
    assert curve.output_semantics == "event_depth_increment", "必须可进逐事件主循环"
    assert curve.can_enter_event_kernel is True
    assert curve.source_type == "published_measurement", (
        f"来源类型应为实测；实际 {curve.source_type}"
    )
    assert curve.evidence_status == "literature_reported", (
        "4 点无重复测量 → 证据状态只能停在 literature_reported"
    )


def test_measured_card_points_match_paper(curve):
    """曲线点位必须与论文实验值逐一对应（不得混入同文的模型预测列）。"""
    assert len(curve.points) == 4
    for (x, y_um), (cx, cy) in zip(MEASURED_POINTS, curve.points):
        assert cx == pytest.approx(x, rel=1e-12), f"x 不符：{cx} vs {x}"
        assert cy == pytest.approx(y_um * 1e-6, rel=1e-12), f"y 不符：{cy} vs {y_um}μm"


def test_measured_card_declares_assumptions(curve):
    """必须显式声明假设（峰值中心≈局部、不含 Gaussian 尾部、低能流保守截断）。"""
    text = " ".join(list(curve.limitations) + list(curve.notes))
    assert "局部" in text, "须声明「峰值中心关系近似局部响应」"
    assert "阈值" in text, "须声明不含完整阈值律"
    assert "低估" in text or "保守" in text, "须声明低能流区的保守截断及其方向"


# ---------------------------------------------------------------------------
# 2. 单位换算：核的入口是 SI，曲线是惯用单位
# ---------------------------------------------------------------------------


def test_unit_conversion_at_kernel_entry(curve):
    """**单位换算层存在且正确**。

    回归背景：没有这层时，6.92 J/cm² 的曲线收到 69200 J/m² 的输入会
    直接判「高于上界」→ 实测曲线一条都用不了。
    """
    law = TabulatedEventLaw(curve, laser={"wavelength_m": 1030e-9,
                                           "pulse_duration_s": 300e-15})
    assert law.x_to_si == pytest.approx(1.0e4), "J/cm² → J/m² 应为 1e4"
    assert law.y_to_si == pytest.approx(1.0), "m → m 应为 1"

    # 喂**内部 SI**（J/m²），得到的应是曲线上的坑深（SI 米）
    F = np.array([[x * 1e4 for x, _ in MEASURED_POINTS]])
    got = np.asarray(law.increment(F, None).values)[0]
    for g, (_, y_um) in zip(got, MEASURED_POINTS):
        assert float(g) == pytest.approx(y_um * 1e-6, rel=1e-9), (
            f"查表值 {float(g)*1e6:.4f} μm 应等于实测 {y_um} μm"
        )


def test_unknown_unit_is_rejected_not_defaulted():
    """未知单位必须**报错**，不得默认按 1.0 处理（那会静默错算）。"""
    from ufdemo.response import _unit_to_si_factor

    with pytest.raises(UFDemoError):
        _unit_to_si_factor("furlong/fortnight", where="test")


def test_above_range_still_rejected(curve):
    """单位换算**没有**顺手放宽越界：上界之外仍拒绝。"""
    law = TabulatedEventLaw(curve, laser={"wavelength_m": 1030e-9,
                                           "pulse_duration_s": 300e-15})
    with pytest.raises(UFDemoError) as ei:
        law.increment(np.array([[1e12]]), None)
    assert ei.value.code == "TABLE_OUT_OF_RANGE"


# ---------------------------------------------------------------------------
# 3. 端到端：真实曲线驱动**真实求解**
# ---------------------------------------------------------------------------


def _run_example(**override) -> tuple[RunConfig, object, object]:
    raw = json.loads((EXAMPLES / EXAMPLE).read_text(encoding="utf-8"))
    raw.update(override)
    cfg = RunConfig.from_dict(raw, base_dir=str(ROOT))
    mat = load_material_card(SIC_CARD)
    return cfg, mat, raw


def test_example_declares_response_curve():
    """示例必须显式指定实测曲线 —— 否则走的是材料卡对数律，不是真实数据。"""
    raw = json.loads((EXAMPLES / EXAMPLE).read_text(encoding="utf-8"))
    assert raw["solver"].get("response_curve") == MEASURED_CURVE


def test_end_to_end_real_curve_drives_solver():
    """**端到端**：实测曲线驱动求解，中心深度必须等于曲线给出的实测值。

    这条是「真实数据 → 真实求解」的总证据：
    * 准入通过（深度来源记在 notes 里）；
    * 求解 completed；
    * 中心去除深度 ≈ 0.35 μm（曲线在 6.92 J/cm² 的实测坑深）。
    """
    cfg, mat, _ = _run_example()
    rep = validate_run(cfg, mat)
    assert rep.ok, [e["message"] for e in rep.errors]
    # 深度来源必须是曲线，而不是材料卡的 response
    assert any("响应曲线" in n and "深度来源" in n for n in rep.notes), (
        f"准入报告须写明深度来源；实际 notes={rep.notes}"
    )

    res = solve(cfg, mat, curves_dir=str(CURVES))
    assert res.status == "completed"
    drop = float(res.surface.initial_height[80, 80] - res.surface.height[80, 80])
    # 容差 0.1%：中心能流不必**精确**等于 6.92 J/cm²
    # （脉冲能量由 F=2E/(πw²) 反算，取的是近似值），实测偏差约 0.002%。
    assert drop == pytest.approx(0.35e-6, rel=1e-3), (
        f"中心深度 {drop*1e6:.5f} μm 应等于实测曲线在 6.92 J/cm² 的 0.35 μm"
    )


def test_without_response_curve_sic_card_refuses():
    """**不指定曲线**时，SiC 卡必须拒绝出深度（它没有 δ）。

    这条看起来像"失败"，其实是**正确的**：SiC 材料卡的深度**只**来自实测曲线，
    去掉曲线它确实没有深度来源 —— 拒绝比硬给一个数诚实。
    """
    raw = json.loads((EXAMPLES / EXAMPLE).read_text(encoding="utf-8"))
    raw["solver"] = {**raw["solver"], "response_curve": None}
    cfg = RunConfig.from_dict(raw, base_dir=str(ROOT))
    mat = load_material_card(SIC_CARD)
    # `solve()` 在准入失败时**返回 status="failed"**（既有设计，不抛错）。
    # 唯一不可接受的是「没曲线却出了深度」。
    res = solve(cfg, mat, curves_dir=str(CURVES))
    assert res.status == "failed", (
        f"没指定曲线时 SiC 卡不该出深度；实际 status={res.status}"
    )
    assert res.surface is None, "失败时不应有表面结果"
    # 准入报告须给出**能力类**原因（而不是含混的通用错误）
    codes = {e.get("code") for e in validate_run(cfg, mat).errors}
    assert codes & {"MATERIAL_CAPABILITY_MISSING", "RESPONSE_SEMANTICS_INVALID"}, (
        f"应是能力/语义类错误；实际 {codes}"
    )


def test_without_response_curve_other_card_keeps_log_law():
    """**不指定曲线**时，有 δ 的卡仍走原对数律 —— 本次改动零影响。"""
    raw = json.loads((EXAMPLES / "ten_pulses.json").read_text(encoding="utf-8"))
    cfg = RunConfig.from_dict(raw, base_dir=str(ROOT))
    mat = load_material_card(ROOT / raw["material_card_file"])
    law = build_pulse_law(mat, unit=cfg.unit, curve=None)
    assert type(law).__name__ == "FixedThresholdLogLaw", (
        "不指定曲线时应维持材料卡对数律（改动前行为）"
    )


def test_missing_curve_file_is_explicit_error():
    """指定的曲线卡不存在时必须**明确报错**，不得静默退回对数律。"""
    cfg, mat, _ = _run_example()
    cfg_bad = RunConfig.from_dict(
        {**json.loads((EXAMPLES / EXAMPLE).read_text(encoding="utf-8")),
         "solver": {**json.loads((EXAMPLES / EXAMPLE).read_text(encoding="utf-8"))["solver"],
                    "response_curve": "no_such_curve.curve.json"}},
        base_dir=str(ROOT),
    )
    with pytest.raises(UFDemoError) as ei:
        solve(cfg_bad, mat, curves_dir=str(CURVES))
    assert "找不到响应曲线卡" in str(ei.value)


def test_non_increment_curve_cannot_drive_solver():
    """非增量语义的曲线**不得**驱动求解（既有闸门必须拦住）。"""
    vol = U.load_curve_card(CURVES, "synthetic_volume_per_energy.curve.json")
    with pytest.raises(UFDemoError) as ei:
        build_pulse_law(None, curve=vol)
    assert ei.value.code == "RESPONSE_SEMANTICS_INVALID"


# ---------------------------------------------------------------------------
# 4. 界面路径：submit 必须把 curves_dir 传下去
# ---------------------------------------------------------------------------


def test_submit_accepts_and_forwards_curves_dir():
    """`ui_service.submit` 必须接受 `curves_dir` 并传给 solver。

    回归背景：它此前调 `solve(cfg, material)`，**不传 curves_dir** ——
    界面上即便选了曲线也加载不到，「真实数据驱动求解」在 UI 里永远走不通。
    """
    import inspect

    sig = inspect.signature(U.submit)
    assert "curves_dir" in sig.parameters, "submit 必须接受 curves_dir"

    src = inspect.getsource(U.submit)
    assert "curves_dir=" in src, "submit 必须把 curves_dir 传给 solve"


def test_solve_payload_accepts_curves_dir():
    """契约层的 `solve_payload` 同样要能透传曲线目录。"""
    import inspect

    from ufdemo import webcontract as W

    assert "curves_dir" in inspect.signature(W.solve_payload).parameters
