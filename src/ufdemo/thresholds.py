"""受限阈值协议（批次 H / T14）。

**为什么叫"受限"**：超阈标记（超阈值/改性标记）是本项目最容易出问题的观测量——
它和「去除量」看起来都是"某处是否发生了变化"，但物理含义完全不同。任务书
6.3/7 节与执行细则 11.3 反复划线：

* SiC 的**改性标记不等于已去除体积**；
* CFRP 的**整体阈值不能拆给树脂/纤维**；
* 高温合金的 N=10 阈值**不能当作单脉冲阈值**；
* 阈值标记**不是热影响区**，照射剂量**不是温度**。

因此本模块把该观测量收在一个显式协议里，并把四条硬约束写成断言（配置层拦截，
不只靠界面禁用）：

1. **只能对"本事件的入射能流"判超阈**：绝不允许拿 ``cumulative_fluence``（累计
   入射剂量）去和单脉冲阈值比。两者量纲虽同，物理含义不同（``assert_basis_is_event``）。
2. **阈值口径必须是单脉冲口径**：卡里若声明 ``fixed_point_N10``（N=10 定点阈值）
   或候选名里出现 ``N≥2``，一律判为不可用（``_declare_multipulse``）——这正是
   "高温合金 N=10 阈值不能当单脉冲阈值"的红线。
3. **多个条件对应阈值必须显式选择**：卡里有两个候选（如 SiC 的 2.35 / 4.97 J/cm²）
   时不静默取默认，而是返回不可用并要求 ``threshold_protocol.candidate_index``。
4. **不产生深度**：协议输出只有掩膜与计数，永不参与高度场更新
   （``assert_not_removal``）。

只有协议 ``available`` 时界面才提供 ``threshold_mask`` 图层；否则沿用
``UNAVAILABLE_LAYERS`` 的口径如实报不可用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .config import SEMANTIC_THRESHOLD_ONLY
from .errors import CONFIG_INVALID, RESPONSE_SEMANTICS_INVALID, UFDemoError

# 允许的能流基准：只有"本事件入射能流"。累计剂量不在其中——这是刻意的。
THRESHOLD_FLUENCE_BASES: tuple[str, ...] = ("per_event_incident",)

# 明确拒绝的基准名（给出可读原因，而不是含糊的"非法取值"）
_REJECTED_BASES: dict[str, str] = {
    "cumulative_fluence": "累计入射剂量≠单脉冲峰值能流；两者量纲相同但不可比较",
    "cumulative_dose": "累计入射剂量≠单脉冲峰值能流；两者量纲相同但不可比较",
    "mean_fluence": "平均值不能用于逐事件超阈判定",
    "total_fluence": "累计量不能用于逐事件超阈判定",
}

# 阈值口径判定：只有"单脉冲口径"能用于本事件超阈比较。
# 依据卡里**显式声明的口径名**，而不是数值大小——数值大小无法区分这两类阈值。
_MULTIPULSE_KIND_PREFIXES: tuple[str, ...] = (
    "fixed_point_n",          # 高温合金：N=10 定点阈值（红线）
    "multipulse_cumulative",
    "cumulative",
)
_PER_EVENT_KIND_PREFIXES: tuple[str, ...] = (
    "single_pulse",
    "machining_effective",    # 逐事件增量律里的阈值，按事件判定
)
# 口径名里出现 ``N<k>``（k>=2）→ 多脉冲累计口径。``Fth1``/``n1`` 是第 1 发端点，放行。
_MULTIPULSE_N_RE = re.compile(r"(?<![a-z0-9])n(\d+)")


@dataclass
class ThresholdProtocol:
    """一次运行实际采用的阈值观测协议。"""

    output_semantics: str
    available: bool
    reason: str | None
    observable_name: str
    fluence_basis: str
    threshold_internal: float | None = None
    threshold_label: str | None = None
    # 阈值口径（卡内声明）：single_pulse* / machining_effective 属单脉冲口径；
    # fixed_point_N10 等多脉冲定点口径会被拦下而不可用。
    threshold_kind: str | None = None
    condition: Mapping[str, Any] = field(default_factory=dict)
    source_kind: str = "response"          # response | threshold_candidate
    source_index: int | None = None
    n_candidates: int = 0
    evidence_status: str | None = None
    restricted: bool = True
    notes: tuple[str, ...] = ()
    used_for_depth: bool = False           # 恒为 False；保留字段便于外部核对

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_semantics": self.output_semantics,
            "available": self.available,
            "reason": self.reason,
            "observable": self.observable_name,
            "fluence_basis": self.fluence_basis,
            "threshold_internal": self.threshold_internal,
            "threshold_label": self.threshold_label,
            "threshold_kind": self.threshold_kind,
            "condition": dict(self.condition),
            "source_kind": self.source_kind,
            "source_index": self.source_index,
            "n_candidates": self.n_candidates,
            "evidence_status": self.evidence_status,
            "restricted": self.restricted,
            "used_for_depth": False,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 硬约束断言（配置层拦截）
# ---------------------------------------------------------------------------


def assert_basis_is_event(fluence_basis: Any, *, field_path: str = "threshold_protocol.fluence_basis") -> str:
    """基准必须是"本事件入射能流"。累计/平均基准一律拒绝。"""
    if fluence_basis in _REJECTED_BASES:
        raise UFDemoError(
            CONFIG_INVALID,
            f"阈值协议的能流基准 {fluence_basis!r} 不可用",
            field_path=field_path,
            actual=fluence_basis,
            requirement=f"取值属于 {list(THRESHOLD_FLUENCE_BASES)}",
            suggestion=_REJECTED_BASES[str(fluence_basis)] + "。请改用 per_event_incident。",
        )
    if fluence_basis not in THRESHOLD_FLUENCE_BASES:
        raise UFDemoError(
            CONFIG_INVALID,
            f"阈值协议的能流基准 {fluence_basis!r} 未登记",
            field_path=field_path,
            actual=fluence_basis,
            requirement=f"取值属于 {list(THRESHOLD_FLUENCE_BASES)}",
        )
    return str(fluence_basis)


def assert_not_removal(protocol: ThresholdProtocol, *, field_path: str = "threshold_protocol") -> None:
    """阈值协议不得被当作去除量使用（结构上不可能产生深度）。"""
    if protocol.used_for_depth:
        raise UFDemoError(
            RESPONSE_SEMANTICS_INVALID,
            "阈值协议被标记为参与深度更新",
            field_path=field_path,
            actual=True,
            requirement="used_for_depth 恒为 False",
            suggestion="阈值是分类观测，不产生去除量：改性标记≠已去除体积。",
        )
    if protocol.output_semantics != SEMANTIC_THRESHOLD_ONLY:
        raise UFDemoError(
            RESPONSE_SEMANTICS_INVALID,
            "阈值协议的响应语义必须是 threshold_only",
            field_path=f"{field_path}.output_semantics",
            actual=protocol.output_semantics,
            requirement=SEMANTIC_THRESHOLD_ONLY,
            suggestion="阈值观测不得声明为逐事件增量，否则会与深度核混淆。",
        )


# ---------------------------------------------------------------------------
# 协议构造
# ---------------------------------------------------------------------------


def _finite_positive(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 and float(v) == float(v)


def _declare_multipulse(*texts: Any) -> str | None:
    """口径名是否声明了"多脉冲（N>=2）累计"——命中则返回证据串。

    这是"高温合金 N=10 阈值不能当作单脉冲阈值"那条红线的机器化版本：
    ``fixed_point_N10`` / ``threshold_at_N10_green`` 会被拦下，而
    ``single_pulse_modification_threshold`` / ``multipulse_fitted_Fth1``（第 1 发
    端点）与 ``sic4h_n1_modification``（N=1）放行。
    """
    for t in texts:
        if not isinstance(t, str):
            continue
        for m in _MULTIPULSE_N_RE.finditer(t.lower()):
            if int(m.group(1)) >= 2:
                return t
    for t in texts:
        if isinstance(t, str) and any(
            t.lower().startswith(p) for p in _MULTIPULSE_KIND_PREFIXES
        ):
            return t
    return None


def _declares_per_event_kind(kind: Any) -> bool:
    return isinstance(kind, str) and any(
        kind.lower().startswith(p) for p in _PER_EVENT_KIND_PREFIXES
    )


def build_threshold_protocol(
    material: Any,
    *,
    enabled: bool = False,
    candidate_index: int | None = None,
    observable_name: str = "fluence_above_threshold",
    fluence_basis: str = "per_event_incident",
) -> ThresholdProtocol:
    """由材料卡构造受限阈值协议（公开入口）。

    无论 ``enabled`` 与否都会先做**配置层**校验（非法基准一律报错）；随后：

    * ``enabled=False``：即使材料卡有可用阈值，也返回 ``available=False`` 并给出
      「未开启」原因——界面据实报不可用，**不返回全 0 假数组**；
    * ``enabled=True``：按材料卡解析；多阈值候选且未显式指定 ``candidate_index``
      时同样返回不可用并说明原因（SiC 改性阈值≠去除阈值，不静默取默认）。
    """
    proto = _resolve_threshold_protocol(
        material,
        candidate_index=candidate_index,
        observable_name=observable_name,
        fluence_basis=fluence_basis,
    )
    if not enabled and proto.available:
        proto.available = False
        proto.reason = (
            "阈值协议未开启（threshold_protocol.enabled=false）：本次运行不记录超阈标记，"
            "界面据实报不可用，而不是返回全 0 假数组。"
        )
        proto.notes = (
            *proto.notes,
            "材料卡本身提供可用阈值；本次运行只是未开启记录，可开启后重跑。",
        )
    assert_not_removal(proto)
    return proto


def _resolve_threshold_protocol(
    material: Any,
    *,
    candidate_index: int | None = None,
    observable_name: str = "fluence_above_threshold",
    fluence_basis: str = "per_event_incident",
) -> ThresholdProtocol:
    """按材料卡解析阈值协议（不看 enabled，只管材料卡能不能支撑这个观测量）。"""
    basis = assert_basis_is_event(fluence_basis)
    response = dict(getattr(material, "response", {}) or {})
    candidates = tuple(getattr(material, "threshold_candidates", ()) or ())
    evidence = getattr(material, "evidence_status", None)

    usable: list[tuple[int, Mapping[str, Any]]] = []
    rejected: list[tuple[int, Mapping[str, Any], str]] = []
    for i, c in enumerate(candidates):
        if not _finite_positive(c.get("threshold_J_m2")):
            continue
        evidence_text = _declare_multipulse(
            c.get("observable_name"), c.get("branch_id"), c.get("label")
        )
        if evidence_text is None:
            usable.append((i, c))
        else:
            rejected.append((i, c, evidence_text))

    single = getattr(material, "threshold_internal", None)
    single_kind = response.get("threshold_kind")
    single_multipulse = _declare_multipulse(single_kind)
    single_usable = _finite_positive(single) and single_multipulse is None

    base_note = (
        "受限协议：只对**本事件入射能流**判超阈，不使用累计剂量；"
        "该观测为分类标记，不产生去除量，也不代表任何热学量。"
    )

    def _unavailable(reason: str, *, label=None, thr=None, src_kind="response", idx=None,
                     condition=None, notes=(), kind=None, n_cand=None) -> ThresholdProtocol:
        p = ThresholdProtocol(
            output_semantics=SEMANTIC_THRESHOLD_ONLY,
            available=False,
            reason=reason,
            observable_name=observable_name,
            fluence_basis=basis,
            threshold_internal=thr,
            threshold_label=label,
            condition=dict(condition or {}),
            source_kind=src_kind,
            source_index=idx,
            n_candidates=len(usable) if n_cand is None else n_cand,
            evidence_status=evidence,
            threshold_kind=kind,
            notes=(base_note, *notes),
        )
        assert_not_removal(p)
        return p

    def _resolved(*, observable, thr, label, kind, condition, src_kind, idx, n_cand, notes=()) -> ThresholdProtocol:
        p = ThresholdProtocol(
            output_semantics=SEMANTIC_THRESHOLD_ONLY,
            available=True,
            reason=None,
            observable_name=observable,
            fluence_basis=basis,
            threshold_internal=float(thr),
            threshold_label=label,
            condition=dict(condition or {}),
            source_kind=src_kind,
            source_index=idx,
            n_candidates=n_cand,
            evidence_status=evidence,
            threshold_kind=kind,
            notes=(base_note, *notes),
        )
        assert_not_removal(p)
        return p

    # 卡里没有任何可用阈值（null 不得按 0 处理）
    if not usable and not single_usable:
        if rejected and not _finite_positive(single):
            listing = "、".join(
                f"#{i}:{c.get('observable_name') or c.get('branch_id')}" for i, c, _ in rejected
            )
            return _unavailable(
                f"该材料的 {len(rejected)} 个阈值候选均为**多脉冲（N≥2）累计口径**（{listing}），"
                "不能与本事件的单脉冲能流比较——「N 次脉冲阈值」不是「单脉冲阈值」。"
                "如需该口径，请另开一个明确的多脉冲观测量，而不是复用它。",
                n_cand=len(candidates),
                notes=("多脉冲定点阈值被拦下：这是高温合金 N=10 那条红线的机器化检查。",),
            )
        if single_multipulse is not None:
            return _unavailable(
                f"材料卡声明的阈值口径 {single_kind!r} 是多脉冲（N≥2）累计口径，"
                "不能与本事件的单脉冲能流比较；缺失保持 null，也不按 0 处理。",
                kind=single_kind if isinstance(single_kind, str) else None,
                n_cand=len(candidates),
            )
        return _unavailable(
            "材料卡未提供可用阈值（缺失保持 null，不按 0 处理）；阈值展示也不开放",
            kind=single_kind if isinstance(single_kind, str) else None,
            n_cand=len(candidates),
        )

    # 多候选：必须显式选择，且不允许越界
    if len(usable) > 1:
        if candidate_index is None:
            labels = "、".join(
                str(c.get("label") or c.get("observable_name") or c.get("branch_id") or f"#{i}")
                for i, c in usable
            )
            return _unavailable(
                f"存在 {len(usable)} 个条件对应阈值候选（{labels}），必须显式选择 candidate_index；"
                "不静默取默认，也不把改性阈值当去除阈值。",
                n_cand=len(usable),
                notes=("多候选阈值必须由调用方显式选择，避免把两类观测量混为一谈。",),
            )
        if not isinstance(candidate_index, int) or isinstance(candidate_index, bool):
            raise UFDemoError(
                CONFIG_INVALID,
                "candidate_index 必须是整数",
                field_path="threshold_protocol.candidate_index",
                actual=candidate_index,
                requirement="0 <= index < 候选数",
            )
        if not (0 <= candidate_index < len(usable)):
            raise UFDemoError(
                CONFIG_INVALID,
                "candidate_index 超出候选范围",
                field_path="threshold_protocol.candidate_index",
                actual=candidate_index,
                requirement=f"0 <= index < {len(usable)}",
                suggestion=f"卡内可用候选：{[i for i, _ in usable]}",
            )
        idx, cand = usable[candidate_index]
        return _resolved(
            observable=str(cand.get("observable_name") or observable_name),
            thr=cand["threshold_J_m2"],
            label=str(cand.get("label") or f"候选 #{idx}"),
            kind=str(cand.get("observable_name") or "") or None,
            condition=cand.get("condition") or {},
            src_kind="threshold_candidate",
            idx=idx,
            n_cand=len(usable),
            notes=(
                "该阈值来自卡内条件对应候选，界面上必须同时显示其可观测量名称与条件。",
            ),
        )

    # 单候选
    if len(usable) == 1:
        idx, cand = usable[0]
        return _resolved(
            observable=str(cand.get("observable_name") or observable_name),
            thr=cand["threshold_J_m2"],
            label=str(cand.get("label") or "唯一条件对应阈值"),
            kind=str(cand.get("observable_name") or "") or None,
            condition=cand.get("condition") or {},
            src_kind="threshold_candidate",
            idx=idx,
            n_cand=1,
        )

    # 退回卡内单阈值（口径已确认不是多脉冲）
    single_notes: tuple[str, ...] = ()
    if single_kind is not None and not _declares_per_event_kind(single_kind):
        single_notes = (f"卡内声明口径 {single_kind!r} 未登记为单脉冲口径，请注意其条件。",)
    return _resolved(
        observable=observable_name,
        thr=single,
        label="卡内 response 阈值",
        kind=single_kind if isinstance(single_kind, str) else None,
        condition={},
        src_kind="response",
        idx=None,
        n_cand=0,
        notes=single_notes,
    )


def classify_exceedance(protocol: ThresholdProtocol, fluence: Any, mask: Any | None = None) -> Any | None:
    """对**本事件**能流做超阈分类，返回 bool 数组；协议不可用或形状为空时返回 None。"""
    import numpy as np

    assert_not_removal(protocol)
    if not protocol.available or protocol.threshold_internal is None:
        return None
    arr = np.asarray(fluence, dtype=np.float64)
    if arr.size == 0:
        return None
    if not np.all(np.isfinite(arr)):
        raise UFDemoError(
            RESPONSE_SEMANTICS_INVALID,
            "阈值分类输入含 NaN/Inf",
            field_path="threshold_protocol.fluence",
            actual="non-finite",
        )
    out = arr > float(protocol.threshold_internal)
    if mask is not None:
        m = np.asarray(mask, dtype=bool)
        if m.shape != out.shape:
            raise UFDemoError(
                CONFIG_INVALID,
                "阈值分类的照射掩膜形状与能流不一致",
                field_path="threshold_protocol.mask",
                actual=list(m.shape),
                requirement=f"与能流形状 {list(out.shape)} 一致",
            )
        out = out & m
    return out


def reject_cumulative_flux_arg(name: str) -> None:
    """给调用方一个明确的失败入口：任何试图把累计量传进阈值分类的写法都报错。

    存在的意义是让"错误用法"有一个可被测试触发的、具名的拒绝路径，
    而不是靠代码评审口口相传。
    """
    raise UFDemoError(
        CONFIG_INVALID,
        f"阈值协议不接受 {name}",
        field_path=f"threshold_protocol.{name}",
        actual=name,
        requirement="每次判定必须传入本事件入射能流",
        suggestion="累计剂量与单脉冲阈值不可比较；如需累计观测量请另开一个明确的观测量，不要复用本协议。",
    )
