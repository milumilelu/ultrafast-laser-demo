"""文献完整协议评估器 —— 批次 D（T07）。

本模块**只复现文献定义**，不生成网格形貌，也**不进入**逐事件增量主循环
（任务书 3.2 节、细则 4.3 与第 7 节）。

两套「有效脉冲数」不能共用一个公式（任务书 3.1 节）::

    YSZ[R1]  N_eff,YSZ = (pi/4) * (2*w0*f) / v        # 面积等效
    SiC[R2]  N_eff,SiC = K * (2*w0*f) / v             # K = 扫描遍数

两者都是「等效次数」，**都不等于**通用路径生成器产出的整数脉冲事件总数
(``event_count``)。代码中用不同函数名区分，参数一律显式命名为
``effective_count`` / ``event_count``，禁止互相代入。

SiC 阈值与平均去除率[R2]::

    Fth(N) = F_inf + (F1 - F_inf) * exp(-k*(N-1))          # 式(6)
    A_R(N) = delta_eff * [ln(F0 / Fth(N))]_+               # 式(7) 平均率

``A_R`` 是「微槽深度 / 有效脉冲数」的平均响应关系，语义为
``mean_depth_per_effective_pulse``；协议累计深度为 ``A_R * N_eff``，语义为
``cumulative_depth``。两者都**只允许**作为参考结果输出，不得逐事件累加到表面。

证据边界（细则 2.1、10 节）：本模块做到「公式核查 (formula_checked)」与
「数值实现验证」；**不等于**实验图复现，评估结果中显式携带
``verified_by_experiment=False`` 与 ``engineering_extension`` 说明。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import (
    SEMANTIC_CUMULATIVE,
    SEMANTIC_MEAN_RATE,
    SEMANTIC_THRESHOLD_ONLY,
)
from .errors import (
    CONDITION_MISMATCH,
    CONFIG_INVALID,
    NUMERIC_NONFINITE,
    RESPONSE_SEMANTICS_INVALID,
    UFDemoError,
)
from .response import assert_increment_semantics

# 两套定义的稳定标识（写入结果与报告，防止混用）
YSZ_EFFECTIVE_COUNT_DEFINITION = "area_equivalent_ysz_eq9"
SIC_EFFECTIVE_COUNT_DEFINITION = "scan_pass_weighted_sic_eq5"

# YSZ 式(9) 的系数 pi/4；写出来以便测试直接引用，不在别处硬编码
YSZ_AREA_FACTOR = math.pi / 4.0

REQUIRED_CASE_FIELDS = ("case_id", "reference_kind", "material_id")

# 允许的参考算例类型
REFERENCE_KINDS: tuple[str, ...] = (
    "ysz_effective_n",
    "sic_threshold_sweep",
    "sic_mean_rate",
)


# ---------------------------------------------------------------------------
# 0. 通用校验
# ---------------------------------------------------------------------------


def _finite_positive(value: Any, path: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
        raise UFDemoError(
            CONFIG_INVALID,
            f"{path} 必须是有限正值",
            field_path=path,
            actual=value,
            requirement="有限正数",
            suggestion="补一个明确的物理量；缺失数据用 null，不要用 0 代替。",
        )
    return float(value)


def _finite_number(value: Any, path: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        raise UFDemoError(
            NUMERIC_NONFINITE,
            f"{path} 必须是有限数值",
            field_path=path,
            actual=value,
            requirement="有限数",
        )
    return float(value)


def assert_reference_only_semantics(semantics: str, *, field_path: str = "reference.output_semantics") -> str:
    """参考评估器的输出语义**必须**是平均率/累计/阈值，不能是逐事件增量。

    这是与 :func:`response.assert_increment_semantics` 相反方向的一道闸门：
    防止有人把逐事件增量曲线误当参考曲线，或把参考曲线偷偷回灌主循环。
    """
    if semantics not in (SEMANTIC_MEAN_RATE, SEMANTIC_CUMULATIVE, SEMANTIC_THRESHOLD_ONLY):
        raise UFDemoError(
            RESPONSE_SEMANTICS_INVALID,
            f"参考评估器不接受输出语义 {semantics!r}",
            field_path=field_path,
            actual=semantics,
            requirement=f"取值属于 {(SEMANTIC_MEAN_RATE, SEMANTIC_CUMULATIVE, SEMANTIC_THRESHOLD_ONLY)}",
            suggestion="逐事件增量请走 response.FixedThresholdLogLaw；参考量只能走本模块。",
        )
    return semantics


# ---------------------------------------------------------------------------
# 1. 有效脉冲数：两套公式，绝不混用
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectiveCount:
    """有效脉冲数结果。``definition`` 标明它属于哪一篇文献的定义。"""

    value: float
    definition: str
    formula: str
    inputs: Mapping[str, Any]
    unit: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "definition": self.definition,
            "formula": self.formula,
            "inputs": dict(self.inputs),
            "unit": self.unit,
            "note": "有效次数；不等于通用路径生成器的事件总数 event_count。",
        }


def ysz_effective_count(
    spot_radius_m: float,
    repetition_rate_Hz: float,
    speed_m_s: float,
    *,
    event_count: float | None = None,
) -> EffectiveCount:
    """YSZ 面积等效有效脉冲数[R1] 式(9)：``N_eff = (pi/4)*(2*w0*f)/v``。

    Parameters
    ----------
    spot_radius_m:
        光斑**半径** w0（m）。注意文献里写的是 w0，不是直径。
    repetition_rate_Hz:
        脉冲重复频率 f（Hz）。
    speed_m_s:
        扫描速度 v（m/s）。
    event_count:
        可选的**真实**脉冲事件总数，仅随结果一起记录以便对照，**不参与计算**。
    """
    w0 = _finite_positive(spot_radius_m, "spot_radius_m")
    f = _finite_positive(repetition_rate_Hz, "repetition_rate_Hz")
    v = _finite_positive(speed_m_s, "speed_m_s")
    n_eff = YSZ_AREA_FACTOR * (2.0 * w0 * f) / v
    return EffectiveCount(
        value=n_eff,
        definition=YSZ_EFFECTIVE_COUNT_DEFINITION,
        formula="N_eff,YSZ = (pi/4) * (2*w0*f) / v",
        inputs={
            "spot_radius_m": w0,
            "repetition_rate_Hz": f,
            "speed_m_s": v,
            "event_count": event_count,
        },
    )


def sic_effective_count(
    spot_radius_m: float,
    repetition_rate_Hz: float,
    speed_m_s: float,
    passes: int,
    *,
    event_count: float | None = None,
) -> EffectiveCount:
    """SiC 式(5) 有效脉冲数[R2]：``N_eff = K*(2*w0*f)/v``，``K`` 为扫描遍数。

    **与 YSZ 定义不同**：这里没有 ``pi/4`` 因子，且额外乘扫描遍数 K。二者
    不可互换，也不可互相代入。
    """
    w0 = _finite_positive(spot_radius_m, "spot_radius_m")
    f = _finite_positive(repetition_rate_Hz, "repetition_rate_Hz")
    v = _finite_positive(speed_m_s, "speed_m_s")
    if not isinstance(passes, int) or isinstance(passes, bool) or passes < 1:
        raise UFDemoError(
            CONFIG_INVALID,
            "passes（扫描遍数 K）必须是 >= 1 的整数",
            field_path="passes",
            actual=passes,
            requirement="整数 >= 1",
            suggestion="K 是遍数，不是浮点权重；未确认时不要用 1 蒙混。",
        )
    n_eff = float(passes) * (2.0 * w0 * f) / v
    return EffectiveCount(
        value=n_eff,
        definition=SIC_EFFECTIVE_COUNT_DEFINITION,
        formula="N_eff,SiC = K * (2*w0*f) / v",
        inputs={
            "spot_radius_m": w0,
            "repetition_rate_Hz": f,
            "speed_m_s": v,
            "passes": passes,
            "event_count": event_count,
        },
    )


def ysz_speed_for_effective_count(
    spot_radius_m: float,
    repetition_rate_Hz: float,
    effective_count: float,
) -> float:
    """YSZ 式(9) 反解扫描速度：``v = (pi/4)*(2*w0*f)/N_eff``（单位 m/s）。

    G05 用它把 ``w0=16 um、f=33300 Hz、N_eff=3`` 换算成
    ``278.9734276388 mm/s``。错误换算 ``v = 2*w0*f/N`` 会得到 ``355.2 mm/s``。
    """
    w0 = _finite_positive(spot_radius_m, "spot_radius_m")
    f = _finite_positive(repetition_rate_Hz, "repetition_rate_Hz")
    n = _finite_positive(effective_count, "effective_count")
    return YSZ_AREA_FACTOR * (2.0 * w0 * f) / n


def naive_speed_for_effective_count(
    spot_radius_m: float,
    repetition_rate_Hz: float,
    effective_count: float,
) -> float:
    """**故意保留的错误换算** ``v = 2*w0*f/N``，仅用于回归对照与文档。

    不得用于任何物理输出；G05 断言它与 YSZ 式(9) 结果不同。
    """
    w0 = _finite_positive(spot_radius_m, "spot_radius_m")
    f = _finite_positive(repetition_rate_Hz, "repetition_rate_Hz")
    n = _finite_positive(effective_count, "effective_count")
    return (2.0 * w0 * f) / n


# ---------------------------------------------------------------------------
# 2. 高斯束能量/能流（条件一致性检查，不新增实验精度）
# ---------------------------------------------------------------------------


def gaussian_peak_fluence(energy_J: float, spot_radius_m: float) -> float:
    """``F0 = 2E/(pi*w0^2)``（单位 J/m^2）。"""
    e = _finite_positive(energy_J, "energy_J")
    w0 = _finite_positive(spot_radius_m, "spot_radius_m")
    return 2.0 * e / (math.pi * w0 * w0)


def gaussian_pulse_energy(peak_fluence_J_m2: float, spot_radius_m: float) -> float:
    """``E = F0*pi*w0^2/2``（单位 J）。G05：50.1 J/cm^2、w0=16 um → 201.464053689406 uJ。"""
    f0 = _finite_positive(peak_fluence_J_m2, "peak_fluence_J_m2")
    w0 = _finite_positive(spot_radius_m, "spot_radius_m")
    return f0 * math.pi * w0 * w0 / 2.0


# ---------------------------------------------------------------------------
# 3. SiC 阈值函数与平均去除率
# ---------------------------------------------------------------------------


def sic_threshold_fluence(
    effective_count: float,
    *,
    f1_J_m2: float,
    f_inf_J_m2: float,
    k_per_pulse: float,
) -> float:
    """SiC 式(6)[R2]：``Fth(N) = F_inf + (F1 - F_inf)*exp(-k*(N-1))``，单位 J/m^2。

    约定 ``N`` 为**有效脉冲数**（不是事件序号）。``N=1`` 给出 ``F1``；
    ``N -> inf`` 趋向 ``F_inf``。``k`` 单位 1/脉冲。
    """
    n = _finite_positive(effective_count, "effective_count")
    f1 = _finite_positive(f1_J_m2, "multi_response.Fth1_internal")
    f_inf = _finite_positive(f_inf_J_m2, "multi_response.Fth_infinity_internal")
    k = _finite_positive(k_per_pulse, "multi_response.k_inc_per_pulse")
    if f_inf >= f1:
        raise UFDemoError(
            CONFIG_INVALID,
            "SiC 阈值拟合参数要求 F_inf < F1（阈值随脉冲数下降）",
            field_path="multi_response",
            actual={"Fth1": f1, "F_inf": f_inf},
            requirement="F_inf < F1",
            suggestion="核对卡内 multi_response；不要把两类阈值观测量互换。",
        )
    return f_inf + (f1 - f_inf) * math.exp(-k * (n - 1.0))


def sic_mean_removal_rate(
    peak_fluence_J_m2: float,
    effective_count: float,
    *,
    delta_eff_m: float,
    f1_J_m2: float,
    f_inf_J_m2: float,
    k_per_pulse: float,
) -> float:
    """SiC 式(7)[R2] 平均去除率 ``A_R = delta_eff*[ln(F0/Fth(N))]_+``，单位 m/有效脉冲。

    这是**平均响应关系**（微槽深度 / 有效脉冲数），不是逐事件增量。低于阈值
    时返回 0，但不代表「已预测无去除」——真实材料响应未在此标定。
    """
    f0 = _finite_positive(peak_fluence_J_m2, "peak_fluence_J_m2")
    delta_eff = _finite_positive(delta_eff_m, "multi_response.delta_eff_mean_m")
    fth = sic_threshold_fluence(
        effective_count, f1_J_m2=f1_J_m2, f_inf_J_m2=f_inf_J_m2, k_per_pulse=k_per_pulse
    )
    if f0 <= fth:
        return 0.0
    return delta_eff * math.log(f0 / fth)


# ---------------------------------------------------------------------------
# 4. 参考结果
# ---------------------------------------------------------------------------


@dataclass
class ReferenceResult:
    """一次参考求值的完整结果。

    关键不变量：``event_kernel_allowed`` 恒为 ``False``；``output_semantics``
    只能是平均率/累计/阈值（由 :func:`assert_reference_only_semantics` 保证）。
    """

    case_id: str
    material_id: str
    reference_kind: str
    output_semantics: str
    protocol_id: str | None
    evidence_status: str | None
    condition_match: str
    values: dict[str, Any] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    effective_count: EffectiveCount | None = None
    event_count: float | None = None
    labels: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_equation: str | None = None
    source_figure_or_table: str | None = None
    engineering_extension: str | None = None
    verified_by_formula: bool = False
    verified_by_experiment: bool = False
    event_kernel_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "material_id": self.material_id,
            "reference_kind": self.reference_kind,
            "output_semantics": self.output_semantics,
            "output_semantics_zh": _SEMANTIC_ZH.get(self.output_semantics, self.output_semantics),
            "protocol_id": self.protocol_id,
            "evidence_status": self.evidence_status,
            "condition_match": self.condition_match,
            "values": dict(self.values),
            "units": dict(self.units),
            "effective_count": self.effective_count.to_dict() if self.effective_count else None,
            "event_count": self.event_count,
            "labels": dict(self.labels),
            "notes": list(self.notes),
            "warnings": list(self.warnings),
            "source_equation": self.source_equation,
            "source_figure_or_table": self.source_figure_or_table,
            "engineering_extension": self.engineering_extension,
            "verified_by_formula": self.verified_by_formula,
            "verified_by_experiment": self.verified_by_experiment,
            "event_kernel_allowed": self.event_kernel_allowed,
            "kernel_refusal_note": "平均率/累计深度不得进入逐事件增量主循环（任务书 3.2）。",
        }

    @property
    def refused_semantics(self) -> tuple[str, ...]:
        """确保语义闸门双向闭合：把这些语义送进事件核必须被拒绝。"""
        return (SEMANTIC_MEAN_RATE, SEMANTIC_CUMULATIVE)


_SEMANTIC_ZH = {
    SEMANTIC_MEAN_RATE: "给定历史下的平均去除率（不可直接加到表面）",
    SEMANTIC_CUMULATIVE: "某一完整照射协议的累计深度（不可重复累加）",
    SEMANTIC_THRESHOLD_ONLY: "阈值/分类观测（不产生深度）",
}


# ---------------------------------------------------------------------------
# 5. 算例加载与条件匹配
# ---------------------------------------------------------------------------

_LASER_FIELDS = ("wavelength_m", "pulse_duration_s", "repetition_rate_Hz", "spot_radius_m")


def load_reference_case(path: str | Path) -> dict[str, Any]:
    """读取一个参考算例 JSON，并做最小结构校验。"""
    p = Path(path)
    if not p.exists():
        raise UFDemoError(
            CONFIG_INVALID,
            "参考算例文件不存在",
            field_path="<file>",
            actual=str(p),
            suggestion="核对路径；示例在 examples/*_reference_case.json。",
        )
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UFDemoError(
            CONFIG_INVALID, "参考算例 JSON 解析失败", field_path="<file>", actual=str(exc)
        ) from exc
    if not isinstance(raw, Mapping):
        raise UFDemoError(CONFIG_INVALID, "参考算例顶层必须是 JSON 对象", field_path="$", actual=type(raw).__name__)
    for f in REQUIRED_CASE_FIELDS:
        if not raw.get(f):
            raise UFDemoError(
                CONFIG_INVALID,
                f"参考算例缺少 {f}",
                field_path=f,
                actual=raw.get(f),
                requirement=f"必须提供 {f}",
            )
    kind = raw["reference_kind"]
    if kind not in REFERENCE_KINDS:
        raise UFDemoError(
            CONFIG_INVALID,
            "未知的 reference_kind",
            field_path="reference_kind",
            actual=kind,
            requirement=f"取值属于 {list(REFERENCE_KINDS)}",
            suggestion="本批实现 YSZ 有效 N 换算、SiC 阈值/平均率；其它材料属后续批次。",
        )
    raw["_path"] = str(p.resolve())
    return dict(raw)


def iter_reference_cases(paths: Iterable[str | Path]) -> Iterable[dict[str, Any]]:
    """批量读取参考算例（批次 D 的迭代入口，取代 config.py 中的占位）。"""
    for p in paths:
        yield load_reference_case(p)


def match_laser_conditions(
    case: Mapping[str, Any],
    material: Any,
) -> tuple[list[str], str]:
    """把算例中给出的激光条件与材料卡 ``reference_protocol.required_laser`` 比对。

    返回 ``(warnings, condition_match)``。若算例未给出任何激光条件，则**不拒绝**：
    任务书 3.1 明确允许评估器直接输入论文有效 N；此时标签为
    ``paper_direct_effective_n``，并记录在 warnings 里以便追责。
    """
    laser = dict(case.get("laser", {}) or {})
    proto = dict(getattr(material, "reference_protocol", {}) or {})
    req = dict(proto.get("required_laser", {}) or {})
    warnings: list[str] = []

    if not any(laser.get(k) is not None for k in _LASER_FIELDS):
        warnings.append(
            "算例未提供激光条件：按任务书 3.1 直接采用论文有效 N，"
            "条件一致性未逐项核验（condition_match=paper_direct_effective_n）。"
        )
        return warnings, "paper_direct_effective_n"

    if not req:
        warnings.append("材料卡未登记 required_laser，无法逐项比对条件。")
        return warnings, "card_has_no_required_laser"

    for key in _LASER_FIELDS:
        if laser.get(key) is None or key not in req:
            continue
        spec = req[key]
        target = float(spec["value"]) if isinstance(spec, Mapping) else float(spec)
        tol = float(spec["rel_tol"]) if isinstance(spec, Mapping) and "rel_tol" in spec else 0.0
        got = float(laser[key])
        if target == 0.0:
            continue
        rel = abs(got - target) / abs(target)
        if rel > tol + 1e-15:
            raise UFDemoError(
                CONDITION_MISMATCH,
                f"参考条件 {key} 超出材料卡允许误差",
                field_path=f"laser.{key}",
                actual=got,
                requirement=f"{target} ± {tol:.0%}（相对）",
                suggestion="改用卡中已确认的条件；超出窗口时不得做定量参考求值。",
            )
    return warnings, "matched_card_protocol"


# ---------------------------------------------------------------------------
# 6. 评估器
# ---------------------------------------------------------------------------


class ReferenceEvaluator:
    """文献协议评估器。**只复现文献定义，不求解网格，不产生增量。**"""

    @staticmethod
    def evaluate_case(case: Mapping[str, Any], material: Any = None) -> ReferenceResult:
        kind = case.get("reference_kind")
        if kind == "ysz_effective_n":
            return ReferenceEvaluator._evaluate_ysz(case, material)
        if kind == "sic_threshold_sweep":
            return ReferenceEvaluator._evaluate_sic(case, material, sweep=True)
        if kind == "sic_mean_rate":
            return ReferenceEvaluator._evaluate_sic(case, material, sweep=False)
        raise UFDemoError(
            CONFIG_INVALID,
            "未知的 reference_kind",
            field_path="reference_kind",
            actual=kind,
            requirement=f"取值属于 {list(REFERENCE_KINDS)}",
        )

    # -- YSZ ---------------------------------------------------------------
    @staticmethod
    def _evaluate_ysz(case: Mapping[str, Any], material: Any) -> ReferenceResult:
        given = dict(case.get("given", {}) or {})
        if not given:
            raise UFDemoError(CONFIG_INVALID, "YSZ 算例缺少 given 块", field_path="given", actual=None)

        warnings, condition_match = ([], "no_material_card")
        if material is not None:
            warnings, condition_match = match_laser_conditions(case, material)

        # 允许两种输入方向：给 N 反解 v；或给 v 正解 N。二者至少给一个。
        n_eff_in = given.get("effective_count")
        v_in = given.get("speed_m_s")
        if n_eff_in is None and v_in is None:
            raise UFDemoError(
                CONFIG_INVALID,
                "YSZ 参考算例必须给出 effective_count 或 speed_m_s 之一",
                field_path="given",
                actual=given,
                requirement="effective_count 或 speed_m_s",
            )

        if n_eff_in is not None:
            w0 = _finite_positive(given.get("spot_radius_m"), "given.spot_radius_m")
            f = _finite_positive(given.get("repetition_rate_Hz"), "given.repetition_rate_Hz")
            eff = ysz_effective_count(
                w0, f, ysz_speed_for_effective_count(w0, f, float(n_eff_in)),
                event_count=given.get("event_count"),
            )
            speed = ysz_speed_for_effective_count(w0, f, float(n_eff_in))
            naive = naive_speed_for_effective_count(w0, f, float(n_eff_in))
        else:
            eff = ysz_effective_count(
                _finite_positive(given.get("spot_radius_m"), "given.spot_radius_m"),
                _finite_positive(given.get("repetition_rate_Hz"), "given.repetition_rate_Hz"),
                float(v_in),
                event_count=given.get("event_count"),
            )
            speed = float(v_in)
            w0 = eff.inputs["spot_radius_m"]
            f = eff.inputs["repetition_rate_Hz"]
            naive = naive_speed_for_effective_count(w0, f, eff.value)

        values: dict[str, Any] = {
            "speed_m_s": speed,
            "speed_mm_s": speed * 1e3,
            "effective_count": eff.value,
            "effective_count_definition": eff.definition,
            # 故意保留的错误换算，供报告对照，不参与任何物理输出
            "naive_speed_m_s": naive,
            "naive_speed_mm_s": naive * 1e3,
            "naive_minus_correct_rel": (naive - speed) / speed if speed else None,
        }
        units = {
            "speed_m_s": "m/s",
            "speed_mm_s": "mm/s",
            "effective_count": "1",
            "naive_speed_m_s": "m/s",
            "naive_speed_mm_s": "mm/s",
        }

        f0 = given.get("peak_fluence_J_m2")
        if f0 is not None:
            f0 = _finite_positive(f0, "given.peak_fluence_J_m2")
            e = gaussian_pulse_energy(f0, eff.inputs["spot_radius_m"])
            values["peak_fluence_J_m2"] = f0
            values["peak_fluence_J_cm2"] = f0 / 1e4
            values["pulse_energy_J"] = e
            values["pulse_energy_uJ"] = e * 1e6
            units.update(
                {
                    "peak_fluence_J_m2": "J/m^2",
                    "peak_fluence_J_cm2": "J/cm^2",
                    "pulse_energy_J": "J",
                    "pulse_energy_uJ": "uJ",
                }
            )

        return ReferenceResult(
            case_id=str(case.get("case_id")),
            material_id=str(case.get("material_id")),
            reference_kind="ysz_effective_n",
            # 有效 N 换算本身不产生深度：语义记为阈值/不产生深度类
            output_semantics=SEMANTIC_THRESHOLD_ONLY,
            protocol_id=(dict(getattr(material, "reference_protocol", {}) or {}).get("protocol_id") if material else None),
            evidence_status=(getattr(material, "evidence_status", None) if material else None),
            condition_match=condition_match,
            values=values,
            units=units,
            effective_count=eff,
            event_count=given.get("event_count"),
            labels=_labels(material, case),
            notes=[
                "本算例只做运动学/条件换算（有效 N、扫描速度、脉冲能量），不产生任何去除深度。",
                "输出语义记为 threshold_only（不产生深度）；有效 N 换算没有对应的深度语义。",
                "YSZ 式(9) 是面积等效有效 N；SiC 式(5) 另含扫描遍数 K，两者不可互换。",
                "有效 N 不等于通用路径生成器产出的事件总数 event_count。",
            ],
            warnings=warnings,
            source_equation=(getattr(material, "source_equation", None) if material else None),
            source_figure_or_table=(getattr(material, "source_figure_or_table", None) if material else None),
            engineering_extension=None,
            verified_by_formula=True,
            verified_by_experiment=False,
        )

    # -- SiC ---------------------------------------------------------------
    @staticmethod
    def _evaluate_sic(case: Mapping[str, Any], material: Any, *, sweep: bool) -> ReferenceResult:
        if material is None:
            raise UFDemoError(
                CONFIG_INVALID,
                "SiC 参考算例必须绑定材料卡（阈值与平均率参数取自 multi_response）",
                field_path="material_id",
                actual=case.get("material_id"),
                suggestion="用 --material-dir 指向 data/materials，或补 material_card_file。",
            )
        m = dict(getattr(material, "multi_response", {}) or {})
        missing = [k for k in ("Fth1_internal", "Fth_infinity_internal", "k_inc_per_pulse", "delta_eff_mean_m") if m.get(k) is None]
        if missing:
            raise UFDemoError(
                CONFIG_INVALID,
                "材料卡 multi_response 字段不完整，无法评估 SiC 参考语义",
                field_path="multi_response",
                actual=missing,
                requirement="Fth1_internal、Fth_infinity_internal、k_inc_per_pulse、delta_eff_mean_m",
                suggestion="补齐迁移卡；不要自行用相近材料补值。",
            )

        warnings, condition_match = match_laser_conditions(case, material)
        given = dict(case.get("given", {}) or {})
        f0 = _finite_positive(given.get("peak_fluence_J_m2"), "given.peak_fluence_J_m2")

        counts = given.get("effective_counts")
        if counts is None:
            counts = [given.get("effective_count")]
        if not counts or any(c is None for c in counts):
            raise UFDemoError(
                CONFIG_INVALID,
                "SiC 参考算例必须给出 effective_count 或 effective_counts",
                field_path="given",
                actual=given,
                requirement="有效脉冲数（原文定义）",
                suggestion="首个评估器允许直接输入论文有效 N；不要用扫描次数列表反推未确认的工况。",
            )
        counts = [float(_finite_positive(c, "given.effective_counts[i]")) for c in counts]

        sweep_rows: list[dict[str, Any]] = []
        for n in counts:
            fth = sic_threshold_fluence(
                n,
                f1_J_m2=m["Fth1_internal"],
                f_inf_J_m2=m["Fth_infinity_internal"],
                k_per_pulse=m["k_inc_per_pulse"],
            )
            ar = sic_mean_removal_rate(
                f0,
                n,
                delta_eff_m=m["delta_eff_mean_m"],
                f1_J_m2=m["Fth1_internal"],
                f_inf_J_m2=m["Fth_infinity_internal"],
                k_per_pulse=m["k_inc_per_pulse"],
            )
            sweep_rows.append(
                {
                    "effective_count": n,
                    "threshold_J_m2": fth,
                    "threshold_J_cm2": fth / 1e4,
                    "mean_depth_per_effective_pulse_m": ar,
                    "mean_depth_per_effective_pulse_nm": ar * 1e9,
                    # 协议累计深度：仅在该协议匹配时可取；不得逐脉冲重复累加
                    "protocol_cumulative_depth_m": ar * n,
                    "protocol_cumulative_depth_um": ar * n * 1e6,
                }
            )

        # primary 行：优先取材料卡 reference_protocol 声明的有效 N（该协议的代表工况），
        # 其次取首个输入值。绝不取 sweep 的最后一个（可能只是 N->inf 极限探针，
        # 其「累计深度」不具物理意义）。
        proto_n = (dict(getattr(material, "reference_protocol", {}) or {}).get("required_history", {}) or {}).get(
            "effective_count"
        )
        primary = None
        if proto_n is not None:
            for row in sweep_rows:
                if abs(row["effective_count"] - float(proto_n)) <= 1e-9:
                    primary = row
                    break
        if primary is None:
            primary = sweep_rows[0]

        values: dict[str, Any] = {
            "peak_fluence_J_m2": f0,
            "peak_fluence_J_cm2": f0 / 1e4,
            "Fth1_J_m2": float(m["Fth1_internal"]),
            "Fth1_J_cm2": float(m["Fth1_internal"]) / 1e4,
            "Fth_infinity_J_m2": float(m["Fth_infinity_internal"]),
            "Fth_infinity_J_cm2": float(m["Fth_infinity_internal"]) / 1e4,
            "k_per_pulse": float(m["k_inc_per_pulse"]),
            "delta_eff_mean_m": float(m["delta_eff_mean_m"]),
            "effective_count": primary["effective_count"],
            "threshold_J_m2": primary["threshold_J_m2"],
            "threshold_J_cm2": primary["threshold_J_cm2"],
            "mean_depth_per_effective_pulse_m": primary["mean_depth_per_effective_pulse_m"],
            "mean_depth_per_effective_pulse_nm": primary["mean_depth_per_effective_pulse_nm"],
            "protocol_cumulative_depth_m": primary["protocol_cumulative_depth_m"],
            "protocol_cumulative_depth_um": primary["protocol_cumulative_depth_um"],
            "sweep": sweep_rows,
        }
        units = {
            "peak_fluence_J_m2": "J/m^2",
            "peak_fluence_J_cm2": "J/cm^2",
            "Fth1_J_m2": "J/m^2",
            "Fth1_J_cm2": "J/cm^2",
            "Fth_infinity_J_m2": "J/m^2",
            "Fth_infinity_J_cm2": "J/cm^2",
            "k_per_pulse": "1/pulse",
            "delta_eff_mean_m": "m",
            "effective_count": "1",
            "threshold_J_m2": "J/m^2",
            "threshold_J_cm2": "J/cm^2",
            "mean_depth_per_effective_pulse_m": "m/effective_pulse",
            "mean_depth_per_effective_pulse_nm": "nm/effective_pulse",
            "protocol_cumulative_depth_m": "m",
            "protocol_cumulative_depth_um": "um",
        }

        result = ReferenceResult(
            case_id=str(case.get("case_id")),
            material_id=str(case.get("material_id")),
            reference_kind=("sic_threshold_sweep" if sweep else "sic_mean_rate"),
            output_semantics=SEMANTIC_MEAN_RATE,
            protocol_id=dict(getattr(material, "reference_protocol", {}) or {}).get("protocol_id"),
            evidence_status=getattr(material, "evidence_status", None),
            condition_match=condition_match,
            values=values,
            units=units,
            effective_count=None,
            event_count=given.get("event_count"),
            labels=_labels(material, case),
            notes=[
                "式(6) Fth(N) 与式(7) 平均率 A_R 按原文定义复现；delta_eff 为拟合有效尺度均值。",
                "标量字段取 primary 行（优先材料卡声明的协议有效 N，此处 %s），完整 N 扫描见 values.sweep。"
                % (primary["effective_count"],),
                "protocol_cumulative_depth = A_R * N_eff 仅在该完整协议下成立，不得逐脉冲重复累加。",
                "N=1e9 探针仅用于核对 Fth(N) -> F_inf 的极限，其累计深度无物理意义。",
                "改性阈值（2.35 J/cm^2）与结构转变阈值（4.97 J/cm^2）是两个观测量，不可互换。",
            ],
            warnings=warnings,
            source_equation=getattr(material, "source_equation", None),
            source_figure_or_table=getattr(material, "source_figure_or_table", None),
            engineering_extension=(
                "原文平均去除率 → 逐事件模型属工程外推，需另记 engineering_extension 并单独验证；"
                "本模块不做该转换。"
            ),
            verified_by_formula=True,
            verified_by_experiment=False,
        )
        assert_reference_only_semantics(result.output_semantics)
        return result


def _labels(material: Any, case: Mapping[str, Any]) -> dict[str, Any]:
    """参考/合成/数值验证标签，随结果贯穿导出（细则 11.3）。"""
    tags = {
        "run_mode": "reference_case",
        "result_class": "reference_evaluation",
        "physical_depth_prediction": False,
        "synthetic": False,
    }
    if material is not None:
        tags.update(
            {
                "material_id": getattr(material, "id", None),
                "evidence_status": getattr(material, "evidence_status", None),
                "source_type": getattr(material, "source_type", None),
                "card_version": getattr(material, "card_version", None),
                "card_sha256": getattr(material, "card_sha256", None),
                "physical_prediction_allowed": bool(
                    (getattr(material, "applicability", {}) or {}).get("physical_material_prediction_allowed", False)
                ),
            }
        )
    if case.get("labels"):
        tags.update(dict(case["labels"]))
    return tags


# ---------------------------------------------------------------------------
# 7. 转成可导出的运行结果（复用 io.save_run 的目录结构，不产生表面）
# ---------------------------------------------------------------------------


def build_reference_run_result(case: Mapping[str, Any], material: Any, result: ReferenceResult) -> Any:
    """把参考结果包装成 :class:`~ufdemo.solver.RunResult`，以便写入标准运行目录。

    ``surface=None`` → 不写 ``final_surface.npz``：参考评估器不求解网格，
    目录里也就不会出现容易被误当形貌结果的表面文件。
    """
    from .solver import RunResult

    statistics: dict[str, Any] = {}
    for k, v in result.values.items():
        if k == "sweep":
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            statistics[k] = float(v)
    statistics["event_kernel_allowed"] = False
    statistics["verified_by_experiment"] = False

    return RunResult(
        status="completed",
        run_id="",
        config_snapshot={"reference_case": {k: v for k, v in case.items() if not str(k).startswith("_")}},
        material_snapshot=dict(getattr(material, "raw", {}) or {}),
        metadata={
            "schema_version": case.get("schema_version", "1.0"),
            "result_class": "reference_evaluation",
            "run_mode": "reference_case",
            "reference_kind": result.reference_kind,
            "case_id": result.case_id,
            "labels": result.labels,
            "reference_result": result.to_dict(),
        },
        statistics=statistics,
        diagnostics={
            "reference": result.to_dict(),
            "event_model_touched": False,
        },
        warnings=list(result.warnings),
        events_processed=0,
        events_total=0,
        removal_available=False,
        unit=None,
        surface=None,
    )
