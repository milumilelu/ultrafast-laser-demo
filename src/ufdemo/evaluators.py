"""过程响应评估器（U06）—— **不是**单脉冲去除律。

本模块回答：给定工艺三输入（功率、扫描速度、遍数），**这条沟槽最终多宽/多深/多粗糙**。

⚠️ **它不是逐事件去除核**：``NOT_a_pulse_law = True``。
   它**不进**主循环，**不产生**高度场，**不参与**任何形貌更新。
   它输出的是**标量三元组**（宽、深、Ra），属于工艺级过程响应。

数据来源：``data/measured/diamond_rsm_measured.csv``（D01，17 组设计 + 1 组留出）。
系数由**本包数据自行拟合**，不抄论文的拟合值。

模型：标准化 + 二次响应面 + 岭回归（α 固定 0.01，**不在留出集上调参**）。

为什么必须报告「分组五折」而不是只报留出：样本仅 17 行、13 种工况，
单点留出的 3.26% 看起来很漂亮，但**分组五折的深度 MAPE 是 52.42%** ——
那才是这个小样本的真实泛化水平。只报前者是选择性呈现。

设计约束
--------
* **不导入** Streamlit / Plotly（纯逻辑，可被 UI、CLI、报告复用）；
* 不产生网格/高度场；
* 越出采样工况箱一律**显式拒绝**，不外推、不静默钳到边界。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from .errors import CONFIG_INVALID, UFDemoError

#: 明确标记：本模块**不是**脉冲律。任何把它的输出直接加到表面高度的用法都是错的。
NOT_a_pulse_law = True

INPUTS: tuple[str, ...] = ("power_W", "scan_speed_m_s", "passes")
OUTPUTS: tuple[str, ...] = ("width_um", "depth_um", "Ra_um")

#: 标准化中心与尺度（把设计矩阵条件数压下来；固定值，不随数据变）
CENTER = np.array([11.1, 2.0, 100.0])
SCALE = np.array([5.0, 1.0, 50.0])

#: 二次响应面项（顺序与系数一一对应）
TERMS: tuple[str, ...] = ("1", "P", "v", "N", "P2", "v2", "N2", "Pv", "PN", "vN")

#: 固定正则强度。**不在留出集上调参**（否则留出验证失效）。
DEFAULT_ALPHA = 0.01

#: 分组五折的固定随机种子（保证划分可复现、可追踪）
GROUPED_CV_SEED = 20260911

#: 「已验证」的**预设门槛**。达不到就必须如实说「未验证」。
#: 取分组五折深度 MAPE ≤ 10% —— 这是**本工程自设**的展示门槛，
#: 不是材料学判据，也不代表任何物理结论。
VALIDATION_GATE = {
    "grouped_cv_depth_mape_percent_max": 10.0,
    "note": "本工程自设的展示门槛（非材料学判据）。不达即不得标『已验证』。",
}


def design(x: Any) -> np.ndarray:
    """构造二次设计矩阵（标准化后）。"""
    arr = np.atleast_2d(np.asarray(x, dtype=float))
    if arr.shape[1] != 3:
        raise UFDemoError(
            CONFIG_INVALID,
            "工艺输入必须是 [功率_W, 扫描速度_m_s, 遍数] 三列",
            field_path="evaluator.inputs",
            actual=f"shape={arr.shape}",
            requirement="(n, 3)",
        )
    if not np.isfinite(arr).all():
        raise UFDemoError(
            CONFIG_INVALID,
            "工艺输入含非有限值",
            field_path="evaluator.inputs",
            actual="NaN/Inf",
            requirement="有限实数",
        )
    a, b, c = ((arr - CENTER) / SCALE).T
    return np.column_stack([np.ones(len(arr)), a, b, c,
                            a * a, b * b, c * c, a * b, a * c, b * c])


@dataclass
class SupportReport:
    """工况是否落在**采样箱**内，以及为什么。

    ⚠️ 「在箱内」只是**必要条件**，不是「有数据支撑」的保证 ——
    箱内可能有大片未采样区域（例如三个输入角点附近）。
    """

    inside_box: bool
    bounds: Mapping[str, tuple[float, float]]
    outside: tuple[str, ...] = ()
    note: str = (
        "「在采样箱内」是必要条件，不是支撑保证：箱内仍可能存在未采样区域。"
        "结论只适用于同一研究的工艺窗口。"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "inside_box": self.inside_box,
            "bounds": {k: list(v) for k, v in self.bounds.items()},
            "outside": list(self.outside),
            "note": self.note,
        }


class QuadraticResponseSurface:
    """二次响应面 + 岭回归（标准化坐标）。"""

    def __init__(self, alpha: float = DEFAULT_ALPHA) -> None:
        if not (math.isfinite(alpha) and alpha > 0):
            raise UFDemoError(
                CONFIG_INVALID,
                "岭回归 α 必须是有限正数",
                field_path="evaluator.alpha",
                actual=alpha,
                requirement="> 0",
            )
        self.alpha = float(alpha)
        self.coef_: np.ndarray | None = None
        self.bounds_: dict[str, tuple[float, float]] = {}

    # -- 内部 -----------------------------------------------------------------
    def _penalty(self, n: int) -> np.ndarray:
        pen = np.eye(n) * self.alpha
        pen[0, 0] = 0.0  # 不惩罚截距：惩罚它只会把整体水平拉偏
        return pen

    # -- 拟合 ----------------------------------------------------------------
    def fit(self, x: Any, y: Any) -> "QuadraticResponseSurface":
        xs = np.atleast_2d(np.asarray(x, dtype=float))
        ys = np.atleast_2d(np.asarray(y, dtype=float))
        if ys.shape[1] != len(OUTPUTS):
            raise UFDemoError(
                CONFIG_INVALID,
                f"输出必须是 {len(OUTPUTS)} 列（{list(OUTPUTS)}）",
                field_path="evaluator.outputs",
                actual=f"shape={ys.shape}",
                requirement=f"(n, {len(OUTPUTS)})",
            )
        X = design(xs)
        self.coef_ = np.linalg.solve(X.T @ X + self._penalty(X.shape[1]), X.T @ ys)
        # 记录采样箱（**只用训练行**，不含留出行 —— 否则支撑域会被留出点撑大）
        self.bounds_ = {
            name: (float(xs[:, j].min()), float(xs[:, j].max()))
            for j, name in enumerate(INPUTS)
        }
        return self

    # -- 预测 ----------------------------------------------------------------
    def predict(self, x: Any, *, strict: bool = True) -> np.ndarray:
        """预测三个输出。

        ``strict=True``（默认）时，越出采样箱**直接拒绝** —— 不外推。
        这是刻意的：小样本二次面的箱外行为没有任何依据，悄悄外推出来的数字
        看起来同样「精确」，但完全没有支撑。
        """
        if self.coef_ is None:
            raise UFDemoError(CONFIG_INVALID, "评估器尚未拟合", field_path="evaluator.coef",
                              actual=None, requirement="先调用 fit()")
        xs = np.atleast_2d(np.asarray(x, dtype=float))
        support = self.check_support(xs)
        if strict and not support.inside_box:
            raise UFDemoError(
                CONFIG_INVALID,
                "工况超出该数据的采样范围，拒绝外推预测",
                field_path="evaluator.support",
                actual=list(support.outside),
                requirement="三个输入都落在训练数据的采样箱内",
                suggestion="换用落在采样箱内的工况，或另行采集该区域的数据。",
            )
        return design(xs) @ self.coef_

    def check_support(self, x: Any) -> SupportReport:
        xs = np.atleast_2d(np.asarray(x, dtype=float))
        outside: list[str] = []
        for j, name in enumerate(INPUTS):
            lo, hi = self.bounds_.get(name, (-math.inf, math.inf))
            if not all(lo - 1e-12 <= v <= hi + 1e-12 for v in xs[:, j]):
                outside.append(name)
        return SupportReport(inside_box=not outside, bounds=dict(self.bounds_),
                             outside=tuple(outside))


@dataclass
class Prediction:
    """一条工况的预测结果（**标量三元组**，没有形貌、没有高度场）。"""

    inputs: dict[str, float]
    outputs: dict[str, float]
    support: SupportReport
    warnings: tuple[str, ...] = ()
    is_process_response_only: bool = True
    NOT_a_pulse_law: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputs": dict(self.inputs),
            "outputs": dict(self.outputs),
            "support": self.support.to_dict(),
            "warnings": list(self.warnings),
            "is_process_response_only": self.is_process_response_only,
            "NOT_a_pulse_law": self.NOT_a_pulse_law,
        }


@dataclass
class ProcessEvaluator:
    """工艺过程响应评估器（U06）。

    用法::

        ev = ProcessEvaluator.from_rows(rows)      # 用 calibration_candidate 拟合
        ev.predict({"power_W": 14.7, "scan_speed_m_s": 2.77, "passes": 117})
    """

    surface: QuadraticResponseSurface
    n_training_rows: int = 0
    n_distinct_conditions: int = 0
    source_doi: str | None = None
    material_family: str | None = None
    held_out_case_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    # -- 构造 ----------------------------------------------------------------
    @classmethod
    def from_rows(cls, rows: Sequence[Mapping[str, Any]], *,
                  alpha: float = DEFAULT_ALPHA) -> "ProcessEvaluator":
        """用 ``split_role == calibration_candidate`` 的行拟合。

        **留出行绝不参与拟合** —— 这是留出验证成立的前提。
        """
        train = [r for r in rows if str(r.get("split_role", "")) == "calibration_candidate"]
        held = [str(r.get("case_id")) for r in rows
                if str(r.get("split_role", "")) == "held_out_published_confirmation"]
        if not train:
            raise UFDemoError(
                CONFIG_INVALID,
                "没有 calibration_candidate 行，无法拟合",
                field_path="evaluator.rows",
                actual=len(rows),
                requirement="至少 1 行 split_role=calibration_candidate",
            )
        x = np.array([[float(r[k]) for k in INPUTS] for r in train], dtype=float)
        y = np.array([[float(r[k]) for k in OUTPUTS] for r in train], dtype=float)
        surface = QuadraticResponseSurface(alpha=alpha).fit(x, y)
        groups = {str(r.get("condition_group", "")) for r in train}
        doi = next((str(r.get("source_doi")) for r in train if r.get("source_doi")), None)
        fam = next((str(r.get("material_family")) for r in train if r.get("material_family")), None)
        return cls(
            surface=surface,
            n_training_rows=len(train),
            n_distinct_conditions=len(groups),
            source_doi=doi,
            material_family=fam,
            held_out_case_ids=tuple(held),
            warnings=(
                "本评估器是**工艺级过程响应**，不是逐事件去除律；"
                "不得把它输出的深度当作单脉冲增量加到表面高度上。",
                "样本仅 17 行 / 13 种工况：任何「高精度」「普适」的措辞都不成立。",
                "无实测三维形貌：由宽/深构造的表面只能是 assumed-profile 可视化。",
            ),
        )

    # -- 使用 ----------------------------------------------------------------
    def predict(self, inputs: Mapping[str, float], *, strict: bool = True) -> Prediction:
        missing = [k for k in INPUTS if k not in inputs]
        if missing:
            raise UFDemoError(
                CONFIG_INVALID,
                f"缺少工艺输入：{missing}",
                field_path="evaluator.inputs",
                actual=sorted(inputs),
                requirement=f"必须提供 {list(INPUTS)}",
            )
        x = [[float(inputs[k]) for k in INPUTS]]
        out = self.surface.predict(x, strict=strict)[0]
        sup = self.surface.check_support(x)
        warns: list[str] = []
        if not sup.inside_box:
            warns.append("工况超出采样箱，" + ("已拒绝（strict）" if strict else "结果不可用于结论"))
        warns.append("过程响应输出（非脉冲律）：不可用于形貌更新")
        return Prediction(inputs={k: float(inputs[k]) for k in INPUTS},
                          outputs={k: float(out[j]) for j, k in enumerate(OUTPUTS)},
                          support=sup, warnings=tuple(warns))

    # -- 门槛 ----------------------------------------------------------------
    def validation_verdict(self, grouped_cv_depth_mape_percent: float) -> dict[str, Any]:
        """按**预设门槛**判定是否够格称「已验证」。

        小样本 + 高的分组 CV 误差 ⇒ 结论必然是「未验证」。这里把它写成代码，
        而不是靠人记得在报告里写一句。
        """
        limit = VALIDATION_GATE["grouped_cv_depth_mape_percent_max"]
        passed = grouped_cv_depth_mape_percent <= limit
        return {
            "gate": dict(VALIDATION_GATE),
            "grouped_cv_depth_mape_percent": grouped_cv_depth_mape_percent,
            "passed": passed,
            "verdict": "达到自设门槛" if passed else "**未达预设门槛 —— 不得标注「已验证」**",
        }
