"""合成结构报告生成器（批次 G / T11–T13，G06）。

批次 G 必交产物，本脚本一次生成：

1. **两个合成结构实例的统计** —— ``docs/reports/structure_instances.csv``：
   每个示例的结构类型、种子、算法版本、相身份与内联响应、目标/实际体积分数及
   计算方法、铺层/颗粒几何、初末相单元计数、跨相截断诊断；
2. **G06 检查明细** —— ``docs/reports/g06_phase_interfaces.csv``：逐条列出
   同相合并等价、不同相不跳过、不拆伪脉冲、固定种子重现，以及三条红线
   （旧 δ 字段、整体阈值拆分、结构×动态角度互斥）的期望/实测/状态；
3. **G06 报告** —— ``docs/reports/g06_phase_interfaces.md``。

本脚本与 ``tests/test_phase_interfaces.py`` 覆盖同一组不变量，但**独立执行**
（不依赖 pytest），以便验收报告自足。

用法::

    python tools/structure_report.py
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from ufdemo.beam import BeamOptions, beam_patch  # noqa: E402
from ufdemo.config import RunConfig, load_config, validate_run  # noqa: E402
from ufdemo.errors import (  # noqa: E402
    CONDITION_MISMATCH,
    CONFIG_INVALID,
    MATERIAL_CAPABILITY_MISSING,
    RESPONSE_SEMANTICS_INVALID,
    UFDemoError,
)
from ufdemo.materials import load_material_card  # noqa: E402
from ufdemo.paths import iter_events  # noqa: E402
from ufdemo.response import HistoryState  # noqa: E402
from ufdemo.solver import RunResult, solve  # noqa: E402
from ufdemo.structure import (  # noqa: E402
    LayeredStructure,
    ParticleStructure,
    PhaseSpec,
    PlyLayer,
    assert_phase_threshold_not_split,
    build_phase,
    load_structure,
)
from ufdemo.surface import SurfaceState  # noqa: E402

D = ROOT / "docs" / "reports"
EXAMPLES = ROOT / "examples"

# 批次 G 的两个交付示例
STRUCTURE_CASES: tuple[tuple[str, str, str], ...] = (
    ("alsic_particle_composite.json", "g06_alsic_particles", "颗粒增强铝基（合成）"),
    ("cfrp_laminated_ply.json", "g06_cfrp_plies", "CFRP 铺层 [0/90/0/90]（合成）"),
)

CFRP_OVERALL_THRESHOLD_F_REF = 8400.0 / 1.0e4  # 卡上 Fth1 = 0.84 F_ref

# 期望的错误码（断言，不是描述）
EXPECTED_REDLINE_CODES: dict[str, str] = {
    "legacy_delta_over_delta_ref": RESPONSE_SEMANTICS_INVALID,
    "phase_borrows_material_card": MATERIAL_CAPABILITY_MISSING,
    "forbidden_response_source": MATERIAL_CAPABILITY_MISSING,
    "overall_threshold_split": MATERIAL_CAPABILITY_MISSING,
    "structured_x_dynamic_angle": CONFIG_INVALID,
    "structure_type_mismatch": CONDITION_MISMATCH,
}


# ---------------------------------------------------------------------------
# 求解与统计
# ---------------------------------------------------------------------------


def load_case(name: str) -> tuple[Any, Any]:
    path = EXAMPLES / name
    cfg = load_config(str(path))
    card = load_material_card(ROOT / cfg.material_card_file)
    rep = validate_run(cfg, card)
    if not rep.ok:
        raise SystemExit(f"{name} 准入失败：{rep.to_dict()['errors']}")
    return cfg, card


def solve_case(name: str) -> tuple[Any, Any, RunResult]:
    cfg, card = load_case(name)
    return cfg, card, solve(cfg, card)


def collect_structure_rows(results: Mapping[str, RunResult] | None = None) -> list[dict[str, Any]]:
    """每个结构示例一行。``results`` 可复用验收脚本已算好的结果，避免重复求解。"""
    rows: list[dict[str, Any]] = []
    for name, _label, zh in STRUCTURE_CASES:
        if results is not None and name in results:
            cfg, card = load_case(name)
            res = results[name]
        else:
            cfg, card, res = solve_case(name)
        sm = dict(res.metadata.get("structure") or {})
        vf = dict(sm.get("volume_fraction") or {})
        phase = dict((res.diagnostics or {}).get("phase") or {})
        removal = dict((res.diagnostics or {}).get("removal") or {})
        geom = {k: sm.get(k) for k in ("n_particles_target", "n_particles_placed",
                                        "placement_attempts", "mean_diameter", "mean_radius",
                                        "depth", "n_layers_input", "n_layers_merged",
                                        "total_thickness", "same_phase_merge")}
        rows.append(
            {
                "case": name,
                "说明": zh,
                "structure_type": sm.get("structure_type"),
                "material_id": cfg.material_id,
                "algorithm_version": sm.get("algorithm_version"),
                "seed": sm.get("seed"),
                "n_phases": sm.get("n_phases"),
                "phase_names": ",".join(p.get("name", "") for p in (sm.get("phases") or [])),
                "phase_threshold_over_F_ref": ",".join(
                    f"{p.get('name')}={p.get('threshold_internal')}" for p in (sm.get("phases") or [])
                ),
                "phase_delta_over_L_ref": ",".join(
                    f"{p.get('name')}={p.get('delta_internal')}" for p in (sm.get("phases") or [])
                ),
                "target_volume_fraction": sm.get("target_volume_fraction"),
                "actual_volume_fraction": json.dumps(vf.get("actual_volume_fraction"), ensure_ascii=False),
                "volume_fraction_method": vf.get("volume_fraction_method"),
                "n_volume_samples": vf.get("n_samples"),
                **{k: geom[k] for k in geom},
                "initial_phase_cells": json.dumps(phase.get("initial_phase_cell_counts"), ensure_ascii=False),
                "final_phase_cells": json.dumps(phase.get("final_phase_cell_counts"), ensure_ascii=False),
                "n_events": (res.diagnostics.get("events") or {}).get("n_events"),
                "clipped_events": removal.get("clipped_events"),
                "n_clipped_cells": removal.get("n_clipped_cells"),
                "phase_switch_cells": removal.get("phase_switch_cells"),
                "phase_switch_events": removal.get("phase_switch_events"),
                "unapplied_fraction_of_candidate": removal.get("unapplied_fraction_of_candidate"),
                "center_depth_internal": res.statistics.get("center_depth_internal"),
                "depth_unit": res.statistics.get("depth_unit"),
                "status": res.status,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# G06 检查（独立于 pytest）
# ---------------------------------------------------------------------------


def _row(case: str, kind: str, expected: Any, measured: Any, status: str, detail: str) -> dict[str, Any]:
    return {
        "case": case,
        "kind": kind,
        "expected": expected,
        "measured": measured,
        "status": status,
        "detail": detail,
    }


def _check(case: str, kind: str, expected: Any, fn) -> dict[str, Any]:
    try:
        measured, detail = fn()
        return _row(case, kind, expected, measured, "通过", detail)
    except Exception as exc:  # noqa: BLE001
        return _row(case, kind, expected, f"异常：{type(exc).__name__}: {exc}", "失败", "断言未通过")


def _phase(name: str, pid: int, thr: float, delta: float, role: str = "layer") -> PhaseSpec:
    return PhaseSpec(name=name, phase_id=pid, role=role, threshold_internal=thr, delta_internal=delta)


def _expect_error(code: str, fn) -> tuple[str, str]:
    """确认调用确实抛出**指定**错误码，返回 (实测码, 说明)。"""
    try:
        fn()
    except UFDemoError as err:
        return err.code, err.message
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, repr(exc)
    return "（未拒绝）", "该调用本应被拒绝"


def _check_same_phase_merge() -> tuple[str, str]:
    phases = [_phase("A", 1, 1.0, 0.5), _phase("B", 2, 2.0, 0.2)]
    st = LayeredStructure(
        [PlyLayer(thickness=1.0, matrix_phase="A"),
         PlyLayer(thickness=2.0, matrix_phase="A"),
         PlyLayer(thickness=1.5, matrix_phase="B")],
        phases, z_top=0.0, seed=7,
    )
    ref = LayeredStructure(
        [PlyLayer(thickness=3.0, matrix_phase="A"), PlyLayer(thickness=1.5, matrix_phase="B")],
        phases, z_top=0.0, seed=7,
    )
    zs = np.array([0.0, -0.5, -1.0, -2.9, -3.1, -4.4])
    xs = np.zeros_like(zs)
    same_phase = np.array_equal(st.phase_at(xs, xs, zs), ref.phase_at(xs, xs, zs))
    same_iface = np.allclose(
        st.next_different_interface(xs, xs, zs), ref.next_different_interface(xs, xs, zs)
    )
    assert st.merged_layer_count == 2 and st.original_layer_count == 3
    assert same_phase and same_iface
    return (
        f"合并 {st.original_layer_count}→{st.merged_layer_count} 层，几何逐位一致",
        "同相相邻铺层合并后 phase_at / next_different_interface 与预合并单层等价",
    )


def _check_same_phase_seam() -> tuple[str, str]:
    phases = [_phase("A", 1, 1.0, 0.5), _phase("B", 2, 2.0, 0.2)]
    st = LayeredStructure(
        [PlyLayer(thickness=1.0, matrix_phase="A", fiber_phase="B",
                  fiber_angle_deg=0.0, fiber_volume_fraction=0.5, fiber_width=0.4),
         PlyLayer(thickness=1.0, matrix_phase="A", fiber_phase="B",
                  fiber_angle_deg=90.0, fiber_volume_fraction=0.5, fiber_width=0.4)],
        phases, z_top=0.0, seed=3,
    )
    assert st.merged_layer_count == 2  # 铺层角不同 → 不合并
    inf_at_same = st.next_different_interface(0.5, 0.0, 0.0)
    real_iface = st.next_different_interface(0.5, 0.5, 0.0)
    assert inf_at_same == float("inf")
    assert abs(real_iface - 1.0) < 1e-9
    # 不跳过：界面之上仍是当前相
    for z in np.linspace(0.0, -1.0 + 1e-6, 33):
        assert st.phase_at(0.5, 0.5, z) == st.phase_id_of("A")
    return (
        f"同相列 inf；异相列 {real_iface:.6g}（= 层厚）",
        "同相层界不假截断，真实相界面距离解析给出",
    )


def _check_particle_iface() -> tuple[str, str]:
    phases = [_phase("matrix", 1, 1.0, 0.5, role="matrix"), _phase("part", 2, 2.0, 0.2, role="particle")]
    st = ParticleStructure(
        matrix_phase="matrix", particle_phase="part", phases=phases,
        radius=0.5, z_top=0.0, x_range=(-2.0, 2.0), y_range=(-2.0, 2.0), depth=4.0,
        seed=123, target_volume_fraction=0.05,
    )
    assert st.n_particles > 0
    cx, cy, cz, r = float(st._cx[0]), float(st._cy[0]), float(st._cz[0]), float(st._r[0])
    enter = st.next_different_interface(cx, cy, cz + r + 0.3)
    leave = st.next_different_interface(cx, cy, cz)
    assert abs(enter - 0.3) < 1e-9 and abs(leave - r) < 1e-9
    assert st.phase_at(cx, cy, cz + r + 1e-6) == st.phase_id_of("matrix")
    assert st.phase_at(cx, cy, cz) == st.phase_id_of("part")
    assert st.phase_at(cx, cy, cz - r - 1e-6) == st.phase_id_of("matrix")
    assert st.next_different_interface(1.99, 1.99, 0.0) == float("inf")
    return (
        f"进入 {enter:.6g}｜离开 {leave:.6g}｜无颗粒列 inf",
        "解析射线-球求交：进入/离开界面均命中，不越过",
    )


def _one_event_cfrp():
    raw = json.loads((EXAMPLES / "cfrp_laminated_ply.json").read_text(encoding="utf-8"))
    raw["path"]["segments"][0]["end_s"] = 5.0e-4
    cfg = RunConfig.from_dict(raw, base_dir=str(EXAMPLES))
    card = load_material_card(ROOT / cfg.material_card_file)
    assert validate_run(cfg, card).ok
    return cfg, card, load_structure(cfg, card)


def _check_no_pseudo_pulse() -> tuple[str, str]:
    cfg, card, structure = _one_event_cfrp()
    res = solve(cfg, card)
    assert res.ok, res.errors
    assert (res.diagnostics.get("events") or {}).get("n_events") == 1
    laws = structure.laws(unit_mode=cfg.unit.mode)
    fid = structure.phase_id_of("fiber")
    surface0 = SurfaceState.initialize(cfg.grid, cfg.laser, history_enabled=False)
    surface0.initialize_phases(structure)
    event = next(iter(iter_events(cfg.path, cfg.laser)))
    patch = beam_patch(event, surface0, BeamOptions())
    c = cfg.grid.ny // 2
    F = float(patch.fluence[c - patch.iy0, cfg.grid.nx // 2 - patch.ix0])
    expected = float(laws[fid].increment(np.array([[F]]), HistoryState(exposure_count=None), card).values[0, 0])
    applied = float(res.surface.initial_height[c, cfg.grid.nx // 2] - res.surface.height[c, cfg.grid.nx // 2])
    assert abs(applied - expected) <= 1e-12 * max(abs(expected), 1e-30)
    return (
        f"中心列一次应用 {applied:.10g} = 单一纤维核 {expected:.10g}",
        "一个真实脉冲只调用当前暴露相的一个核，未拆成两相叠加",
    )


def _check_seed_reproducible() -> tuple[str, str]:
    phases = [_phase("matrix", 1, 1.0, 0.5, role="matrix"), _phase("part", 2, 2.0, 0.2, role="particle")]
    kw = dict(
        matrix_phase="matrix", particle_phase="part", phases=phases, radius=0.5,
        z_top=0.0, x_range=(-2.0, 2.0), y_range=(-2.0, 2.0), depth=4.0,
        target_volume_fraction=0.06,
    )
    a, b, c = ParticleStructure(seed=2026, **kw), ParticleStructure(seed=2026, **kw), ParticleStructure(seed=2027, **kw)
    assert np.array_equal(a._cx, b._cx) and np.array_equal(a._cz, b._cz)
    assert a.n_particles == b.n_particles
    differing = a.n_particles != c.n_particles or bool(np.any(c._cx != a._cx))
    assert differing
    return (
        f"同种子 {a.n_particles} 个颗粒逐位一致；异种子几何不同",
        "固定种子可复现，且换种子确有影响",
    )


def _check_volume_fraction_separate(results: Mapping[str, RunResult]) -> tuple[str, str]:
    sm = dict((results["alsic_particle_composite.json"].metadata or {}).get("structure") or {})
    vf = dict(sm.get("volume_fraction") or {})
    assert sm.get("target_volume_fraction") == 0.32
    assert vf.get("target_volume_fraction") == 0.32
    assert vf.get("actual_volume_fraction") is not None
    assert vf.get("volume_fraction_method") == "grid_sample"
    assert vf.get("n_samples", 0) > 0
    assert "nominal_volume_fraction" in vf
    return (
        f"目标 0.32｜实际 {json.dumps(vf['actual_volume_fraction'], ensure_ascii=False)}"
        f"（{vf['volume_fraction_method']}, n={vf['n_samples']}）",
        "目标与实际体积分数分别报告，不强制相等",
    )


def _check_truncation_naming(results: Mapping[str, RunResult]) -> tuple[str, str]:
    res = results["cfrp_laminated_ply.json"]
    rem = dict((res.diagnostics or {}).get("removal") or {})
    assert "unapplied_candidate_removal_volume_internal" in rem
    keys = " ".join(rem.keys())
    assert "residual" not in keys and "energy" not in keys
    for k in ("clipped_events", "n_clipped_cells", "phase_switch_cells", "phase_switch_events"):
        assert k in rem
    note = ((res.metadata or {}).get("structure") or {}).get("truncation_note", "")
    assert "未应用候选去除体积" in note
    assert "下一个真实脉冲" in " ".join(res.warnings)
    return (
        f"截断事件 {rem['clipped_events']}｜相切换事件 {rem['phase_switch_events']}｜"
        f"未应用占比 {rem['unapplied_fraction_of_candidate']:.4g}",
        "截断量命名为「未应用候选去除体积」，且不以剩余能量表述",
    )


def collect_g06_checks(results: Mapping[str, RunResult]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    rows.append(_check("G06", "同相合并等价", "合并前后几何逐位一致", _check_same_phase_merge))
    rows.append(_check("G06", "同相层界不假截断/不同相不跳过", "同相列 inf；异相列 = 层厚", _check_same_phase_seam))
    rows.append(_check("G06", "颗粒界面不跳过", "进入/离开距离解析命中", _check_particle_iface))
    rows.append(_check("G06", "不拆伪脉冲", "单次应用 = 单一相核候选量", _check_no_pseudo_pulse))
    rows.append(_check("G06", "固定种子重现", "同种子逐位一致，异种子不同", _check_seed_reproducible))
    rows.append(_check("G06", "目标/实际体积分数分列", "两者分别给出且方法可追溯",
                       lambda: _check_volume_fraction_separate(results)))
    rows.append(_check("G06", "跨相截断命名与诊断", "「未应用候选去除体积」+ 四项统计",
                       lambda: _check_truncation_naming(results)))

    # --- 红线（配置/schema 层拒绝）---------------------------------------
    cfg_cfrp, card_cfrp = load_case("cfrp_laminated_ply.json")
    cfg_alsic, _card_alsic = load_case("alsic_particle_composite.json")

    def legacy():
        return _expect_error(
            EXPECTED_REDLINE_CODES["legacy_delta_over_delta_ref"],
            lambda: build_phase(
                {"name": "x", "role": "matrix", "threshold_over_F_ref": 0.7, "delta_over_delta_ref": 1.0},
                phase_id=1, unit=cfg_cfrp.unit,
            ),
        )

    def borrow():
        return _expect_error(
            EXPECTED_REDLINE_CODES["phase_borrows_material_card"],
            lambda: build_phase(
                {"name": "SiC_particle", "role": "particle", "threshold_over_F_ref": 2.35,
                 "delta_over_L_ref": 0.2, "material_id": "sic_4h_cface_1035nm_multishot"},
                phase_id=2, unit=cfg_alsic.unit,
            ),
        )

    def forbidden_source():
        return _expect_error(
            EXPECTED_REDLINE_CODES["forbidden_response_source"],
            lambda: build_phase(
                {"name": "resin", "role": "matrix", "threshold_over_F_ref": 0.7,
                 "delta_over_L_ref": 0.35, "response_source": "parent_card_threshold"},
                phase_id=1, unit=cfg_cfrp.unit,
            ),
        )

    def split_threshold():
        return _expect_error(
            EXPECTED_REDLINE_CODES["overall_threshold_split"],
            lambda: assert_phase_threshold_not_split(
                _phase("resin", 1, CFRP_OVERALL_THRESHOLD_F_REF, 0.35, role="matrix"),
                card_cfrp, cfg_cfrp.unit,
            ),
        )

    def mutual_exclusion():
        raw = json.loads((EXAMPLES / "cfrp_laminated_ply.json").read_text(encoding="utf-8"))
        raw["solver"]["dynamic_angle"] = True
        rep = validate_run(RunConfig.from_dict(raw, base_dir=str(EXAMPLES)), card_cfrp)
        codes = [e["code"] for e in rep.errors]
        if EXPECTED_REDLINE_CODES["structured_x_dynamic_angle"] not in codes:
            raise AssertionError(f"未按预期拒绝：{codes}")
        return EXPECTED_REDLINE_CODES["structured_x_dynamic_angle"], "配置层拦截：分相截断与动态角度不得同时启用"

    def type_mismatch():
        raw = json.loads((EXAMPLES / "cfrp_laminated_ply.json").read_text(encoding="utf-8"))
        raw["structure"]["structure_type"] = "particle_composite"
        raw["structure"]["particles"] = {
            "matrix_phase": "resin", "particle_phase": "fiber",
            "mean_diameter_m": 3e-5, "depth_m": 5e-5,
        }
        rep = validate_run(RunConfig.from_dict(raw, base_dir=str(EXAMPLES)), card_cfrp)
        codes = [e["code"] for e in rep.errors]
        if EXPECTED_REDLINE_CODES["structure_type_mismatch"] not in codes:
            raise AssertionError(f"未按预期拒绝：{codes}")
        return EXPECTED_REDLINE_CODES["structure_type_mismatch"], "结构类型与材料卡声明不一致时拒绝"

    rows.append(_check("G06", "红线：旧 δ 字段", f"拒绝（{EXPECTED_REDLINE_CODES['legacy_delta_over_delta_ref']}）", legacy))
    rows.append(_check("G06", "红线：相借用其它材料卡", f"拒绝（{EXPECTED_REDLINE_CODES['phase_borrows_material_card']}）", borrow))
    rows.append(_check("G06", "红线：相沿用整体阈值来源", f"拒绝（{EXPECTED_REDLINE_CODES['forbidden_response_source']}）", forbidden_source))
    rows.append(_check("G06", "红线：整体阈值拆给分相", f"拒绝（{EXPECTED_REDLINE_CODES['overall_threshold_split']}）", split_threshold))
    rows.append(_check("G06", "红线：结构×动态角度互斥", f"拒绝（{EXPECTED_REDLINE_CODES['structured_x_dynamic_angle']}）", mutual_exclusion))
    rows.append(_check("G06", "红线：结构类型与卡一致", f"拒绝（{EXPECTED_REDLINE_CODES['structure_type_mismatch']}）", type_mismatch))

    # 统一核对实测码 == 期望码
    for r in rows:
        if r["kind"].startswith("红线") and r["status"] == "通过":
            expected = r["expected"].split("（")[1].rstrip("）")
            if r["measured"] != expected:
                r["status"] = "失败"
                r["detail"] = f"实测码 {r['measured']} != 期望 {expected}"
    return rows


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def write_markdown(
    structure_rows: list[dict[str, Any]],
    check_rows: list[dict[str, Any]],
    fig_paths: list[Path] | None = None,
) -> Path:
    n_pass = sum(1 for r in check_rows if r["status"] == "通过")
    n_fail = sum(1 for r in check_rows if r["status"] == "失败")
    lines = [
        "# G06：分相查询、颗粒/铺层与跨相界面截断报告（批次 G / T11–T13）",
        "",
        f"- 汇总：检查通过 {n_pass}｜失败 {n_fail}",
        "- 机读明细：`docs/reports/g06_phase_interfaces.csv`",
        "- 结构实例统计：`docs/reports/structure_instances.csv`",
        "",
        "> 本报告只做**数值实现验证**：合成结构不含任何实验复现结论；",
        "> 相响应为内联合成定义，跨相截断是有损近似。",
        "",
        "## 1. 两个合成结构实例",
        "",
        "| 算例 | 结构类型 | 材料 | 种子 | 算法 | 相 | 目标 Vf | 实际 Vf（方法） | 事件 | 截断事件 | 相切换事件 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in structure_rows:
        lines.append(
            f"| `{r['case']}` | {r['structure_type']} | `{r['material_id']}` | {r['seed']} | "
            f"{r['algorithm_version']} | {r['phase_names']} | "
            f"{'-' if r['target_volume_fraction'] is None else r['target_volume_fraction']} | "
            f"{r['actual_volume_fraction']}（{r['volume_fraction_method']}） | {r['n_events']} | "
            f"{r['clipped_events']} | {r['phase_switch_events']} |"
        )
    lines += [
        "",
        "### 结构与几何明细",
        "",
        "| 算例 | 相阈值（F_ref） | 相去除尺度（L_ref） | 铺层 输入/合并 | 颗粒 目标/实际 | 初末相单元 | 未应用候选占比 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in structure_rows:
        layers = (
            f"{r.get('n_layers_input')}/{r.get('n_layers_merged')}"
            if r.get("n_layers_input") is not None
            else "-"
        )
        particles = (
            f"{r.get('n_particles_target')}/{r.get('n_particles_placed')}"
            if r.get("n_particles_target") is not None
            else "-"
        )
        frac = r.get("unapplied_fraction_of_candidate")
        lines.append(
            f"| `{r['case']}` | {r['phase_threshold_over_F_ref']} | {r['phase_delta_over_L_ref']} | "
            f"{layers} | {particles} | {r['initial_phase_cells']} → {r['final_phase_cells']} | "
            f"{'-' if frac is None else f'{frac:.4g}'} |"
        )
    lines += [
        "",
        "> 相阈值与去除尺度都是**内部单位**（`F_ref` / `L_ref`）。`delta` 必须写作",
        "> `delta_over_L_ref`（δ/L_ref）：与层厚、光斑、离焦同一长度尺度；",
        "> 若误用 `delta_over_delta_ref`（δ/delta_ref），深度会整体放大 `L_ref/delta_ref`（本工程 100）倍。",
        "",
        "## 2. G06 检查明细",
        "",
        "| 检查 | 期望 | 实测 | 状态 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for r in check_rows:
        lines.append(f"| {r['kind']} | {r['expected']} | {r['measured']} | {r['status']} | {r['detail']} |")
    lines += [
        "",
        "## 3. 界面更新规则（执行细则 8 节）与本实现的对应",
        "",
        "| 规则 | 实现 | 位置 |",
        "|---|---|---|",
        "| 1 当前脉冲只调用当前暴露相 | `_per_phase_candidate` 每单元只命中一个相核，按 `phase_id` 分派 | `solver.py` |",
        "| 2 深度与到界面距离比较 | `applied = min(candidate, next_different_interface)` | `surface.apply_increment` |",
        "| 3 达到界面后更新相标签，下一真实脉冲才响应新相 | 更新 `phase_id`；本事件不重复施加完整能量 | `surface.apply_increment` |",
        "| 4 记录截断事件数/单元次数/候选减实际体积 | `clipped_events`、`n_clipped_cells`、`unapplied_candidate_removal_volume_internal` | `surface.py` / `solver.py` |",
        "| 5 同相相邻区间预先合并、统一浮点容差 | 构造时按指纹合并；`CONTACT_TOL_REL` 统一容差 | `structure.py` |",
        "",
        "## 4. 边界声明",
        "",
        "- 结构几何是**合成**的：颗粒顺序放置（`seed` 固定）、铺层条纹解析；",
        "  有限样本的目标体积分数与实际体积分数**分别报告**，不强制相等（如 0.45 ≠ 随机样本必然 0.45）。",
        "- 跨相截断是**有损近似**：被截断的候选量记为「未应用候选去除体积」，",
        "  **不是**剩余热量，也不是界面能量传输结果。",
        "- 分相截断与动态角度在配置层互斥（M0/M2 不开放该组合）；",
        "  几何意义未单独定义前不得混用。",
        "",
        "## 5. 复现命令",
        "",
        "```bash",
        "python -m ufdemo run examples/alsic_particle_composite.json --out runs/g06_alsic_particles --force-new-suffix",
        "python -m ufdemo run examples/cfrp_laminated_ply.json --out runs/g06_cfrp_plies --force-new-suffix",
        "python tools/structure_report.py",
        "python -m pytest -q -m g06",
        "```",
        "",
    ]
    out = D / "g06_phase_interfaces.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "runs" / "structure_report"))
    args = ap.parse_args()

    D.mkdir(parents=True, exist_ok=True)
    out_root = Path(args.out)

    from ufdemo.io import code_version, save_run

    results: dict[str, RunResult] = {}
    for name, label, _zh in STRUCTURE_CASES:
        cfg, card, res = solve_case(name)
        res.run_id = label
        save_run(res, out_root / label, project_root=ROOT, code_info=code_version(ROOT))
        results[name] = res

    structure_rows = collect_structure_rows(results)
    check_rows = collect_g06_checks(results)

    struct_csv = D / "structure_instances.csv"
    with open(struct_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(structure_rows[0].keys()))
        w.writeheader()
        w.writerows(structure_rows)

    check_csv = D / "g06_phase_interfaces.csv"
    with open(check_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(check_rows[0].keys()))
        w.writeheader()
        w.writerows(check_rows)

    md_path = write_markdown(structure_rows, check_rows)

    n_fail = sum(1 for r in check_rows if r["status"] == "失败")
    print(f"报告：{md_path}")
    print(f"报告：{struct_csv}")
    print(f"报告：{check_csv}")
    print(f"结构实例 {len(structure_rows)} 个｜检查 {len(check_rows)} 项（失败 {n_fail}）")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
