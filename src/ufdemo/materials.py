"""材料目录、能力推导与卡版本（执行细则第 4.4 节、第 7 节）。

关键约定：能力**由完整条件和响应类型推导**，不用“参数数量”或材料名称判断
是否可预测形貌。``null`` 表示数据缺失，能力计算据此禁用深度，绝不补近似材料值。

迁移规则（细则 4.4）：原始 F01/F02 文件保持字节不变，迁移产物写到
``data/materials/*.json`` 并附字段迁移记录 ``docs/reports/material_migration.csv``。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .config import (
    EVIDENCE_STATUSES,
    SEMANTIC_CUMULATIVE,
    SEMANTIC_EVENT_INCREMENT,
    SEMANTIC_MEAN_RATE,
    SEMANTIC_THRESHOLD_ONLY,
    SEMANTIC_TRACK_PASS,
    SEMANTIC_VOLUME_PER_ENERGY,
)
from .errors import CONFIG_INVALID, MATERIAL_CAPABILITY_MISSING, UFDemoError

# 能力名（细则 7 节表 + 任务书 3.2 节语义枚举）
CAP_EVENT_INCREMENT = SEMANTIC_EVENT_INCREMENT
CAP_THRESHOLD = SEMANTIC_THRESHOLD_ONLY
CAP_MEAN_RATE = SEMANTIC_MEAN_RATE
CAP_CUMULATIVE = SEMANTIC_CUMULATIVE
CAP_TRACK_PASS = SEMANTIC_TRACK_PASS
CAP_VOLUME_EFFICIENCY = SEMANTIC_VOLUME_PER_ENERGY
CAP_REFERENCE_EVALUATOR = "reference_evaluator"
CAP_SYNTHETIC_STRUCTURE = "synthetic_structure"
CAP_TABLE_IMPORT = "table_import"

ALL_CAPABILITIES: tuple[str, ...] = (
    CAP_EVENT_INCREMENT,
    CAP_THRESHOLD,
    CAP_MEAN_RATE,
    CAP_CUMULATIVE,
    CAP_TRACK_PASS,
    CAP_VOLUME_EFFICIENCY,
    CAP_REFERENCE_EVALUATOR,
    CAP_SYNTHETIC_STRUCTURE,
    CAP_TABLE_IMPORT,
)


def _finite_positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 and float(value) == float(value)


def _missing(value: Any) -> bool:
    return value is None or value == "unknown"


def _normalize_response(raw: Mapping[str, Any]) -> dict[str, Any]:
    """把卡内的**声明单位**字段映射为内部量。

    * SI 卡：``threshold_J_m2`` / ``delta_m``
    * 合成卡：``threshold_over_F_ref`` / ``delta_over_L_ref``（已是内部归一量）

    合成卡里 δ 的归一化基准是 **L_ref**，不是 delta_ref：逐事件更新作用在
    ``h/L_ref`` 上，深度就是 ``h/L_ref`` 之差，与光斑、层厚、颗粒尺寸同处一个
    尺度（任务书「合成模式的单位规则」）。若把 ``δ/delta_ref`` 当成内部量直接相减，
    深度会被放大 ``L_ref/delta_ref`` 倍（本工程默认 100 倍）。旧字段名
    ``delta_over_delta_ref`` 一律拒绝，避免静默的百倍错误。

    不做任何跨材料或跨脉宽的补值：缺失保持 ``None``。
    """
    r = dict(raw)
    if r.get("delta_over_delta_ref") is not None:
        raise UFDemoError(
            CONFIG_INVALID,
            "合成卡使用了旧字段 delta_over_delta_ref",
            field_path="response.delta_over_delta_ref",
            actual=r.get("delta_over_delta_ref"),
            requirement="改用 delta_over_L_ref（δ/L_ref，与几何同一长度尺度）",
            suggestion=(
                "δ/L_ref 与 δ/delta_ref 相差 L_ref/delta_ref 倍；"
                "直接沿用旧值会让深度整体差 100 倍。"
            ),
        )
    if r.get("threshold_internal") is None:
        if r.get("threshold_J_m2") is not None:
            r["threshold_internal"] = r.get("threshold_J_m2")
        elif r.get("threshold_over_F_ref") is not None:
            r["threshold_internal"] = r.get("threshold_over_F_ref")
        else:
            r["threshold_internal"] = None
    if r.get("delta_internal") is None:
        if r.get("delta_m") is not None:
            r["delta_internal"] = r.get("delta_m")
        elif r.get("delta_over_L_ref") is not None:
            r["delta_internal"] = r.get("delta_over_L_ref")
        else:
            r["delta_internal"] = None
    return r


def _normalize_multi_response(raw: Mapping[str, Any]) -> dict[str, Any]:
    m = dict(raw)
    if m.get("Fth1_internal") is None and m.get("Fth1_fitted_J_m2") is not None:
        m["Fth1_internal"] = m.get("Fth1_fitted_J_m2")
    if m.get("Fth_infinity_internal") is None and m.get("Fth_infinity_J_m2") is not None:
        m["Fth_infinity_internal"] = m.get("Fth_infinity_J_m2")
    return m


@dataclass
class Capability:
    name: str
    available: bool
    reason: str
    missing: tuple[str, ...] = ()
    conditional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "conditional": self.conditional,
            "reason": self.reason,
            "missing": list(self.missing),
        }


@dataclass
class MaterialSpec:
    schema_version: str
    id: str
    identity: Mapping[str, Any]
    structure_type: str
    evidence_status: str
    source_type: str
    card_version: str
    response: Mapping[str, Any] = field(default_factory=dict)
    allowed_run_modes: tuple[str, ...] = ()
    history_definition: Mapping[str, Any] = field(default_factory=dict)
    source_equation: str | None = None
    source_figure_or_table: str | None = None
    validity_domain: Mapping[str, Any] = field(default_factory=dict)
    applicability: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)
    threshold_candidates: tuple[Mapping[str, Any], ...] = ()
    reference_protocol: Mapping[str, Any] = field(default_factory=dict)
    multi_response: Mapping[str, Any] = field(default_factory=dict)
    phases: tuple[Mapping[str, Any], ...] = ()
    fixture_only: bool = False
    enabled_by_default: bool = True
    blocked_reason: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    card_sha256: str | None = None
    capabilities: dict[str, Capability] = field(default_factory=dict)

    NORMALIZED_MATERIAL_DIRS = ("data/materials",)

    # -- 载入 ---------------------------------------------------------------
    @staticmethod
    def from_dict(raw: Mapping[str, Any], *, card_sha256: str | None = None) -> "MaterialSpec":
        if not isinstance(raw, Mapping):
            raise UFDemoError(CONFIG_INVALID, "材料卡必须是 JSON 对象", field_path="<material>", actual=type(raw).__name__)
        mid = raw.get("id")
        if not isinstance(mid, str) or not mid:
            raise UFDemoError(CONFIG_INVALID, "材料卡缺少 id", field_path="id", actual=mid)
        ev = raw.get("evidence_status")
        if ev not in EVIDENCE_STATUSES:
            raise UFDemoError(
                CONFIG_INVALID,
                "材料卡 evidence_status 非法",
                field_path="evidence_status",
                actual=ev,
                requirement=f"取值属于 {list(EVIDENCE_STATUSES)}",
                suggestion="细则 2.1：执行层统一五种证据状态，不使用 synthetic。",
            )
        spec = MaterialSpec(
            schema_version=str(raw.get("schema_version", "1.0")),
            id=mid,
            identity=dict(raw.get("identity", {}) or {}),
            structure_type=str(raw.get("structure_type", "homogeneous")),
            evidence_status=ev,
            source_type=str(raw.get("source_type", "research_card_migration")),
            card_version=str(raw.get("card_version", "0.0.0")),
            response=_normalize_response(raw.get("response", {}) or {}),
            allowed_run_modes=tuple(raw.get("allowed_run_modes", ()) or ()),
            history_definition=dict(raw.get("history_definition", {}) or {}),
            source_equation=raw.get("source_equation"),
            source_figure_or_table=raw.get("source_figure_or_table"),
            validity_domain=dict(raw.get("validity_domain", {}) or {}),
            applicability=dict(raw.get("applicability", {}) or {}),
            provenance=dict(raw.get("provenance", {}) or {}),
            threshold_candidates=tuple(raw.get("threshold_candidates", ()) or ()),
            reference_protocol=dict(raw.get("reference_protocol", {}) or {}),
            multi_response=_normalize_multi_response(raw.get("multi_response", {}) or {}),
            phases=tuple(raw.get("phases", ()) or ()),
            fixture_only=bool(raw.get("fixture_only", False)),
            enabled_by_default=bool(raw.get("enabled_by_default", True)),
            blocked_reason=raw.get("blocked_reason"),
            raw=dict(raw),
            card_sha256=card_sha256,
        )
        spec.capabilities = compute_capabilities(spec)
        return spec

    # -- 查询 ---------------------------------------------------------------
    def capability(self, name: str) -> Capability:
        return self.capabilities.get(name, Capability(name, False, "能力未登记"))

    def is_physical(self) -> bool:
        """是否允许作为物理材料预测（合成演示与测试 fixture 为 False）。"""
        if self.fixture_only:
            return False
        if self.source_type in ("analytic_test_definition", "synthetic_definition"):
            return False
        return bool(self.applicability.get("physical_material_prediction_allowed", False))

    def watermark(self) -> dict[str, Any]:
        """贯穿导出与回放的标签（细则 11.3 / 任务书 15.2）。"""
        return {
            "material_id": self.id,
            "family": self.identity.get("family"),
            "grade": self.identity.get("grade"),
            "evidence_status": self.evidence_status,
            "source_type": self.source_type,
            "card_version": self.card_version,
            "card_sha256": self.card_sha256,
            "physical_prediction_allowed": self.is_physical(),
            "identity_confirmed_by_user": bool(self.identity.get("material_identity_confirmed_by_user", False)),
        }

    def to_dict(self) -> dict[str, Any]:
        return dict(self.raw)

    # -- 响应语义 -----------------------------------------------------------
    @property
    def response_semantics(self) -> str | None:
        return self.response.get("output_semantics")

    @property
    def delta_m(self) -> float | None:
        return self.response.get("delta_internal")

    @property
    def threshold_internal(self) -> float | None:
        return self.response.get("threshold_internal")


def _check_log_kernel(spec: MaterialSpec) -> tuple[bool, str, tuple[str, ...]]:
    """固定阈值对数核的完整性检查（细则 7 节表）。

    无论响应类型是否匹配，都给出**缺失字段清单**，让能力表能直接说明
    “缺什么”，而不是只写一句“类型不对”。
    """
    missing: list[str] = []
    r = spec.response
    if not r:
        return False, "材料卡未提供 response 块", ("response",)
    kind = r.get("kind")
    if _missing(r.get("fluence_basis")):
        missing.append("fluence_basis")
    if _missing(r.get("depth_direction")):
        missing.append("depth_direction")
    if not _finite_positive(r.get("threshold_internal")):
        missing.append("threshold")
    if not _finite_positive(r.get("delta_internal")):
        missing.append("delta_m")

    if kind not in ("log_fixed", "log_fixed_effective", "logarithmic_effective"):
        detail = "、".join(missing) if missing else "响应类型不产生逐事件深度"
        return False, f"响应类型 {kind!r} 不是固定阈值对数核；缺 {detail}", tuple(missing)
    semantics = r.get("output_semantics")
    if semantics != SEMANTIC_EVENT_INCREMENT:
        return False, f"响应语义为 {semantics!r}，不能直接逐事件累加", tuple(missing)
    if missing:
        return False, "缺少 " + "、".join(missing) + "，无法计算去除深度", tuple(missing)
    return True, "固定阈值对数核字段完整", ()


def compute_capabilities(spec: MaterialSpec) -> dict[str, Capability]:
    """由完整条件与响应类型推导能力（细则 7 节）。"""
    caps: dict[str, Capability] = {}
    r = spec.response
    m = spec.multi_response
    domain_known = bool(spec.validity_domain) and spec.validity_domain.get("scope") not in (None, "unknown")
    analytic = spec.validity_domain.get("scope") == "analytic_all"

    # 1. 逐事件增量
    ok, reason, missing = _check_log_kernel(spec)
    if ok:
        cond = spec.response.get("kind") == "log_fixed_effective"
        if cond:
            reason = (
                "固定阈值对数核字段完整，但阈值是“加工有效”阈值："
                "仅在文献加工分支的条件匹配岗位上可用；不匹配时拒绝定量执行。"
            )
        if not (domain_known or analytic or spec.fixture_only):
            ok, reason, missing = False, "有效范围未确认（validity_domain.scope=unknown），不开放定量深度", ("validity_domain",)
        caps[CAP_EVENT_INCREMENT] = Capability(CAP_EVENT_INCREMENT, ok, reason, missing, conditional=cond)
    else:
        caps[CAP_EVENT_INCREMENT] = Capability(CAP_EVENT_INCREMENT, False, reason, missing)

    # 2. 阈值展示
    thr = r.get("threshold_internal")
    n_thr = sum(1 for c in spec.threshold_candidates if _finite_positive(c.get("threshold_J_m2")))
    if _finite_positive(thr) or n_thr > 0:
        caps[CAP_THRESHOLD] = Capability(
            CAP_THRESHOLD,
            True,
            f"提供 {n_thr} 个条件对应阈值候选" if n_thr else "提供条件对应阈值",
        )
    else:
        caps[CAP_THRESHOLD] = Capability(CAP_THRESHOLD, False, "无可用阈值；阈值展示也不开放", ("threshold",))

    # 3. 平均去除率（仅评估器）
    if m.get("kind") == "exponential_saturation_effective_N":
        need = [k for k in ("Fth1_internal", "Fth_infinity_internal", "k_inc_per_pulse", "delta_eff_mean_m") if not _finite_positive(m.get(k))]
        caps[CAP_MEAN_RATE] = Capability(
            CAP_MEAN_RATE,
            not need,
            "原文式（7）平均去除率：只能进入参考评估器，不得进入逐事件主循环" if not need else "字段不完整",
            tuple(need),
        )
        caps[CAP_REFERENCE_EVALUATOR] = Capability(CAP_REFERENCE_EVALUATOR, not need, "可复现原文有效 N 语义" if not need else "字段不完整", tuple(need))
        caps[CAP_EVENT_INCREMENT] = Capability(
            CAP_EVENT_INCREMENT,
            False,
            "原文平均去除率不是逐事件增量；转换为逐事件模型必须记录 engineering_extension 并另做验证",
        )
    elif m.get("kind") == "power_law_incubation":
        caps[CAP_MEAN_RATE] = Capability(CAP_MEAN_RATE, False, "仅给出整体孵化阈值形态，无平均率拟合式")
        caps[CAP_EVENT_INCREMENT] = Capability(
            CAP_EVENT_INCREMENT,
            False,
            "缺少去除尺度 δ：不得由阈值生成物理深度",
            ("delta_m",),
        )
    if r.get("kind") == "reference_N_threshold":
        caps[CAP_THRESHOLD] = Capability(
            CAP_THRESHOLD,
            True,
            "条件对应阈值可用；N 的定义必须同时显示，禁止当作单脉冲阈值",
        )

    # 4. 合成结构与导入入口
    if spec.structure_type in ("particle_composite", "laminated_fiber_composite", "homogeneous_effective", "homogeneous"):
        caps[CAP_SYNTHETIC_STRUCTURE] = Capability(CAP_SYNTHETIC_STRUCTURE, True, "允许生成显式标记的合成结构")
    caps[CAP_TABLE_IMPORT] = Capability(CAP_TABLE_IMPORT, True, "允许导入受校验的曲线卡（批次 F / T10 交付）")

    # 5. 未开放的能力必须在配置层拦截
    for name in (CAP_CUMULATIVE, CAP_TRACK_PASS, CAP_VOLUME_EFFICIENCY):
        caps.setdefault(name, Capability(name, False, "非逐事件增量语义；禁止进入事件核"))

    return caps


# ---------------------------------------------------------------------------
# 目录加载
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_material_card(path: str | Path) -> MaterialSpec:
    p = Path(path)
    if not p.exists():
        raise UFDemoError(
            CONFIG_INVALID,
            "材料卡文件不存在",
            field_path="material_card_file",
            actual=str(p),
            suggestion="核对路径；真实材料卡在 data/materials/，人工 fixture 在 tests/fixtures/。",
        )
    raw = json.loads(p.read_text(encoding="utf-8"))
    return MaterialSpec.from_dict(raw, card_sha256=_sha256_file(p))


def load_material_catalog(material_dir: str | Path, *, include_non_physical: bool = True) -> dict[str, MaterialSpec]:
    """载入材料目录。文件名以 ``_`` 开头的合成演示卡按需排除。"""
    d = Path(material_dir)
    out: dict[str, MaterialSpec] = {}
    if not d.exists():
        return out
    for p in sorted(d.glob("*.json")):
        spec = load_material_card(p)
        if p.name.startswith("_") and not include_non_physical:
            continue
        if spec.id in out:
            raise UFDemoError(
                CONFIG_INVALID,
                "材料卡 ID 重复",
                field_path="id",
                actual=spec.id,
                requirement="目录内 ID 唯一",
                suggestion=f"冲突文件：{p.name}",
            )
        out[spec.id] = spec
    return out


def resolve_material(config: Any, material_dir: str | Path, *, include_non_physical: bool = True) -> MaterialSpec:
    """按 ``config.material_card_file`` 或 ``config.material_id`` 解析材料卡。"""
    cf = getattr(config, "material_card_file", None)
    if cf:
        spec = load_material_card(cf)
        if spec.id != getattr(config, "material_id", spec.id):
            raise UFDemoError(
                MATERIAL_CAPABILITY_MISSING,
                "配置的 material_id 与卡内 id 不一致",
                field_path="material_id",
                actual=config.material_id,
                requirement=f"应等于卡内 id {spec.id!r}",
                suggestion="对齐两者，避免凭名称自动匹配“最近”的卡。",
            )
        return spec
    catalog = load_material_catalog(material_dir, include_non_physical=include_non_physical)
    mid = getattr(config, "material_id", None)
    if mid not in catalog:
        raise UFDemoError(
            MATERIAL_CAPABILITY_MISSING,
            f"材料目录中找不到 id={mid!r}",
            field_path="material_id",
            actual=mid,
            requirement=f"目录中可用：{sorted(catalog)}",
            suggestion="补材料卡或修正 material_id；未知牌号不自动匹配“最近”的卡。",
        )
    return catalog[mid]


def capability_table(catalog: Mapping[str, MaterialSpec]) -> list[dict[str, Any]]:
    """能力总表（批次 H / G06、G09 报告的输入）。"""
    rows: list[dict[str, Any]] = []
    for mid, spec in sorted(catalog.items()):
        for cap in ALL_CAPABILITIES:
            c = spec.capability(cap)
            rows.append(
                {
                    "material_id": mid,
                    "family": spec.identity.get("family"),
                    "grade": spec.identity.get("grade"),
                    "evidence_status": spec.evidence_status,
                    "capability": cap,
                    "available": c.available,
                    "conditional": c.conditional,
                    "reason": c.reason,
                    "missing": ";".join(c.missing),
                }
            )
    return rows
