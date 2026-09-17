"""材料目录、能力推导与卡版本（执行细则第 4.4 节、第 7 节）。

关键约定：能力**由完整条件和响应类型推导**，不用“参数数量”或材料名称判断
是否可预测形貌。``null`` 表示数据缺失，能力计算据此禁用深度，绝不补近似材料值。

迁移规则（细则 4.4）：原始 F01/F02 文件保持字节不变，迁移产物写到
``data/materials/*.json`` 并附字段迁移记录 ``docs/reports/material_migration.csv``。
"""

from __future__ import annotations

import hashlib
import json
import math
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
from .errors import (
    CONDITION_MISMATCH,
    CONFIG_INVALID,
    MATERIAL_CAPABILITY_MISSING,
    RESPONSE_SEMANTICS_INVALID,
    UFDemoError,
)

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
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


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
    def from_dict(
        raw: Mapping[str, Any],
        *,
        card_sha256: str | None = None,
        protocol_base: str | Path | None = None,
    ) -> "MaterialSpec":
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
            reference_protocol=resolve_reference_protocol(raw, base=protocol_base),
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


def build_watermark(material: MaterialSpec, unit: Any, *, run_mode: str) -> dict[str, Any]:
    """贯穿导出与回放的**完整**标签（细则 11.3 / 任务书 15.2）。

    这是标签的唯一权威来源：``solver`` 写入结果、``io`` 落盘、界面与回放都读它，
    避免"导出说一个模式、界面显示另一个模式"。``unit`` 允许为 ``None``
    （此时单位相关字段为 ``None``，而不是编造默认标签）。
    """
    wm = material.watermark()
    unit_mode = getattr(unit, "mode", None) if unit is not None else None
    wm.update(
        {
            "run_mode": run_mode,
            "unit_mode": unit_mode,
            "unit_system": unit_mode,
            "length_label": getattr(unit, "length_label", None) if unit is not None else None,
            "fluence_label": getattr(unit, "fluence_label", None) if unit is not None else None,
            "depth_label": getattr(unit, "depth_label", None) if unit is not None else None,
            "physical_depth_export_allowed": (
                getattr(unit, "allows_physical_depth_export", None) if unit is not None else None
            ),
            "geometry_feedback": None,  # 由调用方按实际求解器设置补
            "acceleration": None,
            "warnings": [],
        }
    )
    return wm


def watermark_rows(wm: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把水印压成 ``statistics.csv`` 的自描述行（``watermark.<key>``）。

    这样即便单独拿走 ``statistics.csv``，也仍能读出材料身份、模式与证据状态。
    """
    keys = (
        "material_id", "family", "grade", "evidence_status", "source_type",
        "card_version", "card_sha256", "physical_prediction_allowed",
        "identity_confirmed_by_user", "run_mode", "unit_mode",
        "physical_depth_export_allowed",
    )
    rows: list[dict[str, Any]] = []
    for k in keys:
        v = wm.get(k)
        rows.append({"metric": f"watermark.{k}", "value": "null" if v is None else v, "unit": ""})
    return rows


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


# ---------------------------------------------------------------------------
# 参考协议装配（ADR-0021）
#
# 材料卡只登记「引用」：``{"protocol_id": ..., "protocol_file": "data/protocols/<id>.json"}``。
# 完整协议体（**源文献的装置条件**）在 ``data/protocols/`` 下独立存放；本函数把它装配回
# ``MaterialSpec.reference_protocol``，形状与分离前**完全一致** ⇒ config / references /
# webcontract 等消费方零改动。
#
# 为什么必须分开（2026-09-17）：光束参数（λ/τ/f/w0）是**设备量**，不是材料属性。
# 核函数 ``a = δ·ln(F/F_th)`` 是局域能流定律，与光斑无关 —— 实测 ``response.py`` 与
# ``solver.py`` 里**零** w0 引用，卡里的 w0 从不进入物理计算，只作「条件门禁」。
# 留在卡里会被误读成材料参数，而且同一台设备的条件要在多张卡里各抄一遍。
#
# 三层归属（不得互相串位）：
#   * 材料属性（δ、F_th、相结构）    → ``data/materials/*.json``
#   * 源文献装置条件（协议）          → ``data/protocols/*.json``
#   * 本机设备（NA/M²/名义 w0 与 zR） → ``data/config/shared_experiment_background.json``
#
# 装配后卡片的 w0 与**本机名义光学**（0.874 µm）不匹配时会被条件门禁如实拒绝 ——
# 这不是缺陷：那张卡描述的是源文献那台机器，同一条 ``validate_run`` 会给出
# ``CONDITION_MISMATCH`` 并指明实际值与要求值。
# ---------------------------------------------------------------------------

REFERENCE_PROTOCOL_DIR = "data/protocols"

#: 装配进 ``reference_protocol`` 的协议字段（保持分离前的键集为子集，只做超集扩展）
_PROTOCOL_RUNTIME_KEYS: tuple[str, ...] = (
    "required_laser",
    "required_history",
    "protocol_note",
    "reference_peak_fluence_J_m2",
    "source_ids",
)


def resolve_reference_protocol(
    raw: Mapping[str, Any], *, base: str | Path | None = None
) -> dict[str, Any]:
    """把卡里的 ``reference_protocol`` 引用装配成完整协议字典。

    * 无 ``reference_protocol`` → ``{}``；
    * 已内联 ``required_laser``/``required_history``（人工 fixture）→ **原样返回**；
    * 引用形式 → 读 ``protocol_file`` 并装配；文件缺失或 ``protocol_id`` 不一致 ⇒ **如实报错**，
      绝不返回空协议、也绝不静默降级。
    """
    rp = dict(raw.get("reference_protocol") or {})
    if not rp:
        return {}
    if "required_laser" in rp or "required_history" in rp:
        return rp

    pid = rp.get("protocol_id")
    rel = rp.get("protocol_file")
    if not isinstance(pid, str) or not pid:
        raise UFDemoError(
            CONFIG_INVALID,
            "reference_protocol 缺少 protocol_id",
            field_path="reference_protocol.protocol_id",
            actual=rp,
            requirement="非空字符串",
        )
    if not isinstance(rel, str) or not rel:
        raise UFDemoError(
            CONFIG_INVALID,
            "材料卡既未内联参考协议，也未给出 reference_protocol.protocol_file",
            field_path="reference_protocol.protocol_file",
            actual=pid,
            requirement='形如 "data/protocols/<protocol_id>.json"',
            suggestion="参考协议已独立存放（ADR-0021）；请用 tools/migrate_materials.py 重新生成材料卡。",
        )

    # 解析协议文件的候选根目录：先按调用方给的基址（通常是卡片所在树的根，这样
    # 生成器把产物写到临时目录时也能自洽解析），再回退到 resource_root()（wheel /
    # 自定义资源目录 / 环境变量覆盖）。都不命中才报错 —— 报错时把**所有**试过的
    # 路径写进 actual，避免"只看到一个不存在的路径"而误判。
    roots: list[Path] = []
    if base is not None:
        roots.append(Path(base))
    try:
        # 延迟导入：``resource_root`` 在包 __init__ 里定义，顶层导入会形成环
        from . import resource_root

        fallback = resource_root()
        if fallback not in roots:
            roots.append(fallback)
    except Exception:  # noqa: BLE001 - 资源根不可用时只保留已给的基址
        pass

    path: Path | None = None
    for root in roots:
        cand = root / rel
        if cand.exists():
            path = cand
            break
    if path is None:
        raise UFDemoError(
            CONFIG_INVALID,
            "参考协议文件不存在",
            field_path="reference_protocol.protocol_file",
            actual=[str(r / rel) for r in roots],
            requirement=f"{pid} 的协议文件应存在于上述任一路径",
            suggestion="重跑 tools/migrate_materials.py 生成 data/protocols/。",
        )
    try:
        proto = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UFDemoError(
            CONFIG_INVALID,
            "参考协议文件不是合法 JSON",
            field_path="reference_protocol.protocol_file",
            actual=f"{path}: {exc}",
        ) from exc
    if proto.get("protocol_id") != pid:
        raise UFDemoError(
            CONFIG_INVALID,
            "协议文件与卡内 protocol_id 不一致",
            field_path="reference_protocol.protocol_id",
            actual={"card": pid, "file": proto.get("protocol_id")},
            requirement="两者必须相同",
            suggestion="材料卡与协议库不同步：重跑 tools/migrate_materials.py。",
        )

    out: dict[str, Any] = {"protocol_id": pid, "protocol_file": rel}
    for key in _PROTOCOL_RUNTIME_KEYS:
        if key in proto:
            out[key] = proto[key]
    return out


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
    # 协议引用按**卡片所在树的根**解析：``<root>/data/materials/x.json`` 的根是
    # ``parents[2]``（materials→data→root），于是 ``data/protocols/<id>.json`` 命中。
    # 这样生成器把产物写到临时目录、或 wheel 换装到别的 prefix 时都能自洽；
    # 解析不到再回退 resource_root()（见 resolve_reference_protocol）。
    resolved = p.resolve()
    base = resolved.parents[2] if len(resolved.parents) >= 3 else resolved.parent
    return MaterialSpec.from_dict(
        raw, card_sha256=_sha256_file(p), protocol_base=base
    )


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


# ---------------------------------------------------------------------------
# 七材料能力入口（批次 H / T14；执行细则 7 节表）
# ---------------------------------------------------------------------------
#
# 细则 7 节给了一张「入口 / 初始开放内容 / 必须拦截」三列表。本节的职责是把它变成
# **可执行**的：每条「必须拦截」都绑定到一个真实存在的 enforcement，并配一个探针。
# ``verify_entry_enforcements`` 会真的跑一遍探针——拦截没生效就报失败，
# 而不是在报告里写一句"已拦截"。
#
# 约定：探针**在拦截生效时返回 None**；拦截没生效时抛 ``AssertionError``。
# 这样"开放内容被高估"和"拦截形同虚设"都会立刻暴露。

ENTRY_SYNTHETIC_DEMO_CARD = "_synthetic_demo_isotropic.json"


@dataclass(frozen=True)
class OpenedItem:
    """入口**开放**的某一项内容，以及可核对的依据。"""

    item: str
    capability: str | None = None   # 能力名：至少一张入口卡必须 available
    run_mode: str | None = None     # 或：至少一张入口卡必须允许该运行模式
    note: str = ""

    def verified_by(self) -> str:
        if self.capability:
            return f"capability:{self.capability}"
        if self.run_mode:
            return f"run_mode:{self.run_mode}"
        return "unbound"


@dataclass(frozen=True)
class BlockedItem:
    """入口**必须拦截**的某一项，绑定到实际生效的 enforcement 与探针。"""

    item: str
    code: str          # 拦截生效时应出现的错误码
    enforcement: str   # 生效位置 module.function
    probe_key: str     # 探针键（见 _ENTRY_PROBES）
    expectation: str   # 探针成功判据的口径说明


@dataclass(frozen=True)
class DeferredItem:
    """规格要求开放、但**当前实现尚未支持**的一项（不得声称为已开放）。

    与 ``BlockedItem`` 的区别：``BlockedItem`` 是**红线**（必须永远拦截）；
    ``DeferredItem`` 是**缺口**（规格要求开放，代码还没做到），必须带可复核的
    ``probe_key`` 证明"现在确实打不开"，以免日后被静默改写成"已开放"。
    """

    item: str
    reason: str        # 当前不支持的实现层原因
    probe_key: str     # 证明"当前确实打不开"的探针键（见 _ENTRY_PROBES）
    expectation: str   # 探针成功判据的口径说明


@dataclass(frozen=True)
class MaterialEntry:
    """一个材料族的入口：入口卡、开放内容、必须拦截项、未开放缺口。"""

    family: str
    entry_ids: tuple[str, ...]
    opened: tuple[OpenedItem, ...]
    blocked: tuple[BlockedItem, ...]
    deferred: tuple[DeferredItem, ...] = ()


# --- 探针工具 ---------------------------------------------------------------


def _entry_probe_config(**overrides: Any) -> Any:
    """条件匹配探针用的最小桩配置（只含 check_reference_conditions 读的字段）。"""
    import types

    base: dict[str, Any] = {
        "run_mode": "reference_case",
        "reference_conditions": {},
        "material_card_file": None,
        "material_id": None,
        "laser": types.SimpleNamespace(
            wavelength_m=None,
            pulse_duration_s=None,
            repetition_rate_Hz=None,
            spot_radius_m=None,
        ),
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _expect_blocked(fn: Any, code: str, what: str) -> None:
    """跑一次探针：必须抛出指定错误码，否则说明该拦截没生效。"""
    try:
        fn()
    except UFDemoError as err:
        if err.code != code:
            raise AssertionError(f"{what}：期望错误码 {code}，实际 {err.code}") from err
        return
    raise AssertionError(f"{what}：未触发拦截（期望 {code}）")


def _entry_probe_catalog(catalog: Mapping[str, MaterialSpec], mdir: str | Path, mid: str) -> MaterialSpec:
    spec = catalog.get(mid)
    if spec is None:
        raise AssertionError(f"入口卡 {mid!r} 不在材料目录中")
    return spec


def _probe_ysz_no_branch_merge(catalog: Mapping[str, MaterialSpec], mdir: str | Path) -> None:
    _expect_blocked(
        lambda: resolve_material(_entry_probe_config(material_id="zirconia_ysz"), mdir),
        MATERIAL_CAPABILITY_MISSING,
        "氧化锆：模糊名不得被解析成静态或加工分支",
    )
    static_thr = _finite_positive_error(
        _entry_probe_catalog(catalog, mdir, "zirconia_ysz_static_aps8ysz"), "zirconia_ysz_static_aps8ysz"
    )
    machining = _entry_probe_catalog(catalog, mdir, "zirconia_ysz_machining_effective_n3")
    if static_thr == machining.threshold_internal:
        raise AssertionError("氧化锆：静态分支与加工分支的阈值被合并为同一数字")
    if machining.response_semantics != SEMANTIC_EVENT_INCREMENT:
        raise AssertionError("氧化锆：加工分支的响应语义不是逐事件增量")


def _finite_positive_error(spec: MaterialSpec, name: str) -> float:
    thr = spec.threshold_internal
    if not _finite_positive(thr):
        raise AssertionError(f"{name}：缺少可用阈值（{thr!r}）")
    return float(thr)


def _probe_ysz_no_extra_incubation(catalog: Mapping[str, MaterialSpec], mdir: str | Path) -> None:
    import numpy as np

    from .response import HistoryState, build_pulse_law

    card = _entry_probe_catalog(catalog, mdir, "zirconia_ysz_machining_effective_n3")
    if card.response.get("extra_incubation_prohibited_without_refit") is not True:
        raise AssertionError("氧化锆：卡未声明禁止无重拟合的额外孵化")
    law = build_pulse_law(card)
    fluence = np.array([[2.0 * float(law.threshold_internal)]])
    history = HistoryState(exposure_count=np.zeros((1, 1), dtype=np.uint32))
    _expect_blocked(
        lambda: law.increment(fluence, history),
        RESPONSE_SEMANTICS_INVALID,
        "氧化锆：带局部历史的孵化必须显式报错（不得静默忽略、也不得默认启用）",
    )


def _probe_alsic_no_sic_card_borrow(catalog: Mapping[str, MaterialSpec], mdir: str | Path) -> None:
    from .structure import build_phase

    raw = {
        "name": "SiC_particle",
        "role": "particle",
        "material_id": SIC_CARD_ID,
        "threshold_over_F_ref": 1.0,
        "delta_over_L_ref": 0.2,
    }
    _expect_blocked(
        lambda: build_phase(raw, phase_id=1, unit=None),
        MATERIAL_CAPABILITY_MISSING,
        "铝基 SiC：颗粒相不得借用块体单晶 SiC 卡做标定",
    )


def _probe_cfrp_no_threshold_split(catalog: Mapping[str, MaterialSpec], mdir: str | Path) -> None:
    import types

    from .structure import assert_phase_threshold_not_split, build_phase

    card = _entry_probe_catalog(catalog, mdir, "cfrp_t700_yb01_800nm")
    overall_j_m2 = card.response.get("threshold_J_m2") or card.response.get("threshold_internal")
    unit = types.SimpleNamespace(mode="dimensionless", F_ref_J_m2=1.0e4)
    split = float(overall_j_m2) / float(unit.F_ref_J_m2)  # 0.84 F_ref
    phase = build_phase(
        {"name": "resin", "role": "matrix", "threshold_over_F_ref": split, "delta_over_L_ref": 0.35},
        phase_id=1,
        unit=unit,
    )
    _expect_blocked(
        lambda: assert_phase_threshold_not_split(phase, card, unit),
        MATERIAL_CAPABILITY_MISSING,
        "CFRP：整体等效阈值不得拆给树脂/纤维",
    )


def _probe_cfrp_no_depth_without_delta(catalog: Mapping[str, MaterialSpec], mdir: str | Path) -> None:
    from .response import build_pulse_law

    card = _entry_probe_catalog(catalog, mdir, "cfrp_t700_yb01_800nm")
    if _finite_positive(card.delta_m):
        raise AssertionError("CFRP：卡内出现了去除尺度 δ，前提被推翻，请重核该卡")
    _expect_blocked(
        lambda: build_pulse_law(card),
        RESPONSE_SEMANTICS_INVALID,
        "CFRP：缺 δ 时不得生成物理深度",
    )


def _probe_inconel_no_n10_as_single_pulse(catalog: Mapping[str, MaterialSpec], mdir: str | Path) -> None:
    from .thresholds import build_threshold_protocol

    card = _entry_probe_catalog(catalog, mdir, INCONEL_CARD_ID)
    for idx in (None, 0, 1):
        proto = build_threshold_protocol(card, enabled=True, candidate_index=idx)
        if proto.available:
            raise AssertionError(f"高温合金：N=10 阈值候选（candidate_index={idx}）被当作单脉冲阈值开放")
        if "多脉冲" not in (proto.reason or ""):
            raise AssertionError(f"高温合金：不可用原因未说明是多脉冲累计口径（{proto.reason!r}）")
        if proto.threshold_internal is not None:
            raise AssertionError("高温合金：多脉冲定点阈值仍被暴露为可用阈值数字")


def _probe_glass_no_grade_merge(catalog: Mapping[str, MaterialSpec], mdir: str | Path) -> None:
    _expect_blocked(
        lambda: resolve_material(_entry_probe_config(material_id="glass_ceramic"), mdir),
        MATERIAL_CAPABILITY_MISSING,
        "微晶玻璃：牌号级模糊名不得被解析成某张卡",
    )
    card = _entry_probe_catalog(catalog, mdir, "glass_ceramic_unbranded_1030nm")
    if card.is_physical():
        raise AssertionError("微晶玻璃：牌号未确认却允许物理材料预测")
    if card.identity.get("material_identity_confirmed_by_user", False):
        raise AssertionError("微晶玻璃：牌号未确认却被标为已确认")


def _probe_sic_no_mean_rate_in_event_kernel(catalog: Mapping[str, MaterialSpec], mdir: str | Path) -> None:
    card = _entry_probe_catalog(catalog, mdir, SIC_CARD_ID)
    if card.capability(CAP_EVENT_INCREMENT).available:
        raise AssertionError("SiC：平均率语义被开放为逐事件增量")
    if card.capability(CAP_MEAN_RATE).available is not True:
        raise AssertionError("SiC：平均率能力应为评估器专用可用")
    if card.capability(CAP_CUMULATIVE).available or card.capability(CAP_VOLUME_EFFICIENCY).available:
        raise AssertionError("SiC：累计/体积效率语义被误开放")

    # 体积效率曲线不得反推局部深度
    from .tables import assert_no_local_depth_from_volume, load_curves

    curves_dir = Path(mdir).parent / "curves"
    curves = {c.curve_id: c for c in load_curves(curves_dir)}
    target = next(
        (c for c in curves.values() if c.output_semantics == SEMANTIC_VOLUME_PER_ENERGY), None
    )
    if target is None:
        raise AssertionError(f"未找到体积效率示例曲线（{curves_dir}）")
    _expect_blocked(
        lambda: assert_no_local_depth_from_volume(target, "depth"),
        CONFIG_INVALID,
        "SiC：体积效率曲线不得反推局部去除深度",
    )


def _probe_sic_no_modification_as_removal(catalog: Mapping[str, MaterialSpec], mdir: str | Path) -> None:
    import dataclasses

    from .thresholds import assert_not_removal, build_threshold_protocol

    card = _entry_probe_catalog(catalog, mdir, SIC_CARD_ID)
    proto = build_threshold_protocol(card, enabled=True, candidate_index=0)
    if not proto.available:
        raise AssertionError(f"SiC：改性阈值候选本应可用（{proto.reason!r}）")
    if proto.used_for_depth:
        raise AssertionError("SiC：改性阈值被标记为参与深度更新")
    if proto.output_semantics != SEMANTIC_THRESHOLD_ONLY:
        raise AssertionError("SiC：改性阈值的响应语义不是 threshold_only")
    tampered = dataclasses.replace(proto, used_for_depth=True)
    _expect_blocked(
        lambda: assert_not_removal(tampered),
        RESPONSE_SEMANTICS_INVALID,
        "SiC：篡改为「参与深度」的阈值协议必须被断言拦下",
    )


def _probe_diamond_no_pulsewidth_mix(catalog: Mapping[str, MaterialSpec], mdir: str | Path) -> None:
    from .config import check_reference_conditions

    a = _entry_probe_catalog(catalog, mdir, "diamond_scd_cvd_1030nm_400fs")
    b = _entry_probe_catalog(catalog, mdir, "diamond_scd_cvd_1030nm_700fs")
    if a.threshold_internal == b.threshold_internal:
        raise AssertionError("金刚石：400 fs 与 700 fs 分支的阈值被合并")
    cfg = _entry_probe_config(
        reference_conditions={"fluence_basis": a.response.get("fluence_basis")}
    )
    cfg.laser.pulse_duration_s = 7.0e-13  # 用 700 fs 条件跑 400 fs 卡
    errs = check_reference_conditions(cfg, a)
    if not any(e.code == CONDITION_MISMATCH for e in errs):
        raise AssertionError("金刚石：400/700 fs 条件不匹配未被拒绝")


def _probe_diamond_unconfirmed_not_default(catalog: Mapping[str, MaterialSpec], mdir: str | Path) -> None:
    from .config import check_reference_conditions

    card = _entry_probe_catalog(catalog, mdir, "diamond_scd_cvd_1030nm_pulsewidth_unconfirmed")
    if card.enabled_by_default:
        raise AssertionError("金刚石：脉宽未核实的候选值被设为默认启用")
    errs = check_reference_conditions(_entry_probe_config(), card)
    if not any(e.code == CONDITION_MISMATCH for e in errs):
        raise AssertionError("金刚石：默认禁用的候选值在参考模式未被拒绝")


def _probe_diamond_synthetic_not_supported(
    catalog: Mapping[str, MaterialSpec], mdir: str | Path
) -> None:
    """证明金刚石的"合成形貌"**当前确实打不开**（规格要求开放，实现未支持）。

    判据（两者都要成立才算"确实打不开"）：
    1. 三张金刚石卡都没有 ``synthetic_structure`` 能力；
    2. 用卡自身的 ``structure_type`` 去构建结构，被结构构建器拒绝（``CONFIG_INVALID``）。
    """
    from .structure import load_structure

    for mid in (
        "diamond_scd_cvd_1030nm_400fs",
        "diamond_scd_cvd_1030nm_700fs",
        "diamond_scd_cvd_1030nm_pulsewidth_unconfirmed",
    ):
        card = _entry_probe_catalog(catalog, mdir, mid)
        cap = card.capability(CAP_SYNTHETIC_STRUCTURE)
        if cap.available:
            raise AssertionError(
                f"金刚石：{mid} 已具备合成结构能力，缺口已闭合——请把入口项从 deferred 移回 opened"
            )
    card = _entry_probe_catalog(catalog, mdir, "diamond_scd_cvd_1030nm_400fs")

    class _Stub:
        structure_type = card.structure_type
        phases: tuple[Any, ...] = ()
        layers: tuple[Any, ...] = ()
        particles: Any = None
        seed = 0
        target_volume_fraction = None

    class _Cfg:
        structure = _Stub()
        unit = None
        grid = None

    _expect_blocked(
        lambda: load_structure(_Cfg(), parent_material=card),
        CONFIG_INVALID,
        "金刚石：结构类型 net_removal_with_optional_modification_mask 未被结构构建器支持",
    )


SIC_CARD_ID = "sic_4h_cface_1035nm_multishot"
INCONEL_CARD_ID = "inconel718_1030nm_n10"

_ENTRY_PROBES: dict[str, Any] = {
    "ysz_no_branch_merge": _probe_ysz_no_branch_merge,
    "ysz_no_extra_incubation": _probe_ysz_no_extra_incubation,
    "alsic_no_sic_card_borrow": _probe_alsic_no_sic_card_borrow,
    "cfrp_no_threshold_split": _probe_cfrp_no_threshold_split,
    "cfrp_no_depth_without_delta": _probe_cfrp_no_depth_without_delta,
    "inconel_no_n10_as_single_pulse": _probe_inconel_no_n10_as_single_pulse,
    "glass_no_grade_merge": _probe_glass_no_grade_merge,
    "sic_no_mean_rate_in_event_kernel": _probe_sic_no_mean_rate_in_event_kernel,
    "sic_no_modification_as_removal": _probe_sic_no_modification_as_removal,
    "diamond_no_pulsewidth_mix": _probe_diamond_no_pulsewidth_mix,
    "diamond_unconfirmed_not_default": _probe_diamond_unconfirmed_not_default,
    "diamond_synthetic_not_supported": _probe_diamond_synthetic_not_supported,
}


MATERIAL_ENTRIES: tuple[MaterialEntry, ...] = (
    MaterialEntry(
        family="氧化锆",
        entry_ids=("zirconia_ysz_machining_effective_n3", "zirconia_ysz_static_aps8ysz"),
        opened=(
            OpenedItem(
                "YSZ 特定加工核（逐事件，条件受限）",
                capability=CAP_EVENT_INCREMENT,
                note="加工有效阈值只在文献加工分支条件匹配的岗位上可用（conditional）。",
            ),
            OpenedItem(
                "YSZ 参考算例与公式核查",
                run_mode="reference_case",
                note="参考评估器只复现公式与协议量，不求解网格。",
            ),
            OpenedItem("条件对应阈值展示", capability=CAP_THRESHOLD),
        ),
        blocked=(
            BlockedItem(
                "合并静态/加工分支",
                MATERIAL_CAPABILITY_MISSING,
                "materials.resolve_material",
                "ysz_no_branch_merge",
                "模糊/前缀名不被解析；两分支语义与阈值分别保留。",
            ),
            BlockedItem(
                "默认额外孵化",
                RESPONSE_SEMANTICS_INVALID,
                "response.FixedThresholdLogLaw.increment",
                "ysz_no_extra_incubation",
                "带局部历史的核显式报错；卡内声明 extra_incubation_prohibited_without_refit。",
            ),
        ),
    ),
    MaterialEntry(
        family="铝基碳化硅",
        entry_ids=("alsic_sicp_aa2024_1030nm",),
        opened=(
            OpenedItem(
                "无量纲合成颗粒",
                capability=CAP_SYNTHETIC_STRUCTURE,
                note="颗粒几何与分相响应内联合成定义；资料浏览只读卡内来源与证据状态。",
            ),
        ),
        blocked=(
            BlockedItem(
                "借用单晶 SiC 当颗粒相标定",
                MATERIAL_CAPABILITY_MISSING,
                "structure.build_phase",
                "alsic_no_sic_card_borrow",
                "相引用其它材料卡（material_id）时直接拒绝。",
            ),
        ),
    ),
    MaterialEntry(
        family="CFRP",
        entry_ids=("cfrp_t700_yb01_800nm",),
        opened=(
            OpenedItem(
                "协议限定整体阈值",
                capability=CAP_THRESHOLD,
                note="整体等效阈值（Fth1）只在协议条件下展示，不拆给分相。",
            ),
            OpenedItem("合成铺层", capability=CAP_SYNTHETIC_STRUCTURE),
        ),
        blocked=(
            BlockedItem(
                "整体阈值拆给树脂/纤维",
                MATERIAL_CAPABILITY_MISSING,
                "structure.assert_phase_threshold_not_split",
                "cfrp_no_threshold_split",
                "相阈值等于父卡整体阈值时拒绝。",
            ),
            BlockedItem(
                "缺 δ 生成物理深度",
                RESPONSE_SEMANTICS_INVALID,
                "response.build_pulse_law",
                "cfrp_no_depth_without_delta",
                "δ 为 null 时构造脉冲律失败，不得由阈值反推深度。",
            ),
        ),
    ),
    MaterialEntry(
        family="高温合金",
        entry_ids=(INCONEL_CARD_ID,),
        opened=(
            OpenedItem(
                "In718 的 N=10 阈值参考",
                capability=CAP_THRESHOLD,
                note="必须同时显示 N 的定义；不得当作单脉冲阈值。",
            ),
            OpenedItem("合成形貌", capability=CAP_SYNTHETIC_STRUCTURE),
        ),
        blocked=(
            BlockedItem(
                "当作单脉冲阈值或任意历史阈值",
                "UNAVAILABLE",
                "thresholds._declare_multipulse",
                "inconel_no_n10_as_single_pulse",
                "多脉冲（N≥2）定点口径的候选与卡内阈值一律判不可用。",
            ),
        ),
    ),
    MaterialEntry(
        family="微晶玻璃",
        entry_ids=("glass_ceramic_unbranded_1030nm",),
        opened=(
            OpenedItem("合成均质表面", capability=CAP_SYNTHETIC_STRUCTURE),
            OpenedItem("导入入口", capability=CAP_TABLE_IMPORT),
        ),
        blocked=(
            BlockedItem(
                "将不同玻璃陶瓷牌号合并",
                MATERIAL_CAPABILITY_MISSING,
                "materials.resolve_material",
                "glass_no_grade_merge",
                "牌号级模糊名不解析；牌号未确认时不允许物理材料预测。",
            ),
        ),
    ),
    MaterialEntry(
        family="SiC",
        entry_ids=(SIC_CARD_ID,),
        opened=(
            OpenedItem(
                "两类阈值（改性 / 结构变化）",
                capability=CAP_THRESHOLD,
                note="多候选必须显式选择 candidate_index，不静默取默认。",
            ),
            OpenedItem("有效 N 参考评估器", capability=CAP_REFERENCE_EVALUATOR),
            OpenedItem("平均去除率（仅评估器）", capability=CAP_MEAN_RATE),
        ),
        blocked=(
            BlockedItem(
                "平均率直接逐事件累加",
                CONFIG_INVALID,
                "tables.assert_no_local_depth_from_volume",
                "sic_no_mean_rate_in_event_kernel",
                "非 event_depth_increment 曲线不得生成局部深度；逐事件能力不开放。",
            ),
            BlockedItem(
                "改性量当去除量",
                RESPONSE_SEMANTICS_INVALID,
                "thresholds.assert_not_removal",
                "sic_no_modification_as_removal",
                "阈值协议 used_for_depth 恒为 False；篡改后被断言拦下。",
            ),
        ),
    ),
    MaterialEntry(
        family="金刚石",
        entry_ids=(
            "diamond_scd_cvd_1030nm_400fs",
            "diamond_scd_cvd_1030nm_700fs",
            "diamond_scd_cvd_1030nm_pulsewidth_unconfirmed",
        ),
        opened=(
            OpenedItem(
                "条件对应阈值",
                capability=CAP_THRESHOLD,
                note="400 fs 与 700 fs 分卡保存，各自的协议条件不同。",
            ),
            OpenedItem("导入入口", capability=CAP_TABLE_IMPORT),
        ),
        deferred=(
            DeferredItem(
                "合成形貌",
                "卡内 structure_type = net_removal_with_optional_modification_mask 未被结构构建器"
                "（structure.STRUCTURE_TYPES）支持，且无逐事件去除核，故当前无法生成合成形貌。"
                "细则第 7 节要求开放；缺口需在 M2 放行前决策（扩展结构类型／改经共享无量纲卡 "
                f"{ENTRY_SYNTHETIC_DEMO_CARD}／修订规格）。",
                "diamond_synthetic_not_supported",
                "三张金刚石卡均无 synthetic_structure 能力，且该结构类型被 load_structure 拒绝（CONFIG_INVALID）。",
            ),
        ),
        blocked=(
            BlockedItem(
                "混合 400/700 fs",
                CONDITION_MISMATCH,
                "config.check_reference_conditions",
                "diamond_no_pulsewidth_mix",
                "脉宽不匹配时拒绝参考定量执行。",
            ),
            BlockedItem(
                "未确认脉宽候选值默认启用",
                CONDITION_MISMATCH,
                "config.check_reference_conditions",
                "diamond_unconfirmed_not_default",
                "enabled_by_default=false 的候选值在参考模式被拒绝。",
            ),
        ),
    ),
)


def _run_entry_probe(
    family: str, kind: str, item: str, enforcement: str, code: str, probe_key: str,
    expectation: str, catalog: Mapping[str, MaterialSpec], material_dir: str | Path,
) -> dict[str, Any]:
    """跑一条探针并给出结论行（探针缺失/失败一律如实标 ok=False）。"""
    probe = _ENTRY_PROBES.get(probe_key)
    if probe is None:
        return {
            "family": family, "kind": kind, "item": item, "enforcement": enforcement,
            "code": code, "ok": False, "detail": f"未登记的探针：{probe_key}",
        }
    try:
        probe(catalog, material_dir)
    except Exception as exc:  # noqa: BLE001 - 探针失败必须如实上报
        return {
            "family": family, "kind": kind, "item": item, "enforcement": enforcement,
            "code": code, "ok": False, "detail": f"{type(exc).__name__}: {exc}",
        }
    return {
        "family": family, "kind": kind, "item": item, "enforcement": enforcement,
        "code": code, "ok": True, "detail": expectation,
    }


def verify_entry_enforcements(
    catalog: Mapping[str, MaterialSpec], material_dir: str | Path
) -> list[dict[str, Any]]:
    """逐条**实跑**入口的「必须拦截」探针、「未开放缺口」探针与「开放内容」依据。"""
    rows: list[dict[str, Any]] = []
    for entry in MATERIAL_ENTRIES:
        for b in entry.blocked:
            rows.append(
                _run_entry_probe(
                    entry.family, "blocked", b.item, b.enforcement, b.code,
                    b.probe_key, b.expectation, catalog, material_dir,
                )
            )
        for d in entry.deferred:
            rows.append(
                _run_entry_probe(
                    entry.family, "deferred", d.item, d.reason, "缺口（规格要求开放、实现未支持）",
                    d.probe_key, d.expectation, catalog, material_dir,
                )
            )
        for o in entry.opened:
            ok = False
            detail = "未绑定依据"
            if o.capability:
                hit = [
                    mid
                    for mid in entry.entry_ids
                    if catalog.get(mid) is not None
                    and catalog[mid].capability(o.capability).available
                ]
                ok = bool(hit)
                detail = f"能力 {o.capability} 可用卡：{hit}" if ok else f"没有入口卡提供能力 {o.capability}"
            elif o.run_mode:
                hit = [
                    mid
                    for mid in entry.entry_ids
                    if catalog.get(mid) is not None and o.run_mode in catalog[mid].allowed_run_modes
                ]
                ok = bool(hit)
                detail = f"允许 {o.run_mode} 的卡：{hit}" if ok else f"没有入口卡允许 {o.run_mode}"
            rows.append(
                {
                    "family": entry.family,
                    "kind": "opened",
                    "item": o.item,
                    "enforcement": o.verified_by(),
                    "code": "-",
                    "ok": ok,
                    "detail": detail,
                }
            )
    return rows


def material_entry_rows(
    catalog: Mapping[str, MaterialSpec], material_dir: str | Path
) -> list[dict[str, Any]]:
    """七材料能力入口表（机器可读；供 G09 报告与界面展示）。"""
    verdict = {
        (r["family"], r["kind"], r["item"]): r
        for r in verify_entry_enforcements(catalog, material_dir)
    }
    rows: list[dict[str, Any]] = []
    for entry in MATERIAL_ENTRIES:
        missing = [mid for mid in entry.entry_ids if mid not in catalog]
        for o in entry.opened:
            v = verdict.get((entry.family, "opened", o.item), {})
            rows.append(
                {
                    "family": entry.family,
                    "entry_ids": ";".join(entry.entry_ids),
                    "entry_cards_present": not missing,
                    "kind": "opened",
                    "item": o.item,
                    "binding": o.verified_by(),
                    "code": "-",
                    "verified": v.get("ok"),
                    "detail": v.get("detail"),
                    "note": o.note,
                }
            )
        for d in entry.deferred:
            v = verdict.get((entry.family, "deferred", d.item), {})
            rows.append(
                {
                    "family": entry.family,
                    "entry_ids": ";".join(entry.entry_ids),
                    "entry_cards_present": not missing,
                    "kind": "deferred",
                    "item": d.item,
                    "binding": f"缺口：{d.reason}",
                    "code": "缺口（规格要求开放、实现未支持）",
                    "verified": v.get("ok"),
                    "detail": v.get("detail"),
                    "note": d.expectation,
                }
            )
        for b in entry.blocked:
            v = verdict.get((entry.family, "blocked", b.item), {})
            rows.append(
                {
                    "family": entry.family,
                    "entry_ids": ";".join(entry.entry_ids),
                    "entry_cards_present": not missing,
                    "kind": "blocked",
                    "item": b.item,
                    "binding": b.enforcement,
                    "code": b.code,
                    "verified": v.get("ok"),
                    "detail": v.get("detail"),
                    "note": b.expectation,
                }
            )
    return rows
