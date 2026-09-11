"""查表：曲线 schema、插值核与越界处理（批次 F / T10）。

执行细则第 6 节把批次 F 定义为「曲线 schema；线性插值；可选 PCHIP；越界处理」，
必交「示例 CSV、错误 CSV、原始点与插值图」；第 7 节给出最少字段与两条红线：

1. **查表不是任意外推**（任务书 3.5）：先支持固定材料、波长、脉宽和历史协议下的
   一维响应曲线；默认分段线性，需要平滑时用保形 PCHIP。SciPy 的 PCHIP 默认可
   外推，因此**显式设置 ``extrapolate=False``**，并在业务层返回越界状态。
2. **越界返回状态和原因；低于量测区间不自动返回零**（细则 7 节末）。
   ``TABLE_OUT_OF_RANGE`` 是唯一越界错误码，本模块**绝不**把越界悄悄钳到端点或填 0。
3. **曲线横坐标严格排序且无重复**；重复 x **不静默删除**，需要另附重复试验处理
   规则并保留原始点（任务书 G09）。
4. **只有 ``event_depth_increment`` 曲线且协议确实适用时才能进入事件核**；
   平均/累计/体积曲线进入评估器。仅有终态体积/去除效率时**没有额外形状假设，
   不能唯一反推每个位置的去除深度**（任务书 3.5）。

本模块只做查表与语义路由，**不接入逐事件主循环**（那是批次 H 的 T14「查表导入」
工作）；接入前必须先通过 :func:`assert_curve_can_enter_event_kernel`。

文件布局
--------

一条曲线由两个文件描述（与材料卡的「JSON 元数据 + 数据文件」约定一致）：

* ``<curve_id>.curve.json`` —— 元数据（本模块校验的最少字段）；
* ``<curve_id>.points.csv`` —— **原始数据点**（两列 ``x,y``，允许 ``#`` 注释行）。

CSV 是原始点的唯一来源，加载后原样保存在 :attr:`ResponseCurve.raw_points`
（含重复 x），去重后的可用点保存在 :attr:`ResponseCurve.points`。
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import (
    ALL_SEMANTICS,
    SEMANTIC_CUMULATIVE,
    SEMANTIC_EVENT_INCREMENT,
    SEMANTIC_MEAN_RATE,
    SEMANTIC_THRESHOLD_ONLY,
    SEMANTIC_TRACK_PASS,
    SEMANTIC_VOLUME_PER_ENERGY,
)
from .errors import (
    CONFIG_INVALID,
    CONDITION_MISMATCH,
    NOT_IMPLEMENTED,
    NUMERIC_NONFINITE,
    RESPONSE_SEMANTICS_INVALID,
    TABLE_OUT_OF_RANGE,
    UFDemoError,
)

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

CURVE_SCHEMA_VERSION = "1.0"

# 执行细则 7 节的「查表最少字段」。
REQUIRED_CURVE_FIELDS: tuple[str, ...] = (
    "schema_version",
    "curve_id",
    "material_id",
    "material_identity",
    "x_quantity",
    "y_quantity",
    "output_semantics",
    "fixed_conditions",
    "protocol",
    "source_figure_or_table",
    "valid_range",
    "points_file",
)

QUANTITY_KINDS = ("scalar", "count", "fluence", "energy", "volume", "depth", "rate", "threshold")

# 重复 x 的处理策略。默认 **拒绝**：宁可报错，也不静默删除或静默平均。
DUPLICATE_POLICIES: tuple[str, ...] = ("reject", "mean", "first", "last")

INTERPOLATION_METHODS: tuple[str, ...] = ("linear", "pchip")

# 曲线去向（细则 7 节：只有 event_depth_increment 能进事件核）。
ROUTE_EVENT_KERNEL = "event_kernel"
ROUTE_EVALUATOR = "evaluator"

CURVE_ROUTES: dict[str, str] = {
    SEMANTIC_EVENT_INCREMENT: ROUTE_EVENT_KERNEL,
    SEMANTIC_MEAN_RATE: ROUTE_EVALUATOR,
    SEMANTIC_CUMULATIVE: ROUTE_EVALUATOR,
    SEMANTIC_TRACK_PASS: ROUTE_EVALUATOR,
    SEMANTIC_VOLUME_PER_ENERGY: ROUTE_EVALUATOR,
    SEMANTIC_THRESHOLD_ONLY: ROUTE_EVALUATOR,
}

ROUTE_ZH: dict[str, str] = {
    ROUTE_EVENT_KERNEL: "逐事件核（需协议适用）",
    ROUTE_EVALUATOR: "评估器（不得生成局部形貌）",
}

# 「局部去除深度」类观测量：只有逐事件增量语义的曲线才允许据此产生局部形貌。
LOCAL_DEPTH_QUANTITIES: tuple[str, ...] = (
    "removal_depth",
    "removal_depth_per_pulse",
    "local_depth",
    "depth",
    "depth_per_pulse",
    "center_depth",
    "depth_profile",
)

# 越界判定用的相对容差：允许正好落在端点的查询。
_RANGE_REL_TOL = 1e-12

try:  # pragma: no cover - 取决于环境
    from scipy.interpolate import PchipInterpolator  # type: ignore

    PCHIP_AVAILABLE = True
except ImportError:  # pragma: no cover
    PchipInterpolator = None  # type: ignore
    PCHIP_AVAILABLE = False


# ---------------------------------------------------------------------------
# 结果类型
# ---------------------------------------------------------------------------


@dataclass
class DuplicateReport:
    """重复 x 的处理记录（任务书 G09：不静默删除，须附规则与原始点）。"""

    policy: str
    rule_note: str
    duplicate_x: list[float] = field(default_factory=list)
    merged_count: int = 0
    raw_point_count: int = 0
    unique_point_count: int = 0
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "rule_note": self.rule_note,
            "duplicate_x": self.duplicate_x,
            "merged_count": self.merged_count,
            "raw_point_count": self.raw_point_count,
            "unique_point_count": self.unique_point_count,
            "applied": self.applied,
        }


@dataclass
class ResponseCurve:
    """一条已校验的一维响应曲线。

    ``raw_points`` 是 CSV 的原始内容（含重复 x）；``points`` 是按重复策略处理后的
    可用于插值的点。两者都随对象保留，供报告与界面同时展示。
    """

    curve_id: str
    material_id: str
    material_identity: dict[str, Any]
    x_quantity: dict[str, Any]
    y_quantity: dict[str, Any]
    output_semantics: str
    fixed_conditions: dict[str, Any]
    protocol: dict[str, Any]
    source_figure_or_table: str
    valid_range: tuple[float, float]
    valid_range_note: str
    points: list[tuple[float, float]]
    raw_points: list[tuple[float, float]]
    duplicate_report: DuplicateReport
    source_path: str = ""
    points_path: str = ""
    evidence_status: str = "unverified"
    source_type: str = ""
    depth_direction: str | None = None
    fluence_basis: str | None = None
    notes: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)

    # -- 便捷属性 -----------------------------------------------------------
    @property
    def xs(self) -> list[float]:
        return [p[0] for p in self.points]

    @property
    def ys(self) -> list[float]:
        return [p[1] for p in self.points]

    @property
    def route(self) -> str:
        return CURVE_ROUTES[self.output_semantics]

    @property
    def route_zh(self) -> str:
        return ROUTE_ZH[self.route]

    @property
    def x_unit(self) -> str:
        return str(self.x_quantity.get("unit", ""))

    @property
    def y_unit(self) -> str:
        return str(self.y_quantity.get("unit", ""))

    @property
    def can_enter_event_kernel(self) -> bool:
        """只是**语义**条件；协议适用性还要另外确认。"""
        return self.output_semantics == SEMANTIC_EVENT_INCREMENT

    def to_dict(self, *, include_points: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": CURVE_SCHEMA_VERSION,
            "curve_id": self.curve_id,
            "material_id": self.material_id,
            "material_identity": self.material_identity,
            "x_quantity": self.x_quantity,
            "y_quantity": self.y_quantity,
            "output_semantics": self.output_semantics,
            "route": self.route,
            "route_zh": self.route_zh,
            "can_enter_event_kernel": self.can_enter_event_kernel,
            "fixed_conditions": self.fixed_conditions,
            "protocol": self.protocol,
            "source_figure_or_table": self.source_figure_or_table,
            "valid_range": {"x": list(self.valid_range), "note": self.valid_range_note},
            "point_count": len(self.points),
            "raw_point_count": len(self.raw_points),
            "duplicate_report": self.duplicate_report.to_dict(),
            "evidence_status": self.evidence_status,
            "source_type": self.source_type,
            "depth_direction": self.depth_direction,
            "fluence_basis": self.fluence_basis,
            "source_path": self.source_path,
            "points_path": self.points_path,
            "notes": list(self.notes),
            "limitations": list(self.limitations),
            "source_ids": list(self.source_ids),
        }
        if include_points:
            out["points"] = [list(p) for p in self.points]
            out["raw_points"] = [list(p) for p in self.raw_points]
        return out


@dataclass
class TableLookup:
    """一次查表结果。**越界时 ``values`` 为 ``None``，绝不填 0。**"""

    curve_id: str
    method: str
    x: list[float]
    values: list[float | None]
    in_range: list[bool]
    status: str                 # ok | below_range | above_range | mixed_out_of_range
    reason: str
    valid_range: tuple[float, float]
    output_semantics: str
    route: str
    event_kernel_allowed: bool
    x_quantity: str
    y_quantity: str
    x_unit: str
    y_unit: str
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def scalar_value(self) -> float | None:
        return self.values[0] if len(self.values) == 1 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "curve_id": self.curve_id,
            "method": self.method,
            "x": self.x,
            "values": self.values,
            "in_range": self.in_range,
            "status": self.status,
            "reason": self.reason,
            "valid_range": list(self.valid_range),
            "output_semantics": self.output_semantics,
            "route": self.route,
            "event_kernel_allowed": self.event_kernel_allowed,
            "x_quantity": self.x_quantity,
            "y_quantity": self.y_quantity,
            "x_unit": self.x_unit,
            "y_unit": self.y_unit,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 加载与校验
# ---------------------------------------------------------------------------


def _require(obj: Mapping[str, Any], key: str, *, where: str) -> Any:
    if key not in obj or obj[key] is None:
        raise UFDemoError(
            CONFIG_INVALID,
            f"曲线缺少必填字段 {key!r}",
            field_path=f"{where}.{key}",
            actual=None,
            requirement=f"必须提供 {key!r}（执行细则 7 节查表最少字段）",
            suggestion="补齐该字段后重试；不要用空串或 0 代替缺失信息。",
        )
    return obj[key]


def _check_quantity(obj: Mapping[str, Any], *, where: str) -> dict[str, Any]:
    if not isinstance(obj, Mapping):
        raise UFDemoError(
            CONFIG_INVALID,
            f"{where} 必须是对象",
            field_path=where,
            actual=type(obj).__name__,
            requirement='形如 {"name": ..., "unit": ..., "label": ...}',
        )
    name = _require(obj, "name", where=where)
    unit = _require(obj, "unit", where=where)
    if not isinstance(name, str) or not name.strip():
        raise UFDemoError(
            CONFIG_INVALID, f"{where}.name 必须是非空字符串",
            field_path=f"{where}.name", actual=name, requirement="非空量名",
        )
    if not isinstance(unit, str) or not unit.strip():
        raise UFDemoError(
            CONFIG_INVALID, f"{where}.unit 必须是非空字符串（单位不可省略）",
            field_path=f"{where}.unit", actual=unit,
            requirement="非空单位串，例如 'J/cm^2'、'm'、'1'（无量纲写 '1'）",
        )
    kind = obj.get("kind")
    if kind is not None and kind not in QUANTITY_KINDS:
        raise UFDemoError(
            CONFIG_INVALID, f"{where}.kind 取值非法",
            field_path=f"{where}.kind", actual=kind,
            requirement=f"取值属于 {list(QUANTITY_KINDS)} 或省略",
        )
    return dict(obj)


def load_curve_points(
    csv_path: str | Path,
    *,
    duplicate_policy: str = "reject",
    duplicate_rule_note: str | None = None,
) -> tuple[list[tuple[float, float]], DuplicateReport]:
    """读取原始点 CSV。

    格式：允许 ``#`` 注释行与空行；第一个非注释、非空行必须是表头 ``x,y``；
    其后每行两个字段。表头**必须**是 ``x,y``——单位由曲线卡的
    ``x_quantity.unit`` / ``y_quantity.unit`` 定义，避免 CSV 与卡片各说各话。

    横坐标必须**严格升序**。重复 x 默认拒绝；若声明了策略，则必须同时给出
    ``duplicate_rule_note`` 说明重复试验的处理规则，否则同样拒绝
    （任务书 G09：重复 x 不静默删除）。
    """
    path = Path(csv_path)
    if not path.exists():
        raise UFDemoError(
            CONFIG_INVALID,
            "曲线点文件不存在",
            field_path="points_file",
            actual=str(path),
            requirement="points_file 必须在曲线卡所在目录存在",
            suggestion="检查文件名与相对路径。",
        )

    raw_rows: list[tuple[float, float]] = []
    header_seen = False
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for lineno, row in enumerate(csv.reader(fh), start=1):
            if not row:
                continue
            first = row[0].strip()
            if not first or first.startswith("#"):
                continue
            if not header_seen:
                joined = ",".join(c.strip().lower() for c in row[:2])
                if joined != "x,y":
                    raise UFDemoError(
                        CONFIG_INVALID,
                        "曲线点 CSV 表头必须是 x,y",
                        field_path=f"{path.name}:{lineno}",
                        actual=row,
                        requirement="第一行非注释内容为 'x,y'",
                        suggestion="单位写在曲线卡的 x_quantity/y_quantity 里，不要写进表头。",
                    )
                header_seen = True
                continue
            if len(row) < 2:
                raise UFDemoError(
                    CONFIG_INVALID,
                    "曲线点行字段数不足",
                    field_path=f"{path.name}:{lineno}",
                    actual=row,
                    requirement="每行两个字段：x,y",
                )
            try:
                x = float(row[0])
                y = float(row[1])
            except ValueError as exc:
                raise UFDemoError(
                    CONFIG_INVALID,
                    "曲线点无法解析为数值",
                    field_path=f"{path.name}:{lineno}",
                    actual=row,
                    requirement="两列均为十进制数值",
                    suggestion=f"修正该行（{exc}）。",
                ) from exc
            if not (math.isfinite(x) and math.isfinite(y)):
                raise UFDemoError(
                    NUMERIC_NONFINITE,
                    "曲线点出现非有限数值",
                    field_path=f"{path.name}:{lineno}",
                    actual=row,
                    requirement="x、y 均为有限值（不得为 nan/inf）",
                    suggestion="用 null 语义不适用于数值点列：缺失点应直接删除该行或另建曲线。",
                )
            raw_rows.append((x, y))

    if not header_seen:
        raise UFDemoError(
            CONFIG_INVALID,
            "曲线点 CSV 缺少表头",
            field_path=str(path),
            actual=None,
            requirement="包含表头行 'x,y'",
        )
    if len(raw_rows) < 2:
        raise UFDemoError(
            CONFIG_INVALID,
            "曲线至少需要 2 个点才能插值",
            field_path=str(path),
            actual=len(raw_rows),
            requirement=">= 2 个有限点",
        )

    # 严格升序（G09：横坐标严格排序且无重复）
    for i in range(1, len(raw_rows)):
        if raw_rows[i][0] < raw_rows[i - 1][0]:
            raise UFDemoError(
                CONFIG_INVALID,
                "曲线横坐标必须严格升序",
                field_path=f"{path.name}:第 {i + 1} 个数据点",
                actual=raw_rows[i][0],
                requirement="x 单调不减；本工具不会替你重排（避免与原始图表脱节）",
                suggestion="按原始图表的横坐标顺序整理数据后重试。",
            )

    if duplicate_policy not in DUPLICATE_POLICIES:
        raise UFDemoError(
            CONFIG_INVALID,
            "duplicate_policy 取值非法",
            field_path="duplicate_policy",
            actual=duplicate_policy,
            requirement=f"取值属于 {list(DUPLICATE_POLICIES)}",
        )

    # 分组统计重复 x
    groups: list[list[tuple[float, float]]] = []
    for pt in raw_rows:
        if groups and pt[0] == groups[-1][0][0]:
            groups[-1].append(pt)
        else:
            groups.append([pt])
    dup_xs = [g[0][0] for g in groups if len(g) > 1]
    merged = sum(len(g) - 1 for g in groups if len(g) > 1)

    if dup_xs and duplicate_policy == "reject":
        raise UFDemoError(
            CONFIG_INVALID,
            f"曲线存在重复横坐标（{len(dup_xs)} 组，共 {merged} 个重复点）",
            field_path="points_file",
            actual=dup_xs[:8],
            requirement=(
                "重复 x 不得静默删除或静默平均；必须在曲线卡声明 "
                "duplicate_policy 与 duplicate_rule_note 说明重复试验处理规则"
            ),
            suggestion=(
                "要么把重复点并入重复试验处理规则（设置 duplicate_policy + "
                "duplicate_rule_note），要么先按原始记录的规则合并后再入库。"
            ),
        )
    if dup_xs and not (duplicate_rule_note or "").strip():
        raise UFDemoError(
            CONFIG_INVALID,
            "声明了重复 x 处理策略但未给出重复试验处理规则",
            field_path="duplicate_rule_note",
            actual=duplicate_rule_note,
            requirement="duplicate_policy != 'reject' 时必须写明 duplicate_rule_note",
            suggestion="例如：'同一横坐标多次重复试验取算术平均，原始点另存 raw_points'。",
        )

    if duplicate_policy == "reject":
        points = list(raw_rows)
    else:
        points = []
        for g in groups:
            if len(g) == 1:
                points.append(g[0])
                continue
            ys = [p[1] for p in g]
            if duplicate_policy == "mean":
                y = sum(ys) / len(ys)
            elif duplicate_policy == "first":
                y = ys[0]
            else:  # last
                y = ys[-1]
            points.append((g[0][0], y))

    report = DuplicateReport(
        policy=duplicate_policy,
        rule_note=(duplicate_rule_note or "默认拒绝重复 x；本曲线无重复点。"),
        duplicate_x=list(dup_xs),
        merged_count=merged,
        raw_point_count=len(raw_rows),
        unique_point_count=len(points),
        applied=bool(dup_xs),
    )
    return raw_rows, report


def _resolve_relative(base: Path, ref: str) -> Path:
    cand = Path(ref)
    if cand.is_absolute():
        return cand
    return (base / cand).resolve()


def load_curve(card_path: str | Path) -> ResponseCurve:
    """加载并**严格校验**一张曲线卡（含其点 CSV）。"""
    path = Path(card_path)
    if not path.exists():
        raise UFDemoError(
            CONFIG_INVALID,
            "曲线卡不存在",
            field_path="curve",
            actual=str(path),
            requirement="传入 .curve.json 路径",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UFDemoError(
            CONFIG_INVALID,
            "曲线卡不是合法 JSON",
            field_path=str(path),
            actual=str(exc),
            requirement="UTF-8 编码的 JSON 对象",
        ) from exc
    if not isinstance(raw, Mapping):
        raise UFDemoError(
            CONFIG_INVALID, "曲线卡顶层必须是对象", field_path=str(path),
            actual=type(raw).__name__, requirement="JSON 对象",
        )

    for f in REQUIRED_CURVE_FIELDS:
        _require(raw, f, where="curve")

    if raw["schema_version"] != CURVE_SCHEMA_VERSION:
        raise UFDemoError(
            CONFIG_INVALID,
            "曲线 schema_version 不匹配",
            field_path="schema_version",
            actual=raw["schema_version"],
            requirement=f"== {CURVE_SCHEMA_VERSION}",
        )

    semantics = raw["output_semantics"]
    if semantics not in ALL_SEMANTICS:
        raise UFDemoError(
            CONFIG_INVALID,
            "曲线 output_semantics 未登记",
            field_path="output_semantics",
            actual=semantics,
            requirement=f"取值属于 {list(ALL_SEMANTICS)}（执行细则 4.3，枚举不扩容）",
            suggestion="不要为查表新造语义值；查表只是同一语义下的取值手段。",
        )

    x_quantity = _check_quantity(raw["x_quantity"], where="x_quantity")
    y_quantity = _check_quantity(raw["y_quantity"], where="y_quantity")

    if not isinstance(raw["fixed_conditions"], Mapping) or not raw["fixed_conditions"]:
        raise UFDemoError(
            CONFIG_INVALID,
            "fixed_conditions 必须是非空对象",
            field_path="fixed_conditions",
            actual=raw["fixed_conditions"],
            requirement="至少记录波长/脉宽/重复频率/环境等固定条件",
            suggestion="查表的适用性由固定条件决定；留空等于宣称无条件限制。",
        )
    if not isinstance(raw["protocol"], Mapping) or not raw["protocol"]:
        raise UFDemoError(
            CONFIG_INVALID,
            "protocol 必须是非空对象",
            field_path="protocol",
            actual=raw["protocol"],
            requirement="记录 protocol_id（及必要的历史/协议量）",
            suggestion="没有协议的曲线不得用于定量查表。",
        )
    if not str(raw["source_figure_or_table"]).strip():
        raise UFDemoError(
            CONFIG_INVALID, "source_figure_or_table 不能为空",
            field_path="source_figure_or_table", actual=raw["source_figure_or_table"],
            requirement="给出图号/表号，便于回溯原文",
        )

    # --- 有效区间 ---------------------------------------------------------
    vr = raw["valid_range"]
    if not isinstance(vr, Mapping) or "x" not in vr:
        raise UFDemoError(
            CONFIG_INVALID,
            "valid_range 必须包含 x 区间",
            field_path="valid_range",
            actual=vr,
            requirement='形如 {"x": [min, max], "note": "..."}',
        )
    xr = vr["x"]
    if not isinstance(xr, Sequence) or isinstance(xr, (str, bytes)) or len(xr) != 2:
        raise UFDemoError(
            CONFIG_INVALID,
            "valid_range.x 必须是两个数的数组 [min, max]",
            field_path="valid_range.x", actual=xr, requirement="[min, max]",
        )
    try:
        lo, hi = float(xr[0]), float(xr[1])
    except (TypeError, ValueError) as exc:
        raise UFDemoError(
            CONFIG_INVALID, "valid_range.x 必须是数值",
            field_path="valid_range.x", actual=xr, requirement="[min, max] 数值",
        ) from exc
    if not (math.isfinite(lo) and math.isfinite(hi)):
        raise UFDemoError(
            NUMERIC_NONFINITE, "valid_range.x 出现非有限数值",
            field_path="valid_range.x", actual=xr, requirement="有限数值",
        )
    if not lo < hi:
        raise UFDemoError(
            CONFIG_INVALID, "valid_range.x 必须严格递增",
            field_path="valid_range.x", actual=xr, requirement="min < max",
        )

    # --- 原始点 -----------------------------------------------------------
    base = path.parent
    points_path = _resolve_relative(base, str(raw["points_file"]))
    policy = str(raw.get("duplicate_policy", "reject"))
    rule_note = raw.get("duplicate_rule_note")
    raw_points, dup_report = load_curve_points(
        points_path, duplicate_policy=policy, duplicate_rule_note=rule_note
    )
    points = _dedup(raw_points, policy)

    outside = [p[0] for p in points if p[0] < lo or p[0] > hi]
    if outside:
        raise UFDemoError(
            CONFIG_INVALID,
            "数据点落在声明的有效区间之外",
            field_path="valid_range.x",
            actual=outside[:8],
            requirement=f"valid_range.x 为 [{lo!r}, {hi!r}]，必须覆盖全部数据点",
            suggestion="有效区间不得窄于实测点（窄于数据点的区间意味着这些点永不参与插值，通常是笔误）。",
        )

    # --- 体积类曲线的红线 -------------------------------------------------
    if semantics == SEMANTIC_VOLUME_PER_ENERGY:
        if str(y_quantity["name"]) not in ("removal_volume", "volume_per_energy", "removal_volume_per_energy"):
            # 只是提示性约束：体积曲线的纵轴必须是体积类量
            if "volume" not in str(y_quantity["name"]).lower():
                raise UFDemoError(
                    CONFIG_INVALID,
                    "volume_per_energy 语义的纵轴必须是体积类量",
                    field_path="y_quantity.name",
                    actual=y_quantity["name"],
                    requirement="纵轴量名含 volume（例如 removal_volume）",
                    suggestion="只有终态体积/去除效率时不得改称深度——没有形状假设无法反推局部深度。",
                )
        if raw.get("depth_direction") is not None:
            raise UFDemoError(
                CONFIG_INVALID,
                "体积曲线不得声明 depth_direction",
                field_path="depth_direction",
                actual=raw.get("depth_direction"),
                requirement="volume_per_energy 语义下 depth_direction 必须缺省或为 null",
                suggestion="体积曲线只进体积评估器；加 depth_direction 等于宣称它是深度曲线。",
            )
    elif semantics == SEMANTIC_EVENT_INCREMENT and not raw.get("depth_direction"):
        raise UFDemoError(
            CONFIG_INVALID,
            "逐事件增量曲线必须声明 depth_direction",
            field_path="depth_direction",
            actual=raw.get("depth_direction"),
            requirement="非空（例如 surface_normal）",
            suggestion="逐事件核按表面法向加减材料，方向必须显式。",
        )

    return ResponseCurve(
        curve_id=str(raw["curve_id"]),
        material_id=str(raw["material_id"]),
        material_identity=dict(raw["material_identity"]),
        x_quantity=x_quantity,
        y_quantity=y_quantity,
        output_semantics=str(semantics),
        fixed_conditions=dict(raw["fixed_conditions"]),
        protocol=dict(raw["protocol"]),
        source_figure_or_table=str(raw["source_figure_or_table"]),
        valid_range=(lo, hi),
        valid_range_note=str(vr.get("note", "")),
        points=points,
        raw_points=raw_points,
        duplicate_report=dup_report,
        source_path=str(path),
        points_path=str(points_path),
        evidence_status=str(raw.get("evidence_status", "unverified")),
        source_type=str(raw.get("source_type", "")),
        depth_direction=raw.get("depth_direction"),
        fluence_basis=raw.get("fluence_basis"),
        notes=[str(n) for n in (raw.get("notes") or [])],
        limitations=[str(n) for n in (raw.get("limitations") or [])],
        source_ids=[str(s) for s in ((raw.get("source") or {}).get("source_ids") or [])],
    )


def _dedup(raw_points: list[tuple[float, float]], policy: str) -> list[tuple[float, float]]:
    """按策略合并重复 x；``reject`` 时（只能是无重复的情况）原样返回。"""
    groups: list[list[tuple[float, float]]] = []
    for pt in raw_points:
        if groups and pt[0] == groups[-1][0][0]:
            groups[-1].append(pt)
        else:
            groups.append([pt])
    if policy == "reject":
        return list(raw_points)
    out: list[tuple[float, float]] = []
    for g in groups:
        if len(g) == 1:
            out.append(g[0])
            continue
        ys = [p[1] for p in g]
        if policy == "mean":
            y = sum(ys) / len(ys)
        elif policy == "first":
            y = ys[0]
        else:
            y = ys[-1]
        out.append((g[0][0], y))
    return out


def iter_curve_cards(curves_dir: str | Path) -> list[Path]:
    """列出目录下的曲线卡（``*.curve.json``），按文件名排序。"""
    d = Path(curves_dir)
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.curve.json"))


def load_curves(curves_dir: str | Path) -> list[ResponseCurve]:
    return [load_curve(p) for p in iter_curve_cards(curves_dir)]


# ---------------------------------------------------------------------------
# 插值
# ---------------------------------------------------------------------------


def _as_float_list(x: Any) -> list[float]:
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        vals = [float(x)]
    elif isinstance(x, Sequence) and not isinstance(x, (str, bytes)):
        vals = [float(v) for v in x]
    else:
        try:  # numpy 标量/数组
            import numpy as np

            arr = np.asarray(x, dtype=float).ravel()
            vals = [float(v) for v in arr]
        except Exception as exc:  # noqa: BLE001
            raise UFDemoError(
                CONFIG_INVALID,
                "查表 x 必须是标量或数值序列",
                field_path="x",
                actual=repr(x),
                requirement="float 或 float 序列",
            ) from exc
    if not vals:
        raise UFDemoError(
            CONFIG_INVALID, "查表 x 不能为空", field_path="x", actual=x, requirement="至少一个查询点"
        )
    for v in vals:
        if not math.isfinite(v):
            raise UFDemoError(
                NUMERIC_NONFINITE,
                "查表 x 出现非有限数值",
                field_path="x",
                actual=v,
                requirement="有限数值",
                suggestion="nan/inf 不是有效的查询点。",
            )
    return vals


def lookup(
    curve: ResponseCurve,
    x: Any,
    *,
    method: str = "linear",
    allow_out_of_range: bool = False,
) -> TableLookup:
    """在曲线上查值。

    * ``method="linear"`` —— 默认分段线性，**不外推**；
    * ``method="pchip"`` —— 保形三次插值，``extrapolate=False``。
      环境缺少 SciPy 时抛 ``NOT_IMPLEMENTED``，**绝不**静默退化为线性
      （任务书 3.5：越界/不可用要显式返回，不能悄悄换算法）。

    越界时默认抛 ``TABLE_OUT_OF_RANGE``；``allow_out_of_range=True`` 时返回
    ``values`` 为 ``None`` 的报告（**不是 0**）。
    """
    if method not in INTERPOLATION_METHODS:
        raise UFDemoError(
            CONFIG_INVALID,
            "插值方法非法",
            field_path="method",
            actual=method,
            requirement=f"取值属于 {list(INTERPOLATION_METHODS)}",
        )
    if method == "pchip" and not PCHIP_AVAILABLE:
        raise UFDemoError(
            NOT_IMPLEMENTED,
            "PCHIP 需要 SciPy，当前环境未安装",
            field_path="method",
            actual="pchip",
            requirement="安装 scipy（pyproject 的 table extra）后才能使用 pchip",
            suggestion=(
                "pip install -i https://pypi.tuna.tsinghua.edu.cn/simple scipy；"
                "或改用 method='linear'（默认分段线性）。本工具不会自动换算法。"
            ),
        )

    xs = _as_float_list(x)
    lo, hi = curve.valid_range
    tol = _RANGE_REL_TOL * max(1.0, abs(lo), abs(hi))
    in_range = [(lo - tol) <= v <= (hi + tol) for v in xs]

    status = "ok"
    if not all(in_range):
        below = any(v < lo - tol for v in xs)
        above = any(v > hi + tol for v in xs)
        status = "mixed_out_of_range" if (below and above) else ("below_range" if below else "above_range")

    if status != "ok" and not allow_out_of_range:
        out_vals = [v for v, ok in zip(xs, in_range) if not ok]
        direction = {
            "below_range": "低于有效区间下界",
            "above_range": "高于有效区间上界",
            "mixed_out_of_range": "同时低于下界并高于上界",
        }[status]
        raise UFDemoError(
            TABLE_OUT_OF_RANGE,
            f"查表越界：{direction}",
            field_path="x",
            actual=out_vals[:8],
            requirement=f"查询点必须落在 [{lo!r}, {hi!r}] 内",
            suggestion=(
                "越界不自动返回 0，也不钳到端点；低于量测区间不视为无去除"
                "（除非另有独立阈值律支持）。请收窄查询区间或补充该区间实测数据。"
            ),
        )

    if status == "ok":
        if method == "linear":
            values = _linear(curve, xs)
        else:
            values = _pchip(curve, xs)
    else:
        values = [
            (None if not ok else (_linear(curve, [v])[0] if method == "linear" else _pchip(curve, [v])[0]))
            for v, ok in zip(xs, in_range)
        ]

    reason = (
        "全部查询点位于有效区间内。"
        if status == "ok"
        else (
            f"存在越界查询点（状态 {status}）；越界项返回 None，不返回 0，也不外推。"
        )
    )
    notes = [
        f"插值方法：{method}（{'分段线性，默认' if method == 'linear' else '保形 PCHIP，extrapolate=False'}）",
        f"有效区间：{lo!r} ≤ x ≤ {hi!r}（{curve.valid_range_note or '未附说明'}）",
        f"曲线去向：{curve.route_zh}（语义 {curve.output_semantics}）",
    ]
    if curve.duplicate_report.applied:
        notes.append(
            f"重复 x 处理：{curve.duplicate_report.policy}（{curve.duplicate_report.merged_count} 个重复点已合并；"
            f"规则：{curve.duplicate_report.rule_note}；原始点 {len(curve.raw_points)} 条已保留）"
        )
    if not curve.can_enter_event_kernel:
        notes.append("该曲线不得进入逐事件核，也不得用于生成局部形貌。")

    return TableLookup(
        curve_id=curve.curve_id,
        method=method,
        x=xs,
        values=values,
        in_range=in_range,
        status=status,
        reason=reason,
        valid_range=(lo, hi),
        output_semantics=curve.output_semantics,
        route=curve.route,
        event_kernel_allowed=curve.can_enter_event_kernel,
        x_quantity=str(curve.x_quantity["name"]),
        y_quantity=str(curve.y_quantity["name"]),
        x_unit=curve.x_unit,
        y_unit=curve.y_unit,
        notes=notes,
    )


def _linear(curve: ResponseCurve, xs: list[float]) -> list[float]:
    """手写分段线性，避免 ``numpy.interp`` 在区间外静默钳到端点。"""
    px = curve.xs
    py = curve.ys
    n = len(px)
    out: list[float] = []
    for v in xs:
        if v <= px[0]:
            out.append(py[0])
            continue
        if v >= px[-1]:
            out.append(py[-1])
            continue
        lo, hi = 0, n - 1
        while hi - lo > 1:  # 二分找到 px[lo] <= v <= px[hi]
            mid = (lo + hi) // 2
            if px[mid] <= v:
                lo = mid
            else:
                hi = mid
        x0, x1 = px[lo], px[lo + 1]
        y0, y1 = py[lo], py[lo + 1]
        if x1 == x0:  # 理论上已被去重排除
            out.append(y0)
            continue
        t = (v - x0) / (x1 - x0)
        val = y0 + t * (y1 - y0)
        if not math.isfinite(val):
            raise UFDemoError(
                NUMERIC_NONFINITE,
                "线性插值得到非有限值",
                field_path="x",
                actual=v,
                requirement="插值结果必须有限",
            )
        out.append(val)
    return out


def _pchip(curve: ResponseCurve, xs: list[float]) -> list[float]:
    import numpy as np

    f = PchipInterpolator(np.asarray(curve.xs), np.asarray(curve.ys), extrapolate=False)
    vals = np.asarray(f(np.asarray(xs)), dtype=float)
    if not np.all(np.isfinite(vals)):
        raise UFDemoError(
            NUMERIC_NONFINITE,
            "PCHIP 插值得到非有限值（不应发生：查询点已先行越界检查）",
            field_path="x",
            actual=[float(v) for v in np.asarray(xs)[~np.isfinite(vals)]][:8],
            requirement="extrapolate=False 且查询点在有效区间内",
            suggestion="这是内部一致性错误，请连同曲线卡一并报告。",
        )
    return [float(v) for v in vals]


def interpolate_grid(
    curve: ResponseCurve,
    *,
    n: int = 200,
    method: str = "linear",
) -> dict[str, list[float]]:
    """在有效区间上等距采样原始点与插值线，供「原始点与插值图」使用。"""
    if n < 2:
        raise UFDemoError(
            CONFIG_INVALID, "n 必须 >= 2", field_path="n", actual=n, requirement=">= 2"
        )
    lo, hi = curve.valid_range
    step = (hi - lo) / (n - 1)
    xs = [lo + step * i for i in range(n)]
    res = lookup(curve, xs, method=method)
    return {
        "method": method,
        "x": xs,
        "y": [float(v) for v in res.values],  # 区间内必然有限
        "raw_x": [p[0] for p in curve.raw_points],
        "raw_y": [p[1] for p in curve.raw_points],
        "curve_x": curve.xs,
        "curve_y": curve.ys,
    }


# ---------------------------------------------------------------------------
# 语义路由与两条红线
# ---------------------------------------------------------------------------


def curve_route(curve: ResponseCurve) -> str:
    return curve.route


def assert_no_local_depth_from_volume(curve: ResponseCurve, requested_quantity: str) -> None:
    """红线：**仅有终态体积/去除效率时不能唯一反推每个位置的去除深度**。

    任务书 3.5：没有额外形状假设，体积/平均率曲线不得生成局部深度剖面。
    """
    q = str(requested_quantity)
    if curve.output_semantics == SEMANTIC_EVENT_INCREMENT:
        return
    if q.lower() in LOCAL_DEPTH_QUANTITIES or "depth" in q.lower():
        raise UFDemoError(
            CONFIG_INVALID,
            f"曲线 {curve.curve_id} 的语义是 {curve.output_semantics}，不能生成局部去除深度",
            field_path="requested_quantity",
            actual=q,
            requirement=(
                "只有 output_semantics=event_depth_increment 的曲线才能产生局部形貌；"
                "体积/平均率/累计曲线只进评估器"
            ),
            suggestion=(
                "不要由体积或终态去除效率反推局部深度：没有额外形状假设时该反推不唯一。"
                "如需局部形貌，请补该工况下的逐事件增量曲线。"
            ),
        )


def assert_curve_can_enter_event_kernel(
    curve: ResponseCurve,
    *,
    laser: Mapping[str, Any] | None = None,
) -> None:
    """只有 ``event_depth_increment`` 曲线**且协议确实适用**时才允许进事件核。

    ``laser`` 给定时，会按曲线 ``fixed_conditions`` 里的 ``{value, rel_tol}``
    逐项比对；不匹配抛 ``CONDITION_MISMATCH``。
    """
    if curve.output_semantics != SEMANTIC_EVENT_INCREMENT:
        raise UFDemoError(
            RESPONSE_SEMANTICS_INVALID,
            f"曲线 {curve.curve_id} 的语义 {curve.output_semantics} 不得进入事件核",
            field_path="output_semantics",
            actual=curve.output_semantics,
            requirement="只有 event_depth_increment 可进逐事件主循环",
            suggestion="平均率/累计/体积/阈值曲线请走评估器；不要逐事件累加。",
        )

    if laser is None:
        return

    fields = ("wavelength_m", "pulse_duration_s", "repetition_rate_Hz")
    for key in fields:
        if laser.get(key) is None:
            continue
        spec = curve.fixed_conditions.get(key)
        if spec is None:
            continue
        if isinstance(spec, Mapping):
            if spec.get("value") is None:
                continue  # 该条件在曲线上标记为“未定义”（例如解析 fixture 无波长）
            target = float(spec["value"])
            tol = float(spec.get("rel_tol") or 0.0)
        else:
            target = float(spec)
            tol = 0.0
        if target == 0.0:
            continue
        got = float(laser[key])
        rel = abs(got - target) / abs(target)
        if rel > tol + 1e-15:
            raise UFDemoError(
                CONDITION_MISMATCH,
                f"曲线 {curve.curve_id} 的固定条件 {key} 与请求工况不匹配",
                field_path=f"laser.{key}",
                actual=got,
                requirement=f"{target!r} ± {tol * 100:.4g}%（曲线卡 fixed_conditions）",
                suggestion="查表只在固定协议下有效；请换用条件匹配的曲线，或不要用该曲线做定量查表。",
            )


def derivable_quantities(curve: ResponseCurve) -> list[str]:
    """该曲线**允许**派生出的量（用于界面如实展示可做什么）。"""
    if curve.output_semantics == SEMANTIC_EVENT_INCREMENT:
        return ["removal_depth_per_pulse", "local_depth"]
    if curve.output_semantics == SEMANTIC_VOLUME_PER_ENERGY:
        return ["removal_volume", "removal_volume_per_energy"]
    if curve.output_semantics == SEMANTIC_MEAN_RATE:
        return ["mean_depth_per_effective_pulse", "protocol_cumulative_depth"]
    if curve.output_semantics == SEMANTIC_CUMULATIVE:
        return ["protocol_cumulative_depth"]
    if curve.output_semantics == SEMANTIC_THRESHOLD_ONLY:
        return ["threshold_fluence"]
    return ["protocol_value"]


def curve_watermark(curve: ResponseCurve) -> str:
    """曲线的一行式标签：身份 + 证据 + 语义 + 去向（供插图与报告复用）。

    标签必须描述**该图实际是什么量**：``output_semantics`` 与去向一起给出，
    避免读者把「平均率」当成「逐事件增量」。
    """
    ident = curve.material_identity or {}
    fam = ident.get("family")
    grade = ident.get("grade")
    parts = [str(curve.material_id)]
    if fam or grade:
        parts.append(f"{fam}／{grade}")
    parts.append(f"证据 {curve.evidence_status}")
    parts.append(f"语义 {curve.output_semantics}")
    parts.append(curve.route_zh)
    return "｜".join(parts)


def match_fixed_conditions(
    curve: ResponseCurve,
    laser: Mapping[str, Any],
) -> tuple[list[str], str]:
    """比对请求工况与曲线固定条件，返回 ``(warnings, condition_match)``。

    与 :func:`ufdemo.references.match_laser_conditions` 同口径：不匹配即拒绝
    （由 :func:`assert_curve_can_enter_event_kernel` 抛 ``CONDITION_MISMATCH``）。
    """
    warnings: list[str] = []
    checked = 0
    for key in ("wavelength_m", "pulse_duration_s", "repetition_rate_Hz"):
        if laser.get(key) is None or key not in curve.fixed_conditions:
            continue
        spec = curve.fixed_conditions[key]
        if isinstance(spec, Mapping) and spec.get("value") is None:
            continue  # 曲线上该条件标记为“未定义”，无法逐项核对
        checked += 1
    if checked == 0:
        warnings.append(
            "请求工况与曲线固定条件无交集字段，无法逐项核对："
            "条件一致性未验证（condition_match=not_checkable）。"
        )
        return warnings, "not_checkable"
    return warnings, f"checked_{checked}_fields"
