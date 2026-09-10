"""逐事件响应核、阈值与响应语义（执行细则 4.3、5.2 第 5–6 步、第 7 节）。

固定阈值对数去除核（任务书 6.3，实现时不得自行变更）::

    a(F) = delta * [ln(F / F_th)]_+

``F <= F_th`` 返回 0；计算时**先掩膜后取对数**，避免 ``ln(0)``。

语义闸门：主循环入口只接受 ``event_depth_increment``，拒绝平均率、累计深度、
轨道深度和体积效率——即使数组形状吻合也不能放行。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from .config import (
    INCREMENT_SEMANTICS,
    SEMANTIC_EVENT_INCREMENT,
    SEMANTIC_THRESHOLD_ONLY,
)
from .errors import NUMERIC_NONFINITE, RESPONSE_SEMANTICS_INVALID, UFDemoError

# 响应语义的中文说明（供界面与报告使用；阈值图不得写“热影响区”）
SEMANTIC_ZH: dict[str, str] = {
    "event_depth_increment": "一次事件的局部去除增量（可直接加到表面）",
    "mean_depth_per_effective_pulse": "给定历史下的平均去除率（不可直接加）",
    "cumulative_depth": "某一完整照射协议的累计深度（不可重复累加）",
    "track_or_pass_depth": "单轨/单遍加工结果（不可再按脉冲重复应用）",
    "volume_per_energy": "总体体积效率（不可唯一确定局部坑形）",
    "threshold_only": "阈值/分类观测（不产生深度）",
}


def assert_increment_semantics(semantics: Any, *, field_path: str = "response.output_semantics") -> str:
    """语义闸门。仅 ``event_depth_increment`` 可以通过。"""
    if semantics not in INCREMENT_SEMANTICS:
        zh = SEMANTIC_ZH.get(str(semantics), "未登记的响应语义")
        raise UFDemoError(
            RESPONSE_SEMANTICS_INVALID,
            f"响应语义 {semantics!r} 不能进入逐事件增量主循环",
            field_path=field_path,
            actual=semantics,
            requirement=f"取值属于 {list(INCREMENT_SEMANTICS)}",
            suggestion=f"该语义含义为「{zh}」。平均/累计/轨道/体积曲线请走参考评估器（references.py）。",
        )
    return str(semantics)


@dataclass
class IncrementResult:
    """逐事件响应的候选去除量。

    细则 4.3：必须带 ``output_semantics=event_depth_increment``、``depth_direction``
    和单位模式。
    """

    output_semantics: str
    depth_direction: str
    unit_mode: str
    values: Any | None = None  # (ny_win, nx_win) 候选去除量，内部单位
    available: bool = True
    reason: str | None = None
    modification_mask: Any | None = None
    history: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """细则 5.2 第 6 步：校验语义、方向、非负性和有限性。"""
        import numpy as np

        assert_increment_semantics(self.output_semantics)
        if not self.depth_direction:
            raise UFDemoError(
                RESPONSE_SEMANTICS_INVALID,
                "缺少 depth_direction",
                field_path="response.depth_direction",
                actual=None,
                requirement="surface_normal / vertical_height / not_applicable",
                suggestion="在材料卡与 IncrementResult 中明确深度方向。",
            )
        if self.values is None:
            return
        arr = np.asarray(self.values)
        if not np.all(np.isfinite(arr)):
            raise UFDemoError(
                NUMERIC_NONFINITE,
                "响应输出包含 NaN/Inf",
                field_path="response.values",
                actual="non-finite",
                suggestion="修正核实现；不得用 nan_to_num 隐藏问题。",
            )
        if np.any(arr < 0.0):
            raise UFDemoError(
                RESPONSE_SEMANTICS_INVALID,
                "响应输出出现负去除量",
                field_path="response.values",
                actual=float(np.min(arr)),
                requirement="去除量 >= 0",
                suggestion="检查阈值掩膜与对数核实现。",
            )


@dataclass
class ThresholdResult:
    """阈值/分类观测。与逐事件去除分开分派（细则 4.3）。"""

    output_semantics: str
    available: bool
    threshold_internal: float | None
    observable_name: str
    exceed_mask: Any | None = None
    reason: str | None = None
    history_definition: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_semantics": self.output_semantics,
            "available": self.available,
            "threshold_internal": self.threshold_internal,
            "observable": self.observable_name,
            "reason": self.reason,
            "note": "阈值标记不是热影响区（HAZ）。",
        }


class ThresholdEvaluator:
    """阈值展示器。**不产生深度**，也不与深度核共用零 δ 伪实现。"""

    @staticmethod
    def evaluate(fluence: Any, protocol: Mapping[str, Any]) -> ThresholdResult:
        import numpy as np

        thr = protocol.get("threshold_internal")
        name = protocol.get("observable_name", "threshold_exceedance")
        if thr is None or not (isinstance(thr, (int, float)) and thr > 0):
            return ThresholdResult(
                output_semantics=SEMANTIC_THRESHOLD_ONLY,
                available=False,
                threshold_internal=None,
                observable_name=name,
                reason="缺少可用阈值（null 不得按 0 处理）",
            )
        arr = np.asarray(fluence, dtype=np.float64)
        return ThresholdResult(
            output_semantics=SEMANTIC_THRESHOLD_ONLY,
            available=True,
            threshold_internal=float(thr),
            observable_name=name,
            exceed_mask=arr > float(thr),
            history_definition=dict(protocol.get("history_definition", {}) or {}),
        )


@dataclass
class HistoryState:
    """局部受照历史。

    细则 5.2：禁止用全局事件序号代替局部历史；首脉冲不得用 N=0。
    """

    exposure_count: Any | None = None  # (ny, nx) uint32，本事件响应前的局部计数
    definition: Mapping[str, Any] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.exposure_count is not None


class FixedThresholdLogLaw:
    """固定阈值对数去除核 ``a(F) = delta*[ln(F/F_th)]_+``。"""

    KIND = "log_fixed"

    def __init__(
        self,
        *,
        threshold_internal: float,
        delta_internal: float,
        depth_direction: str = "surface_normal",
        output_semantics: str = SEMANTIC_EVENT_INCREMENT,
        unit_mode: str = "SI",
        source_equation: str | None = None,
        kind: str = KIND,
    ) -> None:
        if not (math.isfinite(threshold_internal) and threshold_internal > 0):
            raise UFDemoError(
                RESPONSE_SEMANTICS_INVALID,
                "阈值必须是有限正值",
                field_path="response.threshold",
                actual=threshold_internal,
                requirement="有限正数",
            )
        if not (math.isfinite(delta_internal) and delta_internal > 0):
            raise UFDemoError(
                RESPONSE_SEMANTICS_INVALID,
                "去除尺度 delta 必须是有限正值",
                field_path="response.delta_m",
                actual=delta_internal,
                requirement="有限正数；缺失时不得以 0 或相近材料值代替",
            )
        assert_increment_semantics(output_semantics)
        self.threshold_internal = float(threshold_internal)
        self.delta_internal = float(delta_internal)
        self.depth_direction = depth_direction
        self.output_semantics = output_semantics
        self.unit_mode = unit_mode
        self.source_equation = source_equation
        self.kind = kind

    def increment(
        self,
        fluence: Any,
        history: HistoryState | None,
        material: Any = None,
    ) -> IncrementResult:
        """计算候选去除量。先掩膜后取对数。"""
        import numpy as np

        F = np.asarray(fluence, dtype=np.float64)
        if not np.all(np.isfinite(F)):
            raise UFDemoError(
                NUMERIC_NONFINITE,
                "输入能流包含 NaN/Inf",
                field_path="response.fluence",
                actual="non-finite",
                suggestion="修正上游光束或路径计算。",
            )
        mask = F > self.threshold_internal
        values = np.zeros_like(F)
        if np.any(mask):
            ratio = F[mask] / self.threshold_internal
            values[mask] = self.delta_internal * np.log(ratio)

        if history is not None and history.enabled:
            # M0 不支持扫描孵化；这里显式报错而不是静默忽略
            raise UFDemoError(
                RESPONSE_SEMANTICS_INVALID,
                "该核未实现历史耦合",
                field_path="solver.history_enabled",
                actual=True,
                requirement="history_enabled=false（M0）",
                suggestion="关闭历史；受标定的孵化核属批次 H（T15）。",
            )

        res = IncrementResult(
            output_semantics=self.output_semantics,
            depth_direction=self.depth_direction,
            unit_mode=self.unit_mode,
            values=values,
            modification_mask=mask,
            history={"history_definition": "none", "valid_effective_count": None},
            diagnostics={
                "threshold_internal": self.threshold_internal,
                "delta_internal": self.delta_internal,
                "source_equation": self.source_equation,
                "kind": self.kind,
                "n_ablating_cells": int(np.count_nonzero(mask)),
                "peak_increment_internal": float(np.max(values)) if values.size else 0.0,
            },
        )
        res.validate()
        return res


def build_pulse_law(material: Any, *, unit: Any | None = None) -> FixedThresholdLogLaw:
    """由材料卡构造脉冲律。字段缺失时报错，不补近似值。"""
    r = dict(getattr(material, "response", {}) or {})
    thr = r.get("threshold_internal")
    delta = r.get("delta_internal")
    if delta is None:
        raise UFDemoError(
            RESPONSE_SEMANTICS_INVALID,
            "材料卡缺少去除尺度 δ，无法生成物理深度",
            field_path="response.delta_m",
            actual=None,
            requirement="有限正数",
            suggestion="补齐配套深度曲线；不得由阈值反推物理深度，也不得用相近材料补值。",
        )
    return FixedThresholdLogLaw(
        threshold_internal=float(thr),
        delta_internal=float(delta),
        depth_direction=str(r.get("depth_direction", "surface_normal")),
        output_semantics=str(r.get("output_semantics", SEMANTIC_EVENT_INCREMENT)),
        unit_mode=getattr(unit, "mode", "SI"),
        source_equation=getattr(material, "source_equation", None),
        kind=str(r.get("kind", FixedThresholdLogLaw.KIND)),
    )
