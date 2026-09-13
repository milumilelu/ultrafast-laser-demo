"""批次 F / T10 —— 查表：曲线 schema、插值与越界（G09 的查表部分）。

任务书 G09 对查表的明文要求：

* 曲线横坐标**严格排序且无重复**；
* 重复试验先按明确规则处理，**不静默删除**；
* 插值**无越界外推**；
* **低于量测域不自动变成零**，除非有独立阈值律支持；
* 原始点和插值线**同时可查看**。

执行细则 7 节另加两条红线：

* 只有 ``event_depth_increment`` 曲线且协议确实适用时才能进入事件核；
* 平均/累计/体积曲线进入评估器；**体积曲线不得反推局部去除深度**。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from ufdemo import tables as T
from ufdemo import references as REF
from ufdemo.errors import (
    CONDITION_MISMATCH,
    CONFIG_INVALID,
    NOT_IMPLEMENTED,
    NUMERIC_NONFINITE,
    RESPONSE_SEMANTICS_INVALID,
    TABLE_OUT_OF_RANGE,
    UFDemoError,
)

pytestmark = pytest.mark.g09

ROOT = Path(__file__).resolve().parents[1]
CURVES = ROOT / "data" / "curves"
INVALID = ROOT / "tests" / "fixtures" / "curves_invalid"

FIXTURE = "analytic_fixture_depth_vs_fluence"
SIC = "sic_threshold_vs_effective_n"
VOL = "synthetic_volume_per_energy"
#: U07 之后新增的**实测**增量曲线（SiC 单脉冲坑深，来源 Micromachines 15(5):573）。
#: 它让「曲线目录」不再只有 fixture —— 求解器终于能吃真实数据。
MEASURED_SIC = "sic4h_measured_single_pulse_crater"

FTH_J_CM2 = 1.0
DELTA_M = 1.0e-7


def card(curve_id: str) -> Path:
    return CURVES / f"{curve_id}.curve.json"


@pytest.fixture(scope="module")
def ysz_curve() -> T.ResponseCurve:
    return T.load_curve(card(FIXTURE))


@pytest.fixture(scope="module")
def sic_curve() -> T.ResponseCurve:
    return T.load_curve(card(SIC))


@pytest.fixture(scope="module")
def vol_curve() -> T.ResponseCurve:
    return T.load_curve(card(VOL))


# ---------------------------------------------------------------------------
# 1. schema 校验
# ---------------------------------------------------------------------------


def test_all_example_curves_load():
    curves = T.load_curves(CURVES)
    assert {c.curve_id for c in curves} == {FIXTURE, SIC, VOL, MEASURED_SIC}


def test_required_fields_match_spec():
    # 执行细则 7 节列出的最少字段必须都在必需集合里
    for f in (
        "curve_id", "material_id", "material_identity", "x_quantity", "y_quantity",
        "output_semantics", "fixed_conditions", "protocol",
        "source_figure_or_table", "valid_range", "points_file",
    ):
        assert f in T.REQUIRED_CURVE_FIELDS, f


@pytest.mark.parametrize(
    "missing",
    ["curve_id", "material_identity", "x_quantity", "y_quantity",
     "output_semantics", "fixed_conditions", "protocol",
     "source_figure_or_table", "valid_range", "points_file"],
)
def test_missing_required_field_rejected(tmp_path, ysz_curve, missing):
    raw = json.loads(card(FIXTURE).read_text(encoding="utf-8"))
    raw.pop(missing)
    (tmp_path / "c.curve.json").write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / f"{FIXTURE}.points.csv").write_text(
        (CURVES / f"{FIXTURE}.points.csv").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(UFDemoError) as ei:
        T.load_curve(tmp_path / "c.curve.json")
    assert ei.value.code == CONFIG_INVALID
    assert missing in (ei.value.field_path or "")


def test_semantics_enum_not_expanded(ysz_curve):
    raw = json.loads(card(FIXTURE).read_text(encoding="utf-8"))
    raw["output_semantics"] = "lookup_curve"
    with pytest.raises(UFDemoError) as ei:
        _load_from_dict(raw)
    assert ei.value.code == CONFIG_INVALID
    assert "未登记" in ei.value.message


def test_empty_unit_rejected():
    raw = json.loads(card(FIXTURE).read_text(encoding="utf-8"))
    raw["y_quantity"]["unit"] = "   "
    with pytest.raises(UFDemoError) as ei:
        _load_from_dict(raw)
    assert ei.value.code == CONFIG_INVALID
    assert "unit" in (ei.value.field_path or "")


def test_valid_range_must_be_two_ordered_numbers():
    raw = json.loads(card(FIXTURE).read_text(encoding="utf-8"))
    raw["valid_range"]["x"] = [40.0, 1.0]
    with pytest.raises(UFDemoError) as ei:
        _load_from_dict(raw)
    assert ei.value.code == CONFIG_INVALID


def test_valid_range_must_cover_all_points():
    raw = json.loads(card(FIXTURE).read_text(encoding="utf-8"))
    raw["valid_range"]["x"] = [1.0, 20.0]  # 数据里有 40
    with pytest.raises(UFDemoError) as ei:
        _load_from_dict(raw)
    assert ei.value.code == CONFIG_INVALID
    assert "有效区间" in ei.value.message


def test_event_increment_curve_requires_depth_direction():
    raw = json.loads(card(FIXTURE).read_text(encoding="utf-8"))
    raw.pop("depth_direction")
    with pytest.raises(UFDemoError) as ei:
        _load_from_dict(raw)
    assert ei.value.code == CONFIG_INVALID
    assert "depth_direction" in (ei.value.field_path or "")


def test_volume_curve_must_not_declare_depth_direction():
    raw = json.loads(card(VOL).read_text(encoding="utf-8"))
    raw["depth_direction"] = "surface_normal"
    with pytest.raises(UFDemoError) as ei:
        _load_from_dict(raw)
    assert ei.value.code == CONFIG_INVALID
    assert "depth_direction" in (ei.value.field_path or "")


def _load_from_dict(raw: dict) -> T.ResponseCurve:
    """把改写后的卡片写到临时文件再加载（点文件按原路径解析）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # 点文件与卡片同目录：复制一份
        src = CURVES / f"{raw.get('curve_id', FIXTURE)}.points.csv"
        (d / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        # 卡片的 curve_id 可能被改坏，单独指定 points_file 指向复制品
        raw = dict(raw)
        raw["points_file"] = src.name
        p = d / "tmp.curve.json"
        p.write_text(json.dumps(raw), encoding="utf-8")
        return T.load_curve(p)


# ---------------------------------------------------------------------------
# 2. CSV 解析与重复 x 处理
# ---------------------------------------------------------------------------


def test_csv_comments_and_blank_lines_allowed(tmp_path):
    p = tmp_path / "ok.points.csv"
    p.write_text("# 注释\n\nx,y\n1.0,0.0\n2.0,1.0\n# 中间注释\n3.0,2.0\n", encoding="utf-8")
    raw, rep = T.load_curve_points(p)
    assert raw == [(1.0, 0.0), (2.0, 1.0), (3.0, 2.0)]
    assert rep.applied is False
    assert rep.policy == "reject"


def test_csv_header_must_be_xy(tmp_path):
    p = tmp_path / "bad.points.csv"
    p.write_text("fluence,depth\n1.0,0.0\n2.0,1.0\n", encoding="utf-8")
    with pytest.raises(UFDemoError) as ei:
        T.load_curve_points(p)
    assert ei.value.code == CONFIG_INVALID
    assert "表头" in ei.value.message


def test_csv_requires_ascending_x(tmp_path):
    p = tmp_path / "unsorted.points.csv"
    p.write_text("x,y\n1.0,0.0\n5.0,1.0\n3.0,0.5\n", encoding="utf-8")
    with pytest.raises(UFDemoError) as ei:
        T.load_curve_points(p)
    assert ei.value.code == CONFIG_INVALID
    assert "升序" in ei.value.message


def test_csv_requires_two_points(tmp_path):
    p = tmp_path / "one.points.csv"
    p.write_text("x,y\n1.0,0.0\n", encoding="utf-8")
    with pytest.raises(UFDemoError) as ei:
        T.load_curve_points(p)
    assert ei.value.code == CONFIG_INVALID


def test_csv_rejects_nonfinite(tmp_path):
    p = tmp_path / "nan.points.csv"
    p.write_text("x,y\n1.0,0.0\n2.0,nan\n3.0,1.0\n", encoding="utf-8")
    with pytest.raises(UFDemoError) as ei:
        T.load_curve_points(p)
    assert ei.value.code == NUMERIC_NONFINITE


def test_csv_rejects_non_numeric(tmp_path):
    p = tmp_path / "bad.points.csv"
    p.write_text("x,y\n1.0,0.0\nabc,1.0\n", encoding="utf-8")
    with pytest.raises(UFDemoError) as ei:
        T.load_curve_points(p)
    assert ei.value.code == CONFIG_INVALID


def test_duplicate_x_rejected_by_default(tmp_path):
    p = tmp_path / "dup.points.csv"
    p.write_text("x,y\n1.0,0.0\n2.0,1.0\n2.0,1.4\n5.0,2.0\n", encoding="utf-8")
    with pytest.raises(UFDemoError) as ei:
        T.load_curve_points(p)
    assert ei.value.code == CONFIG_INVALID
    assert "重复横坐标" in ei.value.message
    # 提示里必须点明「不得静默删除/静默平均」
    assert "静默" in (ei.value.requirement or "")


def test_duplicate_policy_requires_rule_note(tmp_path):
    p = tmp_path / "dup.points.csv"
    p.write_text("x,y\n1.0,0.0\n2.0,1.0\n2.0,1.4\n5.0,2.0\n", encoding="utf-8")
    with pytest.raises(UFDemoError) as ei:
        T.load_curve_points(p, duplicate_policy="mean")
    assert ei.value.code == CONFIG_INVALID
    assert "重复试验处理规则" in ei.value.message


@pytest.mark.parametrize(
    "policy,expected",
    [("mean", 1.2), ("first", 1.0), ("last", 1.4)],
)
def test_duplicate_policies_aggregate_and_keep_raw(tmp_path, policy, expected):
    p = tmp_path / "dup.points.csv"
    p.write_text("x,y\n1.0,0.0\n2.0,1.0\n2.0,1.4\n5.0,2.0\n", encoding="utf-8")
    raw, rep = T.load_curve_points(
        p, duplicate_policy=policy, duplicate_rule_note="重复试验按声明规则合并"
    )
    assert raw == [(1.0, 0.0), (2.0, 1.0), (2.0, 1.4), (5.0, 2.0)], "原始点必须全量保留"
    assert rep.applied is True
    assert rep.merged_count == 1
    assert rep.duplicate_x == [2.0]
    assert rep.raw_point_count == 4
    assert rep.unique_point_count == 3
    assert "重复试验按声明规则合并" in rep.rule_note
    # 合并结果
    merged = T._dedup(raw, policy)
    assert merged[1][1] == pytest.approx(expected)


def test_duplicate_policy_invalid_value(tmp_path):
    p = tmp_path / "dup.points.csv"
    p.write_text("x,y\n1.0,0.0\n2.0,1.0\n", encoding="utf-8")
    with pytest.raises(UFDemoError) as ei:
        T.load_curve_points(p, duplicate_policy="average")
    assert ei.value.code == CONFIG_INVALID


def test_example_curves_preserve_raw_points(ysz_curve):
    assert ysz_curve.raw_points == ysz_curve.points
    assert len(ysz_curve.raw_points) == 9
    assert ysz_curve.duplicate_report.applied is False


# ---------------------------------------------------------------------------
# 3. 分段线性插值
# ---------------------------------------------------------------------------


def test_linear_is_exact_at_nodes(ysz_curve):
    """节点处线性插值必须精确复现原始点（这是数值实现验证的核心断言）。"""
    for x, y in ysz_curve.points:
        got = T.lookup(ysz_curve, x).scalar_value
        assert got == pytest.approx(y, rel=0.0, abs=0.0), f"x={x}"


def test_linear_matches_analytic_formula_at_nodes(ysz_curve):
    """与解析式 a = delta*ln(F/Fth) 逐点对照。"""
    for x, _ in ysz_curve.points:
        expect = DELTA_M * max(math.log(x / FTH_J_CM2), 0.0)
        got = T.lookup(ysz_curve, x).scalar_value
        assert got == pytest.approx(expect, rel=1e-15, abs=1e-24)


def test_linear_endpoint_e_squared_is_200nm(ysz_curve):
    got = T.lookup(ysz_curve, math.exp(2.0)).scalar_value
    assert got == pytest.approx(2 * DELTA_M, rel=0.0, abs=0.0)


def test_linear_midpoint_is_between_neighbours(ysz_curve):
    got = T.lookup(ysz_curve, 1.25).scalar_value
    lo = T.lookup(ysz_curve, 1.0).scalar_value
    hi = T.lookup(ysz_curve, 1.5).scalar_value
    assert lo < got < hi


def test_linear_lies_between_the_two_analytic_nodes(ysz_curve):
    """分段线性在相邻节点之间是弦，解析对数式是凹函数 → 弦在下方。"""
    x = 2.5
    lin = T.lookup(ysz_curve, x).scalar_value
    analytic = DELTA_M * math.log(x / FTH_J_CM2)
    assert lin < analytic


def test_linear_vector_and_scalar_agree(ysz_curve):
    xs = [1.5, 5.0, 20.0]
    vec = T.lookup(ysz_curve, xs)
    assert vec.values == [T.lookup(ysz_curve, x).scalar_value for x in xs]


def test_lookup_rejects_empty_and_nonfinite_queries(ysz_curve):
    with pytest.raises(UFDemoError) as ei:
        T.lookup(ysz_curve, [])
    assert ei.value.code == CONFIG_INVALID
    with pytest.raises(UFDemoError) as ei:
        T.lookup(ysz_curve, float("nan"))
    assert ei.value.code == NUMERIC_NONFINITE


def test_unknown_method_rejected(ysz_curve):
    with pytest.raises(UFDemoError) as ei:
        T.lookup(ysz_curve, 5.0, method="cubic")
    assert ei.value.code == CONFIG_INVALID


# ---------------------------------------------------------------------------
# 4. PCHIP
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not T.PCHIP_AVAILABLE, reason="需要 scipy")
def test_pchip_exact_at_nodes(ysz_curve):
    for x, y in ysz_curve.points:
        got = T.lookup(ysz_curve, x, method="pchip").scalar_value
        assert got == pytest.approx(y, rel=1e-12, abs=1e-20)


@pytest.mark.skipif(not T.PCHIP_AVAILABLE, reason="需要 scipy")
def test_pchip_is_shape_preserving_no_overshoot(ysz_curve):
    """保形性：PCHIP 不得越过相邻节点构成的界。"""
    xs = [1.5 + 0.05 * i for i in range(20)]
    vals = T.lookup(ysz_curve, xs, method="pchip").values
    px, py = ysz_curve.xs, ysz_curve.ys
    for x, v in zip(xs, vals):
        # 找包含 x 的区间
        for i in range(len(px) - 1):
            if px[i] <= x <= px[i + 1]:
                lo, hi = sorted((py[i], py[i + 1]))
                assert lo - 1e-18 <= v <= hi + 1e-18, f"x={x} 越界 {v}"
                break


@pytest.mark.skipif(not T.PCHIP_AVAILABLE, reason="需要 scipy")
def test_pchip_closer_to_analytic_than_linear(ysz_curve):
    """平滑真实关系下 PCHIP 应优于分段线性（这里是解析对数式）。"""
    xs = [1.75, 2.5, 7.0, 15.0]
    for x in xs:
        truth = DELTA_M * math.log(x / FTH_J_CM2)
        lin = abs(T.lookup(ysz_curve, x).scalar_value - truth)
        pch = abs(T.lookup(ysz_curve, x, method="pchip").scalar_value - truth)
        assert pch <= lin, f"x={x}: pchip {pch} > linear {lin}"


def test_pchip_without_scipy_raises_not_implemented(ysz_curve, monkeypatch):
    """缺 SciPy 时必须显式报错，**不得**静默退化为线性。"""
    monkeypatch.setattr(T, "PCHIP_AVAILABLE", False)
    with pytest.raises(UFDemoError) as ei:
        T.lookup(ysz_curve, 5.0, method="pchip")
    assert ei.value.code == NOT_IMPLEMENTED
    assert "scipy" in (ei.value.requirement or "").lower()


# ---------------------------------------------------------------------------
# 5. 越界：不返回 0、不外推、不钳端点
# ---------------------------------------------------------------------------


def test_below_range_raises_out_of_range(ysz_curve):
    with pytest.raises(UFDemoError) as ei:
        T.lookup(ysz_curve, 0.5)
    assert ei.value.code == TABLE_OUT_OF_RANGE
    assert "低于" in ei.value.message


def test_above_range_raises_out_of_range(ysz_curve):
    with pytest.raises(UFDemoError) as ei:
        T.lookup(ysz_curve, 1000.0)
    assert ei.value.code == TABLE_OUT_OF_RANGE
    assert "高于" in ei.value.message


def test_out_of_range_never_returns_zero_and_never_clamps(ysz_curve):
    """核心红线：低于量测区间**不自动返回零**，也不钳到端点值。"""
    res = T.lookup(ysz_curve, [0.2, 0.5, 1.0, 40.0, 200.0], allow_out_of_range=True)
    assert res.status == "mixed_out_of_range"
    assert res.values[0] is None, "低于区间不得返回 0"
    assert res.values[1] is None
    assert res.values[2] == 0.0, "x=1.0 的 0 来自曲线自带的阈值律（数据点），不是越界填零"
    assert res.values[4] is None, "高于区间不得外推，也不得钳到右端点"
    assert res.in_range == [False, False, True, True, False]


def test_out_of_range_report_is_machine_readable(ysz_curve):
    res = T.lookup(ysz_curve, 0.6, allow_out_of_range=True)
    assert res.status == "below_range"
    assert res.ok is False
    d = res.to_dict()
    assert d["values"] == [None]
    assert "不返回 0" in d["reason"]


def test_out_of_range_below_reason_explains_no_auto_zero(ysz_curve):
    with pytest.raises(UFDemoError) as ei:
        T.lookup(ysz_curve, 0.5)
    assert "不自动返回 0" in (ei.value.suggestion or "")
    assert "无去除" in (ei.value.suggestion or "")


def test_endpoints_are_inclusive(ysz_curve):
    lo, hi = ysz_curve.valid_range
    assert T.lookup(ysz_curve, lo).ok
    assert T.lookup(ysz_curve, hi).ok


# ---------------------------------------------------------------------------
# 6. 语义路由与两条红线
# ---------------------------------------------------------------------------


def test_routes(ysz_curve, sic_curve, vol_curve):
    assert ysz_curve.route == T.ROUTE_EVENT_KERNEL
    assert sic_curve.route == T.ROUTE_EVALUATOR
    assert vol_curve.route == T.ROUTE_EVALUATOR


def test_routes_cover_every_semantic():
    from ufdemo.config import ALL_SEMANTICS

    for s in ALL_SEMANTICS:
        assert s in T.CURVE_ROUTES, f"语义 {s} 没有登记去向"


def test_only_event_increment_curve_can_enter_event_kernel(ysz_curve, sic_curve, vol_curve):
    T.assert_curve_can_enter_event_kernel(ysz_curve)  # 不抛
    for c in (sic_curve, vol_curve):
        with pytest.raises(UFDemoError) as ei:
            T.assert_curve_can_enter_event_kernel(c)
        assert ei.value.code == RESPONSE_SEMANTICS_INVALID


def test_condition_mismatch_rejects_event_kernel(ysz_curve):
    # 曲线固定 f=1000 Hz；请求 200 kHz 应拒绝
    with pytest.raises(UFDemoError) as ei:
        T.assert_curve_can_enter_event_kernel(ysz_curve, laser={"repetition_rate_Hz": 200000.0})
    assert ei.value.code == CONDITION_MISMATCH


def test_condition_match_accepts_matching_laser(ysz_curve):
    T.assert_curve_can_enter_event_kernel(ysz_curve, laser={"repetition_rate_Hz": 1000.0})


def test_undefined_condition_is_skipped_not_guessed(ysz_curve):
    # 解析 fixture 的 wavelength 为 null → 不应被当成 0 或猜测值
    T.assert_curve_can_enter_event_kernel(ysz_curve, laser={"wavelength_m": 1.03e-6})


def test_volume_curve_cannot_derive_local_depth(vol_curve):
    for q in ("removal_depth", "removal_depth_per_pulse", "local_depth", "depth_profile"):
        with pytest.raises(UFDemoError) as ei:
            T.assert_no_local_depth_from_volume(vol_curve, q)
        assert ei.value.code == CONFIG_INVALID
        assert "只进评估器" in (ei.value.requirement or "")


def test_volume_curve_message_mentions_non_uniqueness(vol_curve):
    with pytest.raises(UFDemoError) as ei:
        T.assert_no_local_depth_from_volume(vol_curve, "removal_depth")
    assert "不唯一" in (ei.value.suggestion or "")


def test_volume_curve_allows_volume_derivation(vol_curve):
    T.assert_no_local_depth_from_volume(vol_curve, "removal_volume")  # 不抛


def test_event_curve_allows_depth_derivation(ysz_curve):
    T.assert_no_local_depth_from_volume(ysz_curve, "removal_depth")  # 不抛


def test_derivable_quantities(ysz_curve, sic_curve, vol_curve):
    assert "removal_depth_per_pulse" in T.derivable_quantities(ysz_curve)
    assert "removal_depth_per_pulse" not in T.derivable_quantities(vol_curve)
    assert "threshold_fluence" in T.derivable_quantities(sic_curve)


def test_lookup_notes_state_route_and_no_local_depth(vol_curve):
    res = T.lookup(vol_curve, 1.0, allow_out_of_range=True)
    assert res.event_kernel_allowed is False
    assert any("不得进入逐事件核" in n for n in res.notes)


# ---------------------------------------------------------------------------
# 7. 与其余模块的一致性
# ---------------------------------------------------------------------------


def test_sic_threshold_curve_matches_reference_evaluator(sic_curve):
    """SiC 阈值曲线由卡内拟合参数重算，应与 references 的实现逐点一致。"""
    for n, y_j_cm2 in sic_curve.points:
        got = REF.sic_threshold_fluence(
            n, f1_J_m2=23500.0, f_inf_J_m2=7000.0, k_per_pulse=0.0199
        ) / 1e4
        assert got == pytest.approx(y_j_cm2, rel=1e-14), f"N={n}"


def test_sic_threshold_curve_endpoints(sic_curve):
    assert T.lookup(sic_curve, 1.0).scalar_value == pytest.approx(2.35, rel=1e-15)
    # N=720 时应接近 F_inf=0.70
    v720 = T.lookup(sic_curve, 720.0).scalar_value
    assert 0.70 < v720 < 0.71


def test_interpolate_grid_returns_raw_and_interpolated(ysz_curve):
    grid = T.interpolate_grid(ysz_curve, n=50)
    assert len(grid["x"]) == 50
    assert len(grid["y"]) == 50
    assert grid["raw_x"] == [p[0] for p in ysz_curve.raw_points]
    assert grid["raw_y"] == [p[1] for p in ysz_curve.raw_points]
    assert grid["x"][0] == ysz_curve.valid_range[0]
    assert grid["x"][-1] == ysz_curve.valid_range[1]


def test_interpolate_grid_small_n_rejected(ysz_curve):
    with pytest.raises(UFDemoError):
        T.interpolate_grid(ysz_curve, n=1)


def test_curve_to_dict_roundtrip_fields(ysz_curve):
    d = ysz_curve.to_dict()
    for k in ("curve_id", "material_id", "output_semantics", "route",
              "valid_range", "duplicate_report", "points", "raw_points",
              "point_count", "source_figure_or_table"):
        assert k in d
    assert d["point_count"] == len(ysz_curve.points)


def test_load_curve_missing_file():
    with pytest.raises(UFDemoError) as ei:
        T.load_curve(CURVES / "nope.curve.json")
    assert ei.value.code == CONFIG_INVALID


def test_load_curve_bad_json(tmp_path):
    p = tmp_path / "bad.curve.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(UFDemoError) as ei:
        T.load_curve(p)
    assert ei.value.code == CONFIG_INVALID


def test_points_file_missing(tmp_path):
    raw = json.loads(card(FIXTURE).read_text(encoding="utf-8"))
    raw["points_file"] = "does_not_exist.points.csv"
    p = tmp_path / "c.curve.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(UFDemoError) as ei:
        T.load_curve(p)
    assert ei.value.code == CONFIG_INVALID
    assert "不存在" in ei.value.message


def test_iter_curve_cards_sorted_and_complete():
    cards = T.iter_curve_cards(CURVES)
    # 3 张 fixture（公式重算 / 人工解析 / 合成）+ 1 张实测
    assert len(cards) == 4
    assert [p.name for p in cards] == sorted(p.name for p in cards)


def test_iter_curve_cards_missing_dir_returns_empty(tmp_path):
    assert T.iter_curve_cards(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# 8. 无效夹具与 CLI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,code",
    [
        ("invalid_unsorted", CONFIG_INVALID),
        ("invalid_duplicate", CONFIG_INVALID),
        ("invalid_duplicate_no_rule", CONFIG_INVALID),
        ("invalid_nonfinite", NUMERIC_NONFINITE),
        ("invalid_single_point", CONFIG_INVALID),
        ("invalid_bad_header", CONFIG_INVALID),
        ("invalid_nonnumeric", CONFIG_INVALID),
        ("invalid_range_narrow", CONFIG_INVALID),
        ("invalid_volume_depth_dir", CONFIG_INVALID),
        ("invalid_missing_field", CONFIG_INVALID),
        ("invalid_empty", CONFIG_INVALID),
        ("invalid_bad_semantics", CONFIG_INVALID),
        ("invalid_no_depth_direction", CONFIG_INVALID),
        ("invalid_no_unit", CONFIG_INVALID),
    ],
)
def test_invalid_fixtures_rejected_with_expected_code(name, code):
    with pytest.raises(UFDemoError) as ei:
        T.load_curve(INVALID / f"{name}.curve.json")
    assert ei.value.code == code, f"{name}: 期望 {code}，实际 {ei.value.code}"


def test_cli_table_ok(capsys):
    from ufdemo.__main__ import main

    rc = main(["table", str(card(FIXTURE)), "--x", "5.0"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "analytic_fixture_depth_vs_fluence" in out
    assert "1.6094379124341003e-07" in out


def test_cli_table_out_of_range_exit_code(capsys):
    from ufdemo.__main__ import main

    rc = main(["table", str(card(FIXTURE)), "--x", "0.5"])
    assert rc == 1
    err = capsys.readouterr()
    assert "TABLE_OUT_OF_RANGE" in (err.out + err.err)


def test_cli_table_allow_out_of_range_json(capsys):
    from ufdemo.__main__ import main

    rc = main(["table", str(card(FIXTURE)), "--x", "0.5", "5.0", "--allow-out-of-range", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "below_range"
    assert payload["values"][0] is None
    assert payload["values"][1] == pytest.approx(1.6094379124341003e-07)


def test_cli_table_pchip(capsys):
    from ufdemo.__main__ import main

    rc = main(["table", str(card(FIXTURE)), "--x", "2.5", "--method", "pchip", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["method"] == "pchip"
    assert payload["values"][0] > 0


def test_cli_table_all_curves(capsys):
    from ufdemo.__main__ import main

    rc = main(["table", str(CURVES), "--all-curves", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 4   # 3 fixture + 1 measured


def test_cli_table_defaults_to_range_ends(capsys):
    from ufdemo.__main__ import main

    rc = main(["table", str(card(VOL)), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["x"] == [0.35, 8.0]
