"""生成查表示例曲线与无效夹具（批次 F / T10）。

**为什么用生成器**：曲线点的数值必须可复现、可追溯来源。手写 CSV 的数字无法证明
来自哪里；本脚本把每个点的推导写死在代码里，任何人重跑都能得到同样的字节。

生成两类产物：

1. ``data/curves/`` —— 三条**示例曲线**（各一张 ``.curve.json`` + 一个 ``.points.csv``）：

   * ``ysz_analytic_depth_vs_fluence`` —— 来自解析 fixture 的逐事件增量曲线
     ``a(F) = delta * max(ln(F/Fth), 0)``，可与人解析公式逐点对照；
   * ``sic_threshold_vs_effective_n`` —— 由 SiC 材料卡**拟合参数**按式(6)
     重算的阈值曲线，可与 ``ufdemo.references.sic_threshold_fluence`` 对照；
   * ``synthetic_volume_per_energy`` —— 无量纲合成体积曲线，用来演示
     「体积曲线不得反推局部深度」这条红线。

2. ``tests/fixtures/curves_invalid/`` —— **错误 CSV 与错误曲线卡**，每种违反一条
   校验规则；``tools/table_report.py`` 会把每条的实际错误码写进
   ``docs/reports/table_errors.csv``。

用法::

    python tools/make_curves.py
"""

from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CURVES_DIR = ROOT / "data" / "curves"
INVALID_DIR = ROOT / "tests" / "fixtures" / "curves_invalid"

# --- 解析 fixture 常量（与 tests/fixtures/analytic_fixture.json 一致）---------
FTH_J_CM2 = 1.0          # 1 J/cm^2 = 1e4 J/m^2
DELTA_M = 1.0e-7         # 100 nm
W0_M = 1.0e-5

# --- SiC 卡内拟合参数（与 sic_4h_cface_1035nm_multishot.json 一致）-----------
SIC_F1_J_M2 = 23500.0
SIC_FINF_J_M2 = 7000.0
SIC_K = 0.0199


def _write_csv(path: Path, rows: list[tuple[float, float]], *, header_note: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("# ufdemo curve points — 单位见同目录 *.curve.json 的 x_quantity/y_quantity\n")
        if header_note:
            fh.write(f"# {header_note}\n")
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["x", "y"])
        for x, y in rows:
            w.writerow([repr(float(x)), repr(float(y))])


def _dump(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1) YSZ 解析逐事件增量曲线
# ---------------------------------------------------------------------------


def make_ysz_analytic() -> None:
    cid = "ysz_analytic_depth_vs_fluence"
    fluences = [1.0, 1.5, 2.0, 3.0, 5.0, math.exp(2.0), 10.0, 20.0, 40.0]
    rows = [(F, DELTA_M * max(math.log(F / FTH_J_CM2), 0.0)) for F in fluences]
    # 校验：e^2 处应恰好等于 2*delta = 200 nm
    assert abs(rows[5][1] - 2 * DELTA_M) < 1e-24
    _write_csv(
        CURVES_DIR / f"{cid}.points.csv",
        rows,
        header_note="a(F) = delta * ln(F/Fth)，Fth=1 J/cm^2，delta=100 nm（解析 fixture 定义）",
    )
    _dump(
        CURVES_DIR / f"{cid}.curve.json",
        {
            "schema_version": "1.0",
            "curve_id": cid,
            "material_id": "analytic_fixture_not_a_material",
            "material_identity": {
                "family": "mathematical_fixture",
                "grade": None,
                "material_identity_confirmed_by_user": False,
            },
            "x_quantity": {
                "name": "peak_fluence",
                "unit": "J/cm^2",
                "label": "峰值能流（入射表面峰值）",
                "kind": "fluence",
            },
            "y_quantity": {
                "name": "removal_depth_per_pulse",
                "unit": "m",
                "label": "单脉冲去除深度",
                "kind": "depth",
            },
            "output_semantics": "event_depth_increment",
            "depth_direction": "surface_normal",
            "fluence_basis": "incident_surface_peak",
            "fixed_conditions": {
                "wavelength_m": {"value": None, "rel_tol": None, "note": "解析 fixture 未定义波长"},
                "pulse_duration_s": {"value": None, "rel_tol": None, "note": "解析 fixture 未定义脉宽"},
                "repetition_rate_Hz": {"value": 1000.0, "rel_tol": 0.0},
                "spot_radius_m": {"value": W0_M, "rel_tol": 0.0},
                "history": {"value": "none", "note": "人工解析基准关闭全部历史与孵化"},
                "environment": {"value": "not_applicable"},
            },
            "protocol": {
                "protocol_id": "analytic_single_pulse_definition",
                "required_history": {"mode": "none"},
                "protocol_note": "式 a=delta*ln(F/Fth) 的单脉冲解析定义；不是材料数据。",
            },
            "source_figure_or_table": "执行细则 10 节 G01 解析定义（人工构造，非文献图）",
            "valid_range": {
                "x": [1.0, 40.0],
                "note": "Fth=1 J/cm^2 起；低于 Fth 的深度为 0（由阈值律支持，不是越界填零）",
            },
            "points_file": f"{cid}.points.csv",
            "duplicate_policy": "reject",
            "evidence_status": "unverified",
            "source_type": "analytic_test_definition",
            "source": {"source_ids": []},
            "notes": [
                "本曲线用于**数值实现验证**：节点处线性插值应精确等于解析式。",
                "节点之间的线性插值与解析对数式不同，这是分段线性的固有近似，不是误差。",
            ],
            "limitations": [
                "解析 fixture 不是材料卡数据；不得据此输出材料物理预测。",
                "低于 Fth 的深度为 0 来自解析阈值律，不适用于没有独立阈值律的曲线。",
            ],
        },
    )


# ---------------------------------------------------------------------------
# 2) SiC 阈值随有效 N 变化（由卡片拟合参数重算式(6)）
# ---------------------------------------------------------------------------


def make_sic_threshold() -> None:
    cid = "sic_threshold_vs_effective_n"
    ns = [1, 2, 5, 10, 50, 100, 720]
    rows = [
        (float(n), (SIC_FINF_J_M2 + (SIC_F1_J_M2 - SIC_FINF_J_M2) * math.exp(-SIC_K * (n - 1))) / 1e4)
        for n in ns
    ]
    # 校验端点：N=1 -> 2.35 J/cm^2
    assert abs(rows[0][1] - 2.35) < 1e-12
    _write_csv(
        CURVES_DIR / f"{cid}.points.csv",
        rows,
        header_note="Fth(N)=F_inf+(F1-F_inf)*exp(-k*(N-1))，参数取自 SiC 材料卡 multi_response",
    )
    _dump(
        CURVES_DIR / f"{cid}.curve.json",
        {
            "schema_version": "1.0",
            "curve_id": cid,
            "material_id": "sic_4h_cface_1035nm_multishot",
            "material_identity": {
                "family": "SiC",
                "grade": "N 掺杂 4H-SiC C 面",
                "material_identity_confirmed_by_user": False,
            },
            "x_quantity": {
                "name": "effective_count",
                "unit": "1",
                "label": "原文有效脉冲数 N_eff（不是真实脉冲事件数）",
                "kind": "count",
            },
            "y_quantity": {
                "name": "threshold_fluence",
                "unit": "J/cm^2",
                "label": "多脉冲阈值能流 Fth(N)",
                "kind": "threshold",
            },
            "output_semantics": "threshold_only",
            "fixed_conditions": {
                "wavelength_m": {"value": 1.035e-6, "rel_tol": 0.01},
                "pulse_duration_s": {"value": 3.0e-13, "rel_tol": 0.05},
                "repetition_rate_Hz": {"value": 200000.0, "rel_tol": 0.02},
                "environment": {"value": "air"},
                "history": {"mode": "effective_N_saturation"},
            },
            "protocol": {
                "protocol_id": "sic4h_cface_multishot_reference_N",
                "required_history": {"effective_count": 720, "definition": "original_paper_effective_N"},
                "protocol_note": "阈值曲线只在原文协议的有效 N 定义下有效。",
            },
            "source_figure_or_table": "F01 P38-P43（S02 式 6；本 CSV 由卡内拟合参数重算，不是原图数字化）",
            "valid_range": {
                "x": [1.0, 720.0],
                "note": "N 上限取卡内协议值 720；外推到大 N 无实测支持",
            },
            "points_file": f"{cid}.points.csv",
            "duplicate_policy": "reject",
            "evidence_status": "literature_reported",
            "source_type": "card_formula_recomputed",
            "source": {"source_ids": ["S02"]},
            "notes": [
                "**由材料卡的拟合参数重算**，不是从原图逐点数字化；因此本曲线只用于"
                "公式核查与插值链路检查，不构成对原文曲线的复现。",
                "N=1 端点与卡内 threshold_J_m2=23500 J/m^2 一致。",
            ],
            "limitations": [
                "阈值曲线不得当去除深度使用；本曲线 output_semantics=threshold_only。",
                "扫描次数列表存在重复项，本批只接受直接输入原文有效 N。",
            ],
        },
    )


# ---------------------------------------------------------------------------
# 3) 合成体积曲线（演示「体积不得反推局部深度」）
# ---------------------------------------------------------------------------


def make_synthetic_volume() -> None:
    cid = "synthetic_volume_per_energy"
    # 无量纲定义：V/L_ref^3 = 0.8 * max(E/E_ref - 0.35, 0)
    slope, onset = 0.8, 0.35
    es = [0.35, 0.5, 1.0, 2.0, 4.0, 8.0]
    rows = [(e, slope * max(e - onset, 0.0)) for e in es]
    _write_csv(
        CURVES_DIR / f"{cid}.points.csv",
        rows,
        header_note="合成定义：V/L_ref^3 = 0.8 * max(E/E_ref - 0.35, 0)；无量纲，禁止解释为真实材料",
    )
    _dump(
        CURVES_DIR / f"{cid}.curve.json",
        {
            "schema_version": "1.0",
            "curve_id": cid,
            "material_id": "synthetic_demo_isotropic",
            "material_identity": {
                "family": "synthetic_demo",
                "grade": None,
                "material_identity_confirmed_by_user": False,
            },
            "x_quantity": {
                "name": "pulse_energy_over_E_ref",
                "unit": "1",
                "label": "单脉冲能量（以 E_ref 归一）",
                "kind": "scalar",
            },
            "y_quantity": {
                "name": "removal_volume_over_L_ref3",
                "unit": "1",
                "label": "终态去除体积（以 L_ref^3 归一）",
                "kind": "volume",
            },
            "output_semantics": "volume_per_energy",
            "fixed_conditions": {
                "wavelength_m": {"value": None, "rel_tol": None, "note": "合成演示未定义"},
                "pulse_duration_s": {"value": None, "rel_tol": None, "note": "合成演示未定义"},
                "repetition_rate_Hz": {"value": None, "rel_tol": None, "note": "合成演示未定义"},
                "reference_scales": {"L_ref_m": 1e-5, "F_ref_J_m2": 10000.0, "delta_ref_m": 1e-7},
                "environment": {"value": "synthetic"},
            },
            "protocol": {
                "protocol_id": "synthetic_volume_demo",
                "required_history": {"mode": "none"},
                "protocol_note": "合成定义，无任何实验来源。",
            },
            "source_figure_or_table": "无来源：合成演示定义（tools/make_curves.py 顶部常量）",
            "valid_range": {"x": [0.35, 8.0], "note": "低于阈值 0.35 无去除（合成阈值律定义）"},
            "points_file": f"{cid}.points.csv",
            "duplicate_policy": "reject",
            "evidence_status": "unverified",
            "source_type": "synthetic_illustrative",
            "source": {"source_ids": []},
            "notes": [
                "刻意选择 volume_per_energy 语义：用来演示与测试"
                "「仅有终态体积时不得反推局部去除深度」这条红线。",
            ],
            "limitations": [
                "全部数值为合成定义，不得解释为任何真实材料的响应。",
                "体积曲线只进体积评估器；不得生成局部坑形。",
            ],
        },
    )


# ---------------------------------------------------------------------------
# 4) 无效夹具（错误 CSV / 错误卡片）
# ---------------------------------------------------------------------------


def _base_card(cid: str, **over) -> dict:
    card = {
        "schema_version": "1.0",
        "curve_id": cid,
        "material_id": "analytic_fixture_not_a_material",
        "material_identity": {"family": "mathematical_fixture", "grade": None},
        "x_quantity": {"name": "peak_fluence", "unit": "J/cm^2", "kind": "fluence"},
        "y_quantity": {"name": "removal_depth_per_pulse", "unit": "m", "kind": "depth"},
        "output_semantics": "event_depth_increment",
        "depth_direction": "surface_normal",
        "fixed_conditions": {"repetition_rate_Hz": {"value": 1000.0, "rel_tol": 0.0}},
        "protocol": {"protocol_id": "invalid_fixture"},
        "source_figure_or_table": "无效夹具（故意构造，仅用于校验测试）",
        "valid_range": {"x": [1.0, 40.0], "note": "夹具"},
        "points_file": f"{cid}.points.csv",
        "duplicate_policy": "reject",
        "evidence_status": "unverified",
        "source_type": "analytic_test_definition",
    }
    card.update(over)
    return card


def _raw_csv(cid: str, lines: list[str]) -> None:
    """按 curve_id 写点文件；文件名与卡片的 points_file 严格一致。"""
    path = INVALID_DIR / f"{cid}.points.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_invalid_fixtures() -> None:
    # 1. 横坐标未升序
    _raw_csv("invalid_unsorted", ["x,y", "1.0,0.0", "5.0,1.0", "3.0,0.5", "10.0,2.0"])
    _dump(INVALID_DIR / "invalid_unsorted.curve.json", _base_card("invalid_unsorted"))

    # 2. 重复 x 且策略为 reject（默认）
    _raw_csv("invalid_duplicate", ["x,y", "1.0,0.0", "2.0,1.0", "2.0,1.4", "5.0,2.0"])
    _dump(INVALID_DIR / "invalid_duplicate.curve.json", _base_card("invalid_duplicate"))

    # 3. 声明了聚合策略但没有重复试验处理规则（复用 2 的点文件）
    _dump(
        INVALID_DIR / "invalid_duplicate_no_rule.curve.json",
        _base_card(
            "invalid_duplicate_no_rule",
            duplicate_policy="mean",
            points_file="invalid_duplicate.points.csv",
        ),
    )

    # 4. 非有限值
    _raw_csv("invalid_nonfinite", ["x,y", "1.0,0.0", "2.0,nan", "5.0,2.0"])
    _dump(INVALID_DIR / "invalid_nonfinite.curve.json", _base_card("invalid_nonfinite"))

    # 5. 只有一个点
    _raw_csv("invalid_single_point", ["x,y", "1.0,0.0"])
    _dump(INVALID_DIR / "invalid_single_point.curve.json", _base_card("invalid_single_point"))

    # 6. 表头不是 x,y
    _raw_csv("invalid_bad_header", ["fluence,depth", "1.0,0.0", "2.0,1.0"])
    _dump(INVALID_DIR / "invalid_bad_header.curve.json", _base_card("invalid_bad_header"))

    # 7. 无法解析为数值
    _raw_csv("invalid_nonnumeric", ["x,y", "1.0,0.0", "abc,1.0", "5.0,2.0"])
    _dump(INVALID_DIR / "invalid_nonnumeric.curve.json", _base_card("invalid_nonnumeric"))

    # 8. 有效区间窄于数据点
    _raw_csv("invalid_range_narrow", ["x,y", "1.0,0.0", "2.0,1.0", "50.0,3.0"])
    _dump(
        INVALID_DIR / "invalid_range_narrow.curve.json",
        _base_card("invalid_range_narrow", valid_range={"x": [1.0, 20.0], "note": "故意窄于数据"}),
    )

    # 9. 体积曲线却声明 depth_direction
    _raw_csv("invalid_volume_depth_dir", ["x,y", "1.0,0.0", "2.0,1.0"])
    _dump(
        INVALID_DIR / "invalid_volume_depth_dir.curve.json",
        _base_card(
            "invalid_volume_depth_dir",
            output_semantics="volume_per_energy",
            depth_direction="surface_normal",
            y_quantity={"name": "removal_volume", "unit": "m^3", "kind": "volume"},
        ),
    )

    # 10. 缺少必填字段 protocol
    _raw_csv("invalid_missing_field", ["x,y", "1.0,0.0", "2.0,1.0"])
    card = _base_card("invalid_missing_field")
    card.pop("protocol")
    _dump(INVALID_DIR / "invalid_missing_field.curve.json", card)

    # 11. 只有表头、没有数据点
    _raw_csv("invalid_empty", ["x,y"])
    _dump(INVALID_DIR / "invalid_empty.curve.json", _base_card("invalid_empty"))

    # 12. 未登记的响应语义
    _raw_csv("invalid_bad_semantics", ["x,y", "1.0,0.0", "2.0,1.0"])
    _dump(
        INVALID_DIR / "invalid_bad_semantics.curve.json",
        _base_card("invalid_bad_semantics", output_semantics="lookup_curve"),
    )

    # 13. 逐事件增量曲线缺 depth_direction
    _raw_csv("invalid_no_depth_direction", ["x,y", "1.0,0.0", "2.0,1.0"])
    card = _base_card("invalid_no_depth_direction")
    card.pop("depth_direction")
    _dump(INVALID_DIR / "invalid_no_depth_direction.curve.json", card)

    # 14. 单位缺失（空串）
    _raw_csv("invalid_no_unit", ["x,y", "1.0,0.0", "2.0,1.0"])
    _dump(
        INVALID_DIR / "invalid_no_unit.curve.json",
        _base_card("invalid_no_unit", y_quantity={"name": "removal_depth_per_pulse", "unit": "  "}),
    )


def main() -> int:
    make_ysz_analytic()
    make_sic_threshold()
    make_synthetic_volume()
    make_invalid_fixtures()
    n_valid = len(list(CURVES_DIR.glob("*.curve.json")))
    n_invalid = len(list(INVALID_DIR.glob("*.curve.json")))
    print(f"示例曲线：{n_valid} 条 → {CURVES_DIR}")
    print(f"无效夹具：{n_invalid} 组 → {INVALID_DIR}")
    for p in sorted(CURVES_DIR.glob("*.curve.json")):
        print(f"  - {p.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
