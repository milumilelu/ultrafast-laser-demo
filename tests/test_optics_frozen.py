"""光学必须按**名义值冻结**（ADR-0020，用户 2026-09-15 明确"这个一定要改"）。

**回归背景**：`shared_background_patch(threshold_J_m2=…)` 曾经按「声明的实测单线宽度」
**反推等效光斑**并替换 w0、连带改 zR。由于反推方程里带着 `E_p = P_物镜后/f`，
这让光学随**频率与假定的 F_th** 漂移：

| f (kHz) | F_th=78496 → w0 (µm) | 连带 zR (µm) |
|---|---|---|
| 2 | 1.1334 | 3.2649 |
| 20 | 1.3254 | 4.4650 |
| 40 | 1.4085 | 5.0424 |
| 200 | 1.7026 | 7.3678 |
| **名义** | **0.8743** | **1.9429** |

`w0 = M²λ/(πNA)` 只由 λ/NA/M² 决定 —— 频率、能量、阈值都不该动它。
而且 5 μm 是**加工出来的线宽**（多脉冲扫描的结果），不是单脉冲高斯足迹的直径；
把它反解成 w0 等于把频率依赖计入两次（`F0 = 2E/(πw²)` 里本来就带 E），
并把 δ/F_th 的误差吸收进光学自由度。

所以现在：光学**冻结**；声明的单线宽度降级为**对照量**
（模型首击宽度 vs 声明宽度的比值如实报告）。
"""

from __future__ import annotations

import json

import pytest



from ufdemo.calibration import ExperimentRow, PredictionSpec, build_row_config
from ufdemo.config import load_shared_background, shared_background_patch

FIXTURE = "tests/fixtures/analytic_fixture.json"
_OV = {"kind": "log_fixed", "output_semantics": "event_depth_increment",
       "fluence_basis": "incident_peak_fluence", "depth_direction": "surface_normal"}


def _cfg(f_khz: float, threshold: float | None):
    bg = load_shared_background()
    row = ExperimentRow(sample_id="opt", pulse_duration_fs=223.0,
                        repetition_rate_kHz=float(f_khz), scan_speed_mm_s=50.0,
                        hatch_spacing_um=4.0, pass_count=1, mean_depth_um=1.0)
    spec = PredictionSpec(
        material_card_file=FIXTURE, window_um=20.0, dx_um=0.5,
        response_override=({**_OV, "threshold_J_m2": float(threshold),
                            "delta_m": 3.6525e-6} if threshold else None))
    return build_row_config(row, spec=spec, bg=bg)


def test_optics_do_not_depend_on_repetition_rate():
    """同一个阈值下，任何频率的光学**必须相同**（名义值）。"""
    bg = load_shared_background()
    w_nom, zr_nom = bg.derived_waist_m(), bg.derived_rayleigh_m()
    for f_khz in (2.0, 10.0, 20.0, 33.3, 40.0, 200.0):
        cfg = _cfg(f_khz, 78496.3)
        assert cfg.laser.spot_radius_m == pytest.approx(w_nom, rel=1e-12), f"f={f_khz}"
        assert cfg.laser.rayleigh_range_m == pytest.approx(zr_nom, rel=1e-12), f"f={f_khz}"


def test_optics_do_not_depend_on_the_assumed_threshold():
    """同一路径下，阈值写在卡里还是通过 override 传、取哪个值，光学都**一样**。"""
    bg = load_shared_background()
    w_nom, zr_nom = bg.derived_waist_m(), bg.derived_rayleigh_m()
    for thr in (None, 78496.3, 12890.0, 5.0e5):
        cfg = _cfg(20.0, thr)
        assert cfg.laser.spot_radius_m == pytest.approx(w_nom, rel=1e-12), f"thr={thr}"
        assert cfg.laser.rayleigh_range_m == pytest.approx(zr_nom, rel=1e-12), f"thr={thr}"


def test_basis_reports_the_width_comparison_honestly():
    """对照量必须如实给出：名义光学下的首击宽度**小于**声明的 5 μm，比值 > 1。"""
    bg = load_shared_background()
    p = shared_background_patch(bg, repetition_rate_Hz=20e3, threshold_J_m2=7.85e4)
    b = p["laser"]["_spot_radius_basis"]
    assert b["source"] == "frozen_nominal_optics"
    assert b["declared_line_width_um"] == pytest.approx(5.0)
    assert b["model_first_shot_width_um"] < 5.0
    assert b["declared_over_model_width"] == pytest.approx(
        5.0 / b["model_first_shot_width_um"], rel=1e-9)
    # 名义光学下模型给不出 5 μm —— 这条差距必须**看得见**，而不是靠改光斑抹掉
    assert "冻结" in b["note"]


def test_basis_has_no_threshold_when_none_given():
    bg = load_shared_background()
    p = shared_background_patch(bg, repetition_rate_Hz=20e3)
    b = p["laser"]["_spot_radius_basis"]
    assert b["source"] == "frozen_nominal_optics"
    assert b["declared_line_width_um"] == pytest.approx(5.0)   # 声明仍在
    assert "threshold_j_m2" not in b                          # 没阈值就没有对照量
    assert p["laser"]["rayleigh_range_m"] == pytest.approx(bg.derived_rayleigh_m(), rel=1e-12)


def test_equivalent_spot_helper_is_no_longer_used_for_assembly():
    """`equivalent_spot_radius_m` 保留为**诊断工具**，但不得再出现在装配路径里。"""
    import inspect
    from ufdemo import config as C

    src = inspect.getsource(C.shared_background_patch)
    assert "equivalent_spot_radius_m" not in src, (
        "shared_background_patch 不得再用声明宽度反推光斑（ADR-0020）"
    )
    # 助手本身仍可用，且语义自洽
    bg = load_shared_background()
    w, info = bg.equivalent_spot_radius_m(pulse_energy_J=bg.pulse_energy_J(20e3),
                                          threshold_J_m2=7.85e4)
    assert info["declared"] is True
    assert info["source"] == "derived_from_declared_line_width"
