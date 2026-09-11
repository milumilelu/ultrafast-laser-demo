"""查表报告生成器（批次 F / T10）。

批次 F 必交三件产物，本脚本一次生成：

1. **示例 CSV** —— ``data/curves/*.points.csv``（由 ``tools/make_curves.py`` 生成）；
2. **错误 CSV** —— ``docs/reports/table_errors.csv``：每条违反规则的输入、期望错误码、
   实际错误码与状态；
3. **原始点与插值图** —— ``docs/reports/curve_interpolation.html``（原始点散点 +
   分段线性 + 保形 PCHIP 同图）与 ``docs/reports/curve_interpolation.csv``（数值）。

另外生成 ``docs/reports/table_lookup.md`` 汇总报告，并返回可机读检查行供
``tools/run_acceptance.py`` 使用。

用法::

    python tools/table_report.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ufdemo import tables as T  # noqa: E402
from ufdemo.config import (  # noqa: E402
    SEMANTIC_EVENT_INCREMENT,
    SEMANTIC_VOLUME_PER_ENERGY,
)
from ufdemo.errors import (  # noqa: E402
    CONDITION_MISMATCH,
    CONFIG_INVALID,
    NOT_IMPLEMENTED,
    NUMERIC_NONFINITE,
    RESPONSE_SEMANTICS_INVALID,
    TABLE_OUT_OF_RANGE,
    UFDemoError,
)

D = ROOT / "docs" / "reports"
CURVES_DIR = ROOT / "data" / "curves"
INVALID_DIR = ROOT / "tests" / "fixtures" / "curves_invalid"

# 每组无效夹具**应当**触发哪个错误码（这是断言，不是描述）。
EXPECTED_CARD_CODES: dict[str, str] = {
    "invalid_unsorted": CONFIG_INVALID,
    "invalid_duplicate": CONFIG_INVALID,
    "invalid_duplicate_no_rule": CONFIG_INVALID,
    "invalid_nonfinite": NUMERIC_NONFINITE,
    "invalid_single_point": CONFIG_INVALID,
    "invalid_bad_header": CONFIG_INVALID,
    "invalid_nonnumeric": CONFIG_INVALID,
    "invalid_range_narrow": CONFIG_INVALID,
    "invalid_volume_depth_dir": CONFIG_INVALID,
    "invalid_missing_field": CONFIG_INVALID,
    "invalid_empty": CONFIG_INVALID,
    "invalid_bad_semantics": CONFIG_INVALID,
    "invalid_no_depth_direction": CONFIG_INVALID,
    "invalid_no_unit": CONFIG_INVALID,
}

# 行为级案例（不依赖单独夹具）：名称 → (调用说明, 期望错误码)
EXPECTED_BEHAVIOR_CODES: dict[str, str] = {
    "lookup_below_range": TABLE_OUT_OF_RANGE,
    "lookup_above_range": TABLE_OUT_OF_RANGE,
    "pchip_without_scipy": NOT_IMPLEMENTED,
    "local_depth_from_volume": CONFIG_INVALID,
    "event_kernel_from_threshold": RESPONSE_SEMANTICS_INVALID,
    "condition_mismatch_on_event_curve": CONDITION_MISMATCH,
    "unknown_interpolation_method": CONFIG_INVALID,
}


def _row(case: str, kind: str, expected: str, actual: str, status: str, detail: str) -> dict[str, Any]:
    return {
        "case": case,
        "kind": kind,
        "expected_code": expected,
        "actual_code": actual,
        "status": status,
        "detail": detail,
    }


def collect_error_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    # --- 1) 曲线卡层面的拒收 -----------------------------------------------
    for name, expected in EXPECTED_CARD_CODES.items():
        path = INVALID_DIR / f"{name}.curve.json"
        try:
            T.load_curve(path)
            rows.append(_row(name, "card_schema", expected, "（未拒绝）", "失败",
                             "该夹具本应被拒绝，但加载成功"))
        except UFDemoError as err:
            rows.append(_row(name, "card_schema", expected, err.code,
                             "通过" if err.code == expected else "失败", err.message))
        except Exception as err:  # noqa: BLE001
            rows.append(_row(name, "card_schema", expected, type(err).__name__, "失败", repr(err)))

    # --- 2) 行为级拒收 -----------------------------------------------------
    ysz = T.load_curve(CURVES_DIR / "ysz_analytic_depth_vs_fluence.curve.json")
    sic = T.load_curve(CURVES_DIR / "sic_threshold_vs_effective_n.curve.json")
    vol = T.load_curve(CURVES_DIR / "synthetic_volume_per_energy.curve.json")

    probes: list[tuple[str, str, Any]] = [
        ("lookup_below_range", "越界：低于有效区间下界（不得返回 0）",
         lambda: T.lookup(ysz, 0.5)),
        ("lookup_above_range", "越界：高于有效区间上界",
         lambda: T.lookup(ysz, 1e3)),
        ("pchip_without_scipy", "PCHIP 缺 SciPy 时必须显式报错，不得静默退化为线性",
         lambda: _lookup_pchip_without_scipy(ysz)),
        ("local_depth_from_volume", "体积曲线不得反推局部去除深度",
         lambda: T.assert_no_local_depth_from_volume(vol, "removal_depth_per_pulse")),
        ("event_kernel_from_threshold", "阈值曲线不得进入逐事件核",
         lambda: T.assert_curve_can_enter_event_kernel(sic)),
        ("condition_mismatch_on_event_curve", "固定条件不匹配时拒绝逐事件核",
         lambda: T.assert_curve_can_enter_event_kernel(
             ysz, laser={"repetition_rate_Hz": 200000.0})),
        ("unknown_interpolation_method", "未登记的插值方法拒绝",
         lambda: T.lookup(ysz, 5.0, method="cubic_spline")),
    ]
    for name, detail, fn in probes:
        expected = EXPECTED_BEHAVIOR_CODES[name]
        try:
            fn()
            rows.append(_row(name, "behavior", expected, "（未拒绝）", "失败", detail))
        except UFDemoError as err:
            rows.append(_row(name, "behavior", expected, err.code,
                             "通过" if err.code == expected else "失败", detail))
        except Exception as err:  # noqa: BLE001
            rows.append(_row(name, "behavior", expected, type(err).__name__, "失败", repr(err)))
    return rows


def _lookup_pchip_without_scipy(curve: T.ResponseCurve):
    """临时把 PCHIP 标记为不可用，验证**不会**静默退化为线性。"""
    saved = T.PCHIP_AVAILABLE
    try:
        T.PCHIP_AVAILABLE = False
        return T.lookup(curve, 5.0, method="pchip")
    finally:
        T.PCHIP_AVAILABLE = saved


def collect_curve_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for card in T.iter_curve_cards(CURVES_DIR):
        c = T.load_curve(card)
        rows.append(
            {
                "curve_id": c.curve_id,
                "material_id": c.material_id,
                "evidence_status": c.evidence_status,
                "source_type": c.source_type,
                "x_quantity": c.x_quantity["name"],
                "x_unit": c.x_unit,
                "y_quantity": c.y_quantity["name"],
                "y_unit": c.y_unit,
                "output_semantics": c.output_semantics,
                "route": c.route,
                "can_enter_event_kernel": c.can_enter_event_kernel,
                "valid_range_lo": c.valid_range[0],
                "valid_range_hi": c.valid_range[1],
                "raw_point_count": len(c.raw_points),
                "point_count": len(c.points),
                "duplicate_policy": c.duplicate_report.policy,
                "duplicate_applied": c.duplicate_report.applied,
                "source_figure_or_table": c.source_figure_or_table,
                "derivable": ",".join(T.derivable_quantities(c)),
            }
        )
    return rows


def collect_interpolation_rows() -> list[dict[str, Any]]:
    """原始点与两条插值线在同一张长表里，便于逐点核对。"""
    rows: list[dict[str, Any]] = []
    for card in T.iter_curve_cards(CURVES_DIR):
        c = T.load_curve(card)
        lin = T.interpolate_grid(c, n=201, method="linear")
        pch = T.interpolate_grid(c, n=201, method="pchip") if T.PCHIP_AVAILABLE else None
        for i, x in enumerate(lin["x"]):
            rows.append({
                "curve_id": c.curve_id,
                "kind": "interpolated",
                "x": x,
                "linear": lin["y"][i],
                "pchip": None if pch is None else pch["y"][i],
            })
        for x, y in c.raw_points:
            rows.append({
                "curve_id": c.curve_id,
                "kind": "raw_point",
                "x": x,
                "linear": y,
                "pchip": y,
            })
    return rows


def write_figure() -> Path:
    """原始点 + 分段线性 + 保形 PCHIP 三合一图（自包含 HTML）。"""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    cards = list(T.iter_curve_cards(CURVES_DIR))
    fig = make_subplots(
        rows=len(cards), cols=1,
        subplot_titles=[
            f"{T.load_curve(p).curve_id}｜{T.load_curve(p).output_semantics}"
            f"<br><sup>{T.curve_watermark(T.load_curve(p))}</sup>"
            for p in cards
        ],
    )
    for r, path in enumerate(cards, start=1):
        c = T.load_curve(path)
        lin = T.interpolate_grid(c, n=201, method="linear")
        fig.add_trace(
            go.Scatter(x=lin["x"], y=lin["y"], mode="lines", name=f"{c.curve_id} 线性",
                       line=dict(color="#1D9E75", width=2),
                       legendgroup="linear", showlegend=(r == 1)),
            row=r, col=1,
        )
        if T.PCHIP_AVAILABLE:
            pch = T.interpolate_grid(c, n=201, method="pchip")
            fig.add_trace(
                go.Scatter(x=pch["x"], y=pch["y"], mode="lines", name="PCHIP（保形）",
                           line=dict(color="#185FA5", width=2, dash="dot"),
                           legendgroup="pchip", showlegend=(r == 1)),
                row=r, col=1,
            )
        fig.add_trace(
            go.Scatter(x=[p[0] for p in c.raw_points], y=[p[1] for p in c.raw_points],
                       mode="markers", name="原始数据点",
                       marker=dict(color="#D85A30", size=9, symbol="circle-open", line=dict(width=2)),
                       legendgroup="raw", showlegend=(r == 1)),
            row=r, col=1,
        )
        fig.update_yaxes(title_text=f"{c.y_quantity['name']} [{c.y_unit}]", row=r, col=1)
        fig.update_xaxes(title_text=f"{c.x_quantity['name']} [{c.x_unit}]", row=r, col=1)

    fig.update_layout(
        title=(
            "查表：原始数据点与插值线（批次 F / T10）<br>"
            "<sup>插值仅在声明有效区间内进行；不外推、不钳端点、越界不返回 0。"
            "图像仅用于插值链路核查，不构成材料物理验证。</sup>"
        ),
        height=420 * len(cards),
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=70, r=30, t=120, b=50),
    )
    out = D / "curve_interpolation.html"
    fig.write_html(out, include_plotlyjs="cdn", full_html=True)
    return out


def write_markdown(
    curve_rows: list[dict[str, Any]],
    err_rows: list[dict[str, Any]],
    fig_path: Path,
) -> Path:
    n_pass = sum(1 for r in err_rows if r["status"] == "通过")
    n_fail = sum(1 for r in err_rows if r["status"] == "失败")
    lines = [
        "# 查表报告（批次 F / T10）",
        "",
        "> 对照 `docs/reports/progress.md` 与本目录的验收报告阅读。",
        "",
        "## 1. 曲线清单（示例 CSV）",
        "",
        "| 曲线 | 材料 | 横轴（单位） | 纵轴（单位） | 语义 → 去向 | 有效区间 | 原始点 | 可派生量 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in curve_rows:
        route_zh = T.ROUTE_ZH[r["route"]]
        lines.append(
            f"| `{r['curve_id']}` | `{r['material_id']}` | {r['x_quantity']} ({r['x_unit']}) | "
            f"{r['y_quantity']} ({r['y_unit']}) | {r['output_semantics']} → {route_zh} | "
            f"[{r['valid_range_lo']}, {r['valid_range_hi']}] | {r['raw_point_count']} | {r['derivable']} |"
        )
    lines += [
        "",
        "曲线卡与点文件都在 `data/curves/`：卡是 JSON 元数据（执行细则 7 节的查表最少字段），"
        "点文件是 CSV（`x,y` 两列，允许 `#` 注释）。**CSV 是原始点的唯一来源**，"
        "加载后原样保留在 `raw_points`，去重结果在 `points`。",
        "",
        "## 2. 错误 CSV：被拒收的输入与错误码",
        "",
        f"- 汇总：通过 {n_pass}｜失败 {n_fail}",
        f"- 机读明细：`docs/reports/table_errors.csv`",
        "",
        "| 案例 | 类别 | 期望错误码 | 实际错误码 | 状态 | 说明 |",
        "|---|---|---|---|---|---|",
    ]
    for r in err_rows:
        lines.append(
            f"| `{r['case']}` | {r['kind']} | `{r['expected_code']}` | `{r['actual_code']}` | "
            f"{r['status']} | {r['detail']} |"
        )
    lines += [
        "",
        "## 3. 原始点与插值图",
        "",
        f"- 图：`docs/reports/curve_interpolation.html`（原始点散点 + 分段线性 + 保形 PCHIP）",
        "- 数值：`docs/reports/curve_interpolation.csv`（长表：每条曲线的 201 点插值线 + 原始点）",
        "",
        "## 4. 具体执行方式",
        "",
        "| 要求 | 实现 | 位置 |",
        "|---|---|---|",
        "| 最少字段（细则 7 节） | `REQUIRED_CURVE_FIELDS` 逐项校验，缺一即拒 | `tables.py` |",
        "| 默认分段线性 | 手写二分分段线性（**不用** `numpy.interp`，避免区间外静默钳端点） | `_linear()` |",
        "| 可选 PCHIP 且关闭外推 | `PchipInterpolator(..., extrapolate=False)`；缺 SciPy 报 `NOT_IMPLEMENTED`，**不静默退化** | `_pchip()` |",
        "| 越界返回状态与原因 | `TABLE_OUT_OF_RANGE`；`allow_out_of_range=True` 时返回 `None`（**不是 0**） | `lookup()` |",
        "| 低于量测区间不自动变零 | 越界项一律 `None`；只有曲线自带独立阈值律时零点才来自数据 | `lookup()` / 曲线点 |",
        "| 横坐标严格排序且无重复 | 未升序拒绝；重复 x 默认拒绝 | `load_curve_points()` |",
        "| 重复 x 不静默删除 | 需 `duplicate_rule_note`；原始点保留在 `raw_points` | `load_curve_points()` |",
        "| 只有逐事件增量曲线进事件核 | `assert_curve_can_enter_event_kernel()`（另含条件比对） | `tables.py` |",
        "| 体积不得反推局部深度 | `assert_no_local_depth_from_volume()` | `tables.py` |",
        "| 曲线语义不新增枚举 | `output_semantics` 只允许执行细则 4.3 的 6 个值 | `load_curve()` |",
        "",
        "## 5. 边界声明",
        "",
        "- 本报告只做**公式核查**与**数值实现验证**：",
        "  YSZ 曲线来自解析 fixture（可与人解析式逐点比对）；",
        "  SiC 曲线由**材料卡内拟合参数按式(6) 重算**，不是原图逐点数字化，因此",
        "  **不构成对原文曲线的复现**；合成体积曲线无任何实验来源。",
        "- 查表本身**不产生**形貌：不存在 `final_surface.npz`。逐事件核接入属批次 H（T14），",
        "  本批只提供闸门与拒绝路径。",
        "",
        "## 6. 复现命令",
        "",
        "```bash",
        "python tools/make_curves.py",
        "python tools/table_report.py",
        "python -m ufdemo table data/curves/ysz_analytic_depth_vs_fluence.curve.json --x 5",
        "python -m ufdemo table data/curves/ysz_analytic_depth_vs_fluence.curve.json --x 2.5 --method pchip",
        "python -m pytest -q -m g09",
        "```",
        "",
    ]
    out = D / "table_lookup.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> int:
    D.mkdir(parents=True, exist_ok=True)
    curve_rows = collect_curve_rows()
    err_rows = collect_error_rows()
    interp_rows = collect_interpolation_rows()

    err_csv = D / "table_errors.csv"
    with open(err_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(err_rows[0].keys()))
        w.writeheader()
        w.writerows(err_rows)

    interp_csv = D / "curve_interpolation.csv"
    with open(interp_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(interp_rows[0].keys()))
        w.writeheader()
        w.writerows(interp_rows)

    fig_path = write_figure()
    md_path = write_markdown(curve_rows, err_rows, fig_path)

    n_fail = sum(1 for r in err_rows if r["status"] == "失败")
    print(f"报告：{md_path}")
    print(f"报告：{err_csv}")
    print(f"报告：{interp_csv}")
    print(f"插图：{fig_path}")
    print(f"曲线 {len(curve_rows)} 条｜拒收检查 {len(err_rows)} 项（失败 {n_fail}）")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
