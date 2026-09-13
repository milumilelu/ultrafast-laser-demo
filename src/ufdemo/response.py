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
from typing import Any, Mapping, Sequence

from .config import (
    INCREMENT_SEMANTICS,
    SEMANTIC_EVENT_INCREMENT,
    SEMANTIC_THRESHOLD_ONLY,
)
from .errors import (
    NUMERIC_NONFINITE,
    RESPONSE_SEMANTICS_INVALID,
    TABLE_OUT_OF_RANGE,
    UFDemoError,
)

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
        if (
            thr is None
            or not isinstance(thr, (int, float))
            or isinstance(thr, bool)
            or not math.isfinite(float(thr))
            or float(thr) <= 0
        ):
            return ThresholdResult(
                output_semantics=SEMANTIC_THRESHOLD_ONLY,
                available=False,
                threshold_internal=None,
                observable_name=name,
                reason="缺少可用阈值（null 不得按 0 处理）",
            )
        arr = np.asarray(fluence, dtype=np.float64)
        if not np.all(np.isfinite(arr)):
            return ThresholdResult(
                output_semantics=SEMANTIC_THRESHOLD_ONLY,
                available=False,
                threshold_internal=float(thr),
                observable_name=name,
                reason="入射能流含 NaN/Inf，拒绝生成阈值掩膜",
            )
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


#: 曲线卡单位 → SI 因子（U07）。
#: **必须有这一层**：曲线卡上的 x/y 用的是**惯用单位**（如 J/cm²、µm），
#: 而求解器内部一律 SI（J/m²、m）。不换算的话，J/cm² 的曲线收到 J/m² 的
#: 能流会全部「高于上界」而被拒 —— 实测就是这个 bug：6.92 J/cm² 的曲线
#: 收到 69200 J/m² 后直接 `TABLE_OUT_OF_RANGE`，真实曲线一条都用不了。
_UNIT_TO_SI: dict[str, float] = {
    # 能流
    "j/m^2": 1.0, "j/m²": 1.0, "j/m2": 1.0,
    "j/cm^2": 1.0e4, "j/cm²": 1.0e4, "j/cm2": 1.0e4,
    "mj/cm^2": 1.0e1, "mj/cm²": 1.0e1,
    # 长度
    "m": 1.0, "um": 1.0e-6, "µm": 1.0e-6, "μm": 1.0e-6, "nm": 1.0e-9,
    # 计数/无量纲
    "1": 1.0, "": 1.0,
}


def _unit_to_si_factor(unit: Any, *, where: str) -> float:
    """曲线单位 → SI 因子。**未知单位一律报错**，不默认 1.0（那会静默错算）。"""
    key = str(unit or "").strip().lower()
    if key not in _UNIT_TO_SI:
        raise UFDemoError(
            RESPONSE_SEMANTICS_INVALID,
            f"查表核不支持该单位：{unit!r}",
            field_path=where,
            actual=unit,
            requirement=f"取值属于 {sorted(set(_UNIT_TO_SI))}",
            suggestion="在 _UNIT_TO_SI 里登记该单位；**不要**默认按 1.0 处理（会静默错算）。",
        )
    return _UNIT_TO_SI[key]


class TabulatedEventLaw:
    """逐事件**查表**去除核（U07）—— 用实测响应曲线驱动局部增量。

    与 :class:`FixedThresholdLogLaw` 的关键区别：它的响应来自**数据**而非解析式。
    这带来三条必须守住的纪律（写进代码，不只写在文档）：

    1. **只有 ``event_depth_increment`` 曲线能进主循环**。别的语义
       （累计深度、体积效率、平均率、端点几何）一律经**既有闸门**
       :func:`tables.assert_curve_can_enter_event_kernel` 拒绝，
       抛既定错误码 ``RESPONSE_SEMANTICS_INVALID``。
    2. **固定条件必须与运行条件一致** —— 复用同一个闸门的比对逻辑，不另写一套；
       不一致抛 ``CONDITION_MISMATCH``。
    3. **越界不返回 0、不外推、不钳端点** —— 与查表口径完全一致：
       高于上界**一律拒绝**；低于下界默认也拒绝，
       只有当曲线**显式声明**了 ``threshold_rule``（如
       ``{"mode": "zero_below", "reason": ...}``）时才允许记 0。
       **禁止默认填 0**：低于量测区间不等于无去除，那是未知区。

    插值**直接复用** :func:`tables.lookup`（分段线性手写二分 / PCHIP），
    因此逐点结果与查表**完全一致**（有测试逐点比对）。
    """

    KIND = "table_event"

    #: 该核依赖的**显式假设**。构造时会一并记录到 ``IncrementResult.diagnostics``，
    #: 供报告与界面如实展示 —— 「用峰值中心关系近似局部响应」是假设，不是事实。
    DEFAULT_ASSUMPTIONS: tuple[str, ...] = (
        "以「峰值能流 → 中心去除量」的实测关系近似**局部**响应；"
        "未独立标定 Gaussian 光斑内的径向分布。",
        "曲线未含完整阈值律与光斑尾部响应；低于量程的区间是**未知区**，不是零去除区。",
        "该核只处理**单次事件**的局部增量；不含孵化/历史耦合。",
    )

    def __init__(
        self,
        curve: Any,
        *,
        method: str = "linear",
        threshold_rule: Mapping[str, Any] | None = None,
        depth_direction: str | None = None,
        unit_mode: str = "SI",
        laser: Mapping[str, Any] | None = None,
        assumptions: Sequence[str] | None = None,
    ) -> None:
        from . import tables

        # --- 闸门 1：语义 + 条件 ---------------------------------------------
        # 直接调用既有闸门：非增量语义 → RESPONSE_SEMANTICS_INVALID；
        # 固定条件与 laser 不符 → CONDITION_MISMATCH。**不另写一套判定。**
        tables.assert_curve_can_enter_event_kernel(curve, laser=laser)

        self.curve = curve
        self.method = str(method)
        self.unit_mode = unit_mode
        # --- 单位换算（U07）---------------------------------------------------
        # 曲线用**惯用单位**（J/cm²、µm），求解器内部用 **SI**（J/m²、m）。
        # 在这里一次性建好两个因子，避免每次 increment 重算，也避免漏换算。
        self.x_to_si = _unit_to_si_factor(
            (curve.x_quantity or {}).get("unit"), where="curve.x_quantity.unit")
        self.y_to_si = _unit_to_si_factor(
            (curve.y_quantity or {}).get("unit"), where="curve.y_quantity.unit")
        self.depth_direction = (
            depth_direction or getattr(curve, "depth_direction", None) or "surface_normal"
        )
        self.assumptions = tuple(assumptions) if assumptions else self.DEFAULT_ASSUMPTIONS

        # --- 阈值规则：**默认拒绝**低于量程的点 -------------------------------
        # 调用方没显式给时，用**曲线自带**的规则（U07）——
        # 这样「下界以下怎么算」是曲线属性，跟着数据走，而不是求解器替它决定。
        if threshold_rule is None:
            rule = dict(getattr(curve, "threshold_rule", None) or {})
        else:
            rule = dict(threshold_rule)
        mode = str(rule.get("mode", "reject"))
        if mode not in ("reject", "zero_below"):
            raise UFDemoError(
                RESPONSE_SEMANTICS_INVALID,
                f"未知的 threshold_rule.mode：{mode!r}",
                field_path="threshold_rule.mode",
                actual=mode,
                requirement="'reject'（默认，越界即拒）或 'zero_below'（显式声明下界以下无去除）",
                suggestion="不要为『能跑通』而放宽；下界以下没有实测依据。",
            )
        if mode == "zero_below" and not str(rule.get("reason", "")).strip():
            # 显式声明必须**带理由** —— 否则「声明」会退化成随手填个默认值。
            raise UFDemoError(
                RESPONSE_SEMANTICS_INVALID,
                "threshold_rule.mode='zero_below' 必须附带 reason",
                field_path="threshold_rule.reason",
                actual=None,
                requirement="非空字符串，说明凭据（独立阈值律/文献依据）",
                suggestion="没有凭据就保持默认 'reject'：低于量程是未知区，不是零去除。",
            )
        self.threshold_rule = rule
        self.threshold_mode = mode

    # -- 兼容 FixedThresholdLogLaw 的属性（下游按需读取）--------------------
    @property
    def kind(self) -> str:  # noqa: D102
        return self.KIND

    @property
    def output_semantics(self) -> str:  # noqa: D102
        return str(self.curve.output_semantics)

    @property
    def source_equation(self) -> str | None:  # noqa: D102
        return getattr(self.curve, "source_figure_or_table", None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.KIND,
            "curve_id": self.curve.curve_id,
            "output_semantics": self.output_semantics,
            "method": self.method,
            "valid_range": list(self.curve.valid_range),
            "threshold_rule": dict(self.threshold_rule),
            "depth_direction": self.depth_direction,
            "assumptions": list(self.assumptions),
        }

    # -- 逐事件增量 ----------------------------------------------------------
    def increment(
        self,
        fluence: Any,
        history: HistoryState | None,
        material: Any = None,
    ) -> IncrementResult:
        """按**当次事件的局部能流**查表得到去除增量。

        **逐事件**推进：每个事件各查一次，不是把累计量摊到 N 次。
        """
        import numpy as np

        from . import tables

        F = np.asarray(fluence, dtype=np.float64)
        if not np.all(np.isfinite(F)):
            raise UFDemoError(
                NUMERIC_NONFINITE,
                "输入能流包含 NaN/Inf",
                field_path="response.fluence",
                actual="non-finite",
                suggestion="修正上游光束或路径计算。",
            )

        if history is not None and history.enabled:
            # 与 FixedThresholdLogLaw 同口径：查表曲线没有历史耦合的实现，
            # **显式报错而不是静默忽略**。
            raise UFDemoError(
                RESPONSE_SEMANTICS_INVALID,
                "该核未实现历史耦合",
                field_path="solver.history_enabled",
                actual=True,
                requirement="history_enabled=False（查表核尚无孵化/状态累积模型）",
                suggestion="关闭历史，或改用已标定的历史耦合核。",
            )

        # --- 单位换算：曲线用惯用单位，输入是内部 SI ------------------------
        # 曲线卡的 `valid_range` 与点位都是**曲线单位**（如 J/cm²）；
        # 传进来的 `F` 是内部 **SI**（J/m²）。所有比较在 SI 下做，
        # 查表时再折回曲线单位 —— 中间不做隐式换算。
        lo_c, hi_c = self.curve.valid_range
        lo, hi = lo_c * self.x_to_si, hi_c * self.x_to_si
        tol = 1e-9 * max(1.0, abs(lo), abs(hi))
        flat = F.ravel()
        below = flat < (lo - tol)
        above = flat > (hi + tol)
        unit_txt = str((self.curve.x_quantity or {}).get("unit", ""))
        si_txt = str((self.curve.x_quantity or {}).get("unit_si") or "SI 内部单位")

        # --- 越界：**绝不返回 0、绝不外推** ---------------------------------
        if bool(above.any()):
            raise UFDemoError(
                TABLE_OUT_OF_RANGE,
                "高于曲线有效区间上界：拒绝外推",
                field_path="response.fluence",
                actual=[float(v) for v in flat[above][:8]],
                requirement=(f"局部能流必须 ≤ {hi_c:g} {unit_txt}"
                             f"（= {hi:g} {si_txt}）"),
                suggestion=(
                    "上界之外没有实测依据，**不**外推、不钳到端点。"
                    "请收窄工况，或补充该能流区间的实测响应。"
                ),
            )
        if bool(below.any()) and self.threshold_mode != "zero_below":
            raise UFDemoError(
                TABLE_OUT_OF_RANGE,
                "低于曲线有效区间下界：拒绝（下界以下不等于无去除）",
                field_path="response.fluence",
                actual=[float(v) for v in flat[below][:8]],
                requirement=(
                    f"局部能流必须 ≥ {lo_c:g} {unit_txt}"
                    f"（= {lo:g} {si_txt}）；除非曲线显式声明 threshold_rule="
                    "{'mode': 'zero_below', 'reason': ...}"
                ),
                suggestion=(
                    "低于量测区间**不视为无去除**：那是未知区。"
                    "要么补数据，要么在曲线上显式声明阈值规则（并给出凭据）。"
                ),
            )

        values = np.zeros_like(F)
        in_range = ~below  # above 已在上面抛错，走到这里就没有 above
        if bool(in_range.any()):
            # SI → **曲线单位**（查表按卡上单位取值），结果再折回 SI。
            xs_curve = [float(v) / self.x_to_si for v in flat[in_range]]
            # **复用查表**：保证与 tables.lookup 逐点一致，不存在第二套插值。
            res = tables.lookup(self.curve, xs_curve, method=self.method)
            got = res.values
            if got is None or any(v is None for v in got):
                raise UFDemoError(
                    TABLE_OUT_OF_RANGE,
                    "查表返回空值（不应发生：区间已先行校验）",
                    field_path="response.fluence",
                    actual=None,
                    requirement="区间内查询必须返回数值",
                )
            values.ravel()[in_range] = np.asarray(
                [float(v) * self.y_to_si for v in got], dtype=np.float64)

        result = IncrementResult(
            output_semantics=SEMANTIC_EVENT_INCREMENT,
            depth_direction=self.depth_direction,
            unit_mode=self.unit_mode,
            values=values,
            available=True,
            reason=(
                "查表逐事件增量；"
                + ("下界以下按曲线显式声明记 0" if self.threshold_mode == "zero_below"
                   else "下界以下拒绝（未知区）")
            ),
            diagnostics={
                "law_kind": self.KIND,
                "curve_id": self.curve.curve_id,
                "interpolation": self.method,
                "valid_range": [lo, hi],
                "threshold_rule": dict(self.threshold_rule),
                "assumptions": list(self.assumptions),
            },
        )
        result.validate()
        return result


def build_pulse_law(material: Any, *, unit: Any | None = None, curve: Any = None,
                    laser: Mapping[str, Any] | None = None):
    """由材料卡（或曲线）构造脉冲律。

    **按曲线类型分派**（U07 / F05）：

    * 传了 ``curve`` 且语义为 ``event_depth_increment`` → :class:`TabulatedEventLaw`
      （真实数据驱动）；
    * 传了 ``curve`` 但语义不是 → 经既有闸门抛 ``RESPONSE_SEMANTICS_INVALID``
      （**不静默退回对数律**，否则「接了曲线」是假的）；
    * 没传 ``curve`` → 维持原行为（:class:`FixedThresholdLogLaw`）。

    ``laser`` 给定时用于**固定条件比对**（应在闸门里抛 ``CONDITION_MISMATCH``）。
    **必须传运行时激光条件**，而不是材料卡的声明值 —— 否则「这条曲线适不适用于
    本次运行」根本没被检查过。

    ``curve=None`` 时行为与改动前**逐位一致**，既有算例不受影响。
    """
    if curve is not None:
        return TabulatedEventLaw(
            curve,
            unit_mode=getattr(unit, "mode", "SI"),
            depth_direction=getattr(curve, "depth_direction", None),
            # 优先用调用方给的**运行时**条件；没给才退回材料卡声明值。
            laser=(dict(laser) if laser is not None
                   else (dict(getattr(material, "laser_conditions", {}) or {}) or None)),
        )

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
