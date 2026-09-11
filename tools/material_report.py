"""七材料能力入口表 + G09 受限阈值协议报告生成器（批次 H / T14）。

批次 H 必交产物，本脚本一次生成：

1. **七材料能力入口表** —— ``docs/reports/material_capability_table.csv``：
   每个材料族的「开放 / 红线 / 缺口」逐条列出，并附**实跑**核验结论
   （``verified`` 为真表示探针跑通，不是文档声称）。
2. **G09 检查明细** —— ``docs/reports/g09_threshold_protocol.csv``：受限阈值协议的
   硬约束逐条实跑（配置层拦截累计/平均基准、多候选须显式索引、多脉冲定点口径被拦、
   只按本事件能流判超阈的可判别构造、禁用时不造假数组……）以及七材料入口探针汇总。
3. **G09 报告** —— ``docs/reports/g09_threshold_protocol.md``。

本脚本与 ``tests/test_threshold_protocol.py``、``tests/test_material_entries.py``
覆盖同一组不变量，但**独立执行**（不依赖 pytest），以便验收报告自足。

用法::

    python tools/material_report.py
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from ufdemo.config import RunConfig, ThresholdProtocolConfig  # noqa: E402
from ufdemo.errors import CONFIG_INVALID, UFDemoError  # noqa: E402
from ufdemo.materials import load_material_card, load_material_catalog  # noqa: E402
from ufdemo.materials import material_entry_rows, verify_entry_enforcements  # noqa: E402
from ufdemo.solver import solve  # noqa: E402
from ufdemo.surface import SurfaceState  # noqa: E402
from ufdemo.thresholds import (  # noqa: E402
    THRESHOLD_FLUENCE_BASES,
    build_threshold_protocol,
    classify_exceedance,
)

D = ROOT / "docs" / "reports"
MATERIALS = ROOT / "data" / "materials"

SIC = "sic_4h_cface_1035nm_multishot.json"
SIC_ID = "sic_4h_cface_1035nm_multishot"
INCONEL_N10 = "inconel718_1030nm_n10.json"
CFRP = "cfrp_t700_yb01_800nm.json"
GLASS = "glass_ceramic_unbranded_1030nm.json"

SIC_MODIFICATION_J_M2 = 23500.0
SIC_STRUCTURAL_J_M2 = 49700.0


# ---------------------------------------------------------------------------
# 配置桩（与 tests/test_threshold_protocol.py 同源口径）
# ---------------------------------------------------------------------------


def _sic_threshold_raw(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "schema_version": "1.0",
        "case_id": "g09_sic_threshold_only",
        "label": "g09_sic_threshold",
        "run_mode": "threshold_only",
        "unit_system": "SI",
        "material_id": SIC_ID,
        "material_card_file": f"data/materials/{SIC}",
        "seed": 1,
        "grid": {
            "nx": 21, "ny": 21, "dx_m": 2e-6, "dy_m": 2e-6,
            "center_x_m": 0.0, "center_y_m": 0.0, "origin": "cell_center",
            "initial_surface": "flat", "initial_height_m": 0.0,
        },
        "laser": {
            "wavelength_m": 1.035e-6, "pulse_duration_s": 3e-13, "pulse_energy_J": 1e-5,
            "repetition_rate_Hz": 200000.0, "spot_radius_m": 1e-5,
            "focus_xyz_m": [0.0, 0.0, 0.0], "direction_unit": [0.0, 0.0, 1.0],
            "power_measurement_location": "sample_surface",
            "parameter_sources": ["synthetic_definition"],
        },
        "path": {
            "t0_s": 0.0, "time_tolerance_s": 1e-12,
            "segments": [{
                "segment_id": 0, "pass_id": 0, "start_s": 0.0, "end_s": 2.5e-5,
                "start_xyz_m": [0.0, 0.0, 0.0], "end_xyz_m": [0.0, 0.0, 0.0],
                "speed_m_s": 0.0, "laser_on": True, "label": "point",
            }],
        },
        "solver": {
            "mode": "reference", "geometry_feedback": "fixed_geometry",
            "history_enabled": False, "tail_epsilon": 1e-8,
            "memory_budget_bytes": 1073741824, "budget_safety_factor": 1.5,
            "cancel_check_interval": 256, "acceleration": "off",
            "multiline_incubation": False, "structured_interface": False,
        },
        "output": {
            "snapshot_policy": "events", "snapshot_events": [0],
            "max_snapshots": 4, "max_snapshot_bytes": 134217728,
        },
    }
    for dotted, value in overrides.items():
        target = raw
        parts = dotted.split(".")
        for p in parts[:-1]:
            target = target[p]
        target[parts[-1]] = value
    return raw


def _make_config(raw: dict[str, Any]) -> RunConfig:
    return RunConfig.from_dict(raw, base_dir=str(ROOT))


def _card(name: str):
    return load_material_card(MATERIALS / name)


# ---------------------------------------------------------------------------
# G09 检查（独立于 pytest）
# ---------------------------------------------------------------------------


def _row(check: str, expected: Any, measured: Any, status: str, detail: str) -> dict[str, Any]:
    return {"check": check, "expected": expected, "measured": measured,
            "status": status, "detail": detail}


def _check(check: str, expected: Any, fn) -> dict[str, Any]:
    try:
        measured, detail = fn()
        return _row(check, expected, measured, "通过", detail)
    except Exception as exc:  # noqa: BLE001
        return _row(check, expected, f"异常：{type(exc).__name__}: {exc}", "失败", "断言未通过")


def _expect_error(code: str, fn) -> tuple[str, str]:
    try:
        fn()
    except UFDemoError as err:
        return err.code, err.message
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, repr(exc)
    return "（未拒绝）", "该调用本应被拒绝"


def _check_config_rejects_cumulative() -> tuple[str, str]:
    bad = []
    for basis in ("cumulative_fluence", "cumulative_dose", "mean_fluence", "total_fluence"):
        code, _ = _expect_error(CONFIG_INVALID, lambda b=basis: ThresholdProtocolConfig.from_dict(
            {"enabled": True, "fluence_basis": b}))
        if code != CONFIG_INVALID:
            bad.append(f"{basis}→{code}")
    assert not bad, f"未被拦截：{bad}"
    # 通过整份 RunConfig 也必须被拦
    raw = _sic_threshold_raw()
    raw["threshold_protocol"] = {"enabled": True, "fluence_basis": "cumulative_fluence"}
    code, _ = _expect_error(CONFIG_INVALID, lambda: RunConfig.from_dict(raw))
    assert code == CONFIG_INVALID
    return (
        "4 个累计/平均基准 + 整份 RunConfig 均 CONFIG_INVALID",
        "非法能流基准在配置层被拒，不靠界面禁用",
    )


def _check_only_event_basis() -> tuple[str, str]:
    assert THRESHOLD_FLUENCE_BASES == ("per_event_incident",)
    p = build_threshold_protocol(_card(SIC), enabled=False, candidate_index=0)
    assert p.to_dict()["fluence_basis"] == "per_event_incident"
    return f"唯一注册基准={THRESHOLD_FLUENCE_BASES}", "只登记「本事件入射能流」一种基准"


def _check_disabled_not_zeroed() -> tuple[str, str]:
    p = build_threshold_protocol(_card(SIC), enabled=False, candidate_index=0)
    assert p.available is False and "未开启" in p.reason
    assert classify_exceedance(p, np.full((3, 3), 1e9), np.ones((3, 3), dtype=bool)) is None
    assert p.to_dict()["used_for_depth"] is False
    return ("未开启→available=False｜分类返回 None（非全 0 数组）",
            "禁用时如实报不可用，不返回假数组")


def _check_multi_candidate_index() -> tuple[str, str]:
    card = _card(SIC)
    p = build_threshold_protocol(card, enabled=True)
    assert p.available is False and "candidate_index" in p.reason
    assert p.n_candidates == 2
    p0 = build_threshold_protocol(card, enabled=True, candidate_index=0)
    p1 = build_threshold_protocol(card, enabled=True, candidate_index=1)
    assert p0.threshold_internal == SIC_MODIFICATION_J_M2
    assert p1.threshold_internal == SIC_STRUCTURAL_J_M2
    assert p0.observable_name == "single_pulse_modification_threshold"
    assert p1.observable_name == "single_pulse_structural_transformation_threshold"
    return (f"2 个候选：未给索引→不可用｜idx0={p0.threshold_internal:.4g}｜idx1={p1.threshold_internal:.4g}",
            "多候选必须显式 candidate_index，不静默取默认")


def _check_multipulse_blocked() -> tuple[str, str]:
    inconel = build_threshold_protocol(_card(INCONEL_N10), enabled=True)
    assert inconel.available is False, "N=10 定点口径不得当单脉冲阈值"
    assert "多脉冲" in inconel.reason or "N" in inconel.reason
    cfrp = build_threshold_protocol(_card(CFRP), enabled=True)
    assert cfrp.available is True, "CFRP 的 Fth1 是单脉冲端点，不得被误拦"
    return ("高温合金 N=10→不可用｜CFRP Fth1→可用",
            "按口径名机器识别多脉冲：N≥2 定点口径判不可用，N=1 端点放行")


def _check_missing_threshold() -> tuple[str, str]:
    p = build_threshold_protocol(_card(GLASS), enabled=True)
    assert p.available is False
    assert p.threshold_internal is None
    return "微晶玻璃（无阈值）→available=False｜threshold=None", "缺阈值如实报不可用，不补近似值"


def _check_event_fluence_discriminator() -> tuple[str, str]:
    """可判别构造：两发各 0.6 Fth（累计 1.2 Fth）掩膜必须仍为空。"""
    cfg = _make_config(_sic_threshold_raw())
    surface = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False,
                                      threshold_protocol=True)
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
    assert np.all(surface.cumulative_fluence[:5, :5] > thr), "构造前提：累计剂量已超阈"
    assert not surface.threshold_exceeded_mask.any()
    np.testing.assert_array_equal(surface.height, h_before)
    above = np.full((5, 5), 1.2 * thr)
    surface.accumulate_illumination(section, above, mask)
    n = surface.accumulate_threshold(section, classify_exceedance(p, above, mask))
    assert n == 25
    np.testing.assert_array_equal(surface.height, h_before)
    return (f"两发 0.6Fth（累计 1.2Fth）掩膜=空｜单发 1.2Fth 点亮 {n} 单元｜高度逐位不变",
            "只按本事件能流判超阈，且协议不产生去除量")


def _check_solver_toggle() -> tuple[str, str]:
    raw = _sic_threshold_raw()
    raw["threshold_protocol"] = {"enabled": True, "candidate_index": 0}
    res_on = solve(_make_config(raw), _card(SIC))
    thr_on = res_on.diagnostics["threshold"]
    assert thr_on["protocol_enabled"] and thr_on["protocol_available"]
    assert thr_on["fluence_basis"] == "per_event_incident"
    assert thr_on["used_for_depth"] is False
    assert thr_on["n_exceeded_cell_events"] > 0 and thr_on["exceeded_cells_final"] > 0
    assert "threshold_exceeded_mask" in res_on.snapshots[-1]
    assert res_on.statistics["max_depth_internal"] is None

    res_off = solve(_make_config(_sic_threshold_raw()), _card(SIC))
    thr_off = res_off.diagnostics["threshold"]
    assert thr_off["protocol_enabled"] is False
    assert res_off.surface.threshold_exceeded_mask is None
    assert "threshold_exceeded_mask" not in res_off.snapshots[-1]
    assert thr_off["exceeded_cells_final"] == 0
    return (f"启用：超阈单元·事件={thr_on['n_exceeded_cell_events']}｜末态单元={thr_on['exceeded_cells_final']}｜"
            f"禁用：掩膜=None 且快照不写数组",
            "启用时落盘并计数，禁用时不造假数组；threshold_only 深度仍为 null")


def collect_g09_checks() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.append(_check("配置层拒绝累计/平均能流基准", "CONFIG_INVALID", _check_config_rejects_cumulative))
    rows.append(_check("唯一注册基准 = 本事件入射能流", "('per_event_incident',)", _check_only_event_basis))
    rows.append(_check("禁用时报不可用、不返回全 0 假数组", "available=False｜分类=None", _check_disabled_not_zeroed))
    rows.append(_check("多候选须显式 candidate_index", "未给索引→不可用", _check_multi_candidate_index))
    rows.append(_check("多脉冲口径不得当单脉冲阈值", "N=10 拦｜N=1 放行", _check_multipulse_blocked))
    rows.append(_check("缺阈值如实报不可用", "available=False｜threshold=None", _check_missing_threshold))
    rows.append(_check("只按本事件能流判超阈（可判别构造）", "累计超阈但掩膜为空", _check_event_fluence_discriminator))
    rows.append(_check("求解器启用/禁用两态一致", "启用记数落盘｜禁用不造假", _check_solver_toggle))

    # 七材料入口：逐条探针汇总（明细见能力表 CSV）
    catalog = load_material_catalog(MATERIALS)

    def entries():
        verdicts = verify_entry_enforcements(catalog, MATERIALS)
        n_ok = sum(1 for r in verdicts if r["ok"])
        kinds: dict[str, int] = {}
        for r in verdicts:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        assert n_ok == len(verdicts), "存在未通过的入口探针"
        return (
            f"探针 {n_ok}/{len(verdicts)} 成立｜开放 {kinds.get('opened', 0)}｜"
            f"红线 {kinds.get('blocked', 0)}｜缺口 {kinds.get('deferred', 0)}",
            "七材料入口的开放/红线/缺口逐条实跑核验",
        )

    rows.append(_check("七材料能力入口：逐条探针实跑", "全部成立", entries))
    return rows


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def write_markdown(entry_rows: list[dict[str, Any]], checks: list[dict[str, Any]]) -> Path:
    n_pass = sum(1 for r in checks if r["status"] == "通过")
    n_fail = sum(1 for r in checks if r["status"] == "失败")
    kind_zh = {"opened": "开放", "blocked": "红线", "deferred": "缺口"}
    deferred = [r for r in entry_rows if r["kind"] == "deferred"]

    lines = [
        "# G09：七材料能力入口与受限阈值协议报告（批次 H / T14）",
        "",
        f"- 汇总：检查通过 {n_pass}｜失败 {n_fail}",
        "- 机读能力表：`docs/reports/material_capability_table.csv`",
        "- 机读检查明细：`docs/reports/g09_threshold_protocol.csv`",
        "",
        "> 本报告只做**数值实现验证**：软件跑通 ≠ 材料验证。",
        "> 阈值协议只按**本事件入射能流**判超阈，不产生深度；",
        "> 合成模式内部无量纲，导出不含物理 `depth_um`。",
        "",
        "## 1. 七材料能力入口表",
        "",
        "| 材料族 | 类别 | 条目 | 依据 / 生效位置 | 错误码 / 缺口 | 已核验 |",
        "|---|---|---|---|---|---|",
    ]
    for r in entry_rows:
        lines.append(
            f"| {r['family']} | {kind_zh.get(r['kind'], r['kind'])} | {r['item']} | "
            f"{r['binding']} | {r['code']} | {'是' if r['verified'] is True else '否'} |"
        )

    lines += [
        "",
        f"### 缺口（{len(deferred)}）",
        "",
    ]
    if deferred:
        for r in deferred:
            lines.append(f"- **{r['family']}／{r['item']}**：{r['detail']}")
            lines.append(f"  - 原因：{r['binding']}")
    else:
        lines.append("- 无。")

    lines += [
        "",
        "## 2. G09 检查明细",
        "",
        "| 检查 | 期望 | 实测 | 状态 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for r in checks:
        lines.append(f"| {r['check']} | {r['expected']} | {r['measured']} | {r['status']} | {r['detail']} |")

    lines += [
        "",
        "## 3. 协议硬约束与本实现的对应",
        "",
        "| 约束（细则 11.3 / 任务书 6.3） | 实现位置 | 证据 |",
        "|---|---|---|",
        "| 只对本事件入射能流判超阈，绝不用累计剂量 | `thresholds.classify_exceedance` | 两发 0.6Fth 累计 1.2Fth 掩膜仍为空 |",
        "| 阈值口径必须是单脉冲口径；多脉冲定点（N≥2）判不可用 | `thresholds._declare_multipulse` | 高温合金 N=10 拦；CFRP Fth1 放行 |",
        "| 多候选必须显式 `candidate_index` | `thresholds._resolve_threshold_protocol` | SiC 未给索引→不可用 |",
        "| 不产生深度（`used_for_depth` 恒 False） | `thresholds.assert_not_removal` | 高度场逐位不变 |",
        "| 协议未开启/材料无阈值时如实报不可用，不返回假数组 | `build_threshold_protocol` | 掩膜为 `None`，分类返回 `None` |",
        "| 落盘与回放同源 | `surface.to_snapshot` / `io` / `ui_service` | 快照写入掩膜与计数；回放走同一诊断构造 |",
        "",
        "## 4. 边界声明",
        "",
        "- 「超阈/改性标记」是**分类标记**，不是去除量，也不是温度/热影响结果；",
        "  界面图层标签已按措辞守卫（细则 11.3）严格限定为「该图实际是什么量」。",
        "- 受限阈值协议**不接逐事件主循环**的深度计算：它只做观测量累计。",
        "- 金刚石族的「合成形貌」在细则第 7 节要求开放，但当前结构构建器不支持其",
        "  `structure_type`，故如实记为**缺口**并附探针证明（见上表），留待 M2 放行前决策。",
        "",
        "## 5. 复现命令",
        "",
        "```bash",
        "python tools/material_report.py",
        "python -m pytest -q -m g09",
        "python tools/ui_probe.py",
        "```",
        "",
    ]
    out = D / "g09_threshold_protocol.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "runs" / "material_report"))
    _ = ap.parse_args()

    D.mkdir(parents=True, exist_ok=True)
    catalog = load_material_catalog(MATERIALS)

    entry_rows = material_entry_rows(catalog, MATERIALS)
    checks = collect_g09_checks()

    cap_csv = D / "material_capability_table.csv"
    with open(cap_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(entry_rows[0].keys()))
        w.writeheader()
        w.writerows(entry_rows)

    chk_csv = D / "g09_threshold_protocol.csv"
    with open(chk_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(checks[0].keys()))
        w.writeheader()
        w.writerows(checks)

    md_path = write_markdown(entry_rows, checks)

    n_fail = sum(1 for r in checks if r["status"] == "失败")
    n_bad_entry = sum(1 for r in entry_rows if r["verified"] is not True)
    print(f"报告：{md_path}")
    print(f"报告：{cap_csv}")
    print(f"报告：{chk_csv}")
    print(f"能力入口 {len(entry_rows)} 条（未核验 {n_bad_entry}）｜检查 {len(checks)} 项（失败 {n_fail}）")
    return 0 if (n_fail == 0 and n_bad_entry == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
