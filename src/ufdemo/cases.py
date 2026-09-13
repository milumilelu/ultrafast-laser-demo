"""文献算例**回放**（U08）—— 把已知实验协议与实测结果同屏展示。

与「预测」的区别（这条必须说清）：

* 回放 = **已测条件 + 已测结果**，来自论文表格，**不经过模型**；
* 预测 = 模型对**新**工况的输出。

本模块只做前者，外加**可选**叠一条模型曲线作对照。它**不产生**网格形貌。

三条红线（写成代码，不只写在文档）：

1. **``equivalent_pulse_count`` 是等效值，不是真实事件序列**。
   展示时的标签必须写「等效」，且必须带「非真实事件序列」的说明；
   任何把等效值当脉冲数呈现的文案都会被 :func:`assert_pulse_count_wording` 拒绝。
2. **不得由端点数据生成三维形貌**。当前只有端点（深度/宽度），
   只能画散点与误差；若确有重建图层，其标签**必须**含
   「假设截面形状重建」，且**不得**反过来用于训练或计入实验数据。
3. **不得把某工况的累计深度除以 N 后声明成「可用于任意扫描的脉冲核」**。
   本模块**没有**任何「累计 ÷ N」的代码路径。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import CONFIG_INVALID, UFDemoError

#: 回放数据来源（U04 导入的审计包，D02）。
CERAMICS_FILE = "ceramics_dot_line_measured.csv"

#: 重建图层的**强制标签**。由端点数据重建的形貌必须带这个字样，
#: 否则一眼会被读成实测三维形貌。
RECONSTRUCTION_LABEL = "假设截面形状重建"

#: 等效脉冲数的**展示标签**。必须含「等效」与「非真实事件序列」。
EQUIVALENT_PULSE_LABEL = "等效脉冲数（非真实事件序列）"

#: 措辞守卫：一些写法会把等效值说成真实脉冲数。
_FORBIDDEN_EQUIVALENT_PHRASINGS: tuple[str, ...] = (
    "真实脉冲数",
    "实际脉冲数",
    "脉冲数（实测）",
)


def assert_pulse_count_wording(label: str) -> str:
    """守卫「等效脉冲数」的展示措辞。

    要求：含「等效」**且**含「非真实事件序列」；并拒绝把等效值称为真实脉冲数的写法。
    这是**严格子串**口径，与既有 ``assert_safe_wording`` 同风格。
    """
    bad = [t for t in _FORBIDDEN_EQUIVALENT_PHRASINGS if t in label]
    if bad:
        raise UFDemoError(
            CONFIG_INVALID,
            f"等效脉冲数的标签不得表述为真实脉冲数：{bad}",
            field_path="cases.equivalent_pulse_count",
            actual=label,
            requirement=f"必须含「等效」与「非真实事件序列」，如「{EQUIVALENT_PULSE_LABEL}」",
            suggestion="等效脉冲数是按协议折算的量，不是逐事件序列；不要当作可复用的脉冲核。",
        )
    if "等效" not in label or "非真实事件序列" not in label:
        raise UFDemoError(
            CONFIG_INVALID,
            "等效脉冲数的标签必须显式写明「等效」与「非真实事件序列」",
            field_path="cases.equivalent_pulse_count",
            actual=label,
            requirement=f"如「{EQUIVALENT_PULSE_LABEL}」",
        )
    return label


def assert_no_unlabeled_reconstruction(label: str | None) -> None:
    """任何由端点数据重建的图层，标签**必须**含 :data:`RECONSTRUCTION_LABEL`。"""
    if label is None:
        return
    if RECONSTRUCTION_LABEL not in label:
        raise UFDemoError(
            CONFIG_INVALID,
            "由端点数据重建的图层必须标注「假设截面形状重建」",
            field_path="cases.reconstruction_label",
            actual=label,
            requirement=f"标签须含「{RECONSTRUCTION_LABEL}」",
            suggestion="端点数据不足以支撑真实三维形貌；不加标注会被读成实测 3D。",
        )


@dataclass(frozen=True)
class LiteratureCase:
    """一条文献实测算例（**已知条件回放**，不是预测）。"""

    case_id: str
    material_name: str
    material_family: str
    pattern: str
    peak_fluence_J_cm2: float
    depth_um: float
    pulse_count: float | None
    equivalent_pulse_count: float | None
    line_width_um: float | None
    diameter_x_um: float | None
    diameter_y_um: float | None
    wavelength_nm: float | None
    pulse_duration_fs: float | None
    repetition_rate_Hz: float | None
    source_doi: str
    source_locator: str
    quality_note: str = ""
    observation_access: bool = True
    increment_access: bool = False

    # -- 展示 ----------------------------------------------------------------
    @property
    def pulse_count_label(self) -> str:
        """脉冲计数列的中文标签 —— 等效值绝不写成「脉冲数」。"""
        if self.equivalent_pulse_count is not None:
            return assert_pulse_count_wording(EQUIVALENT_PULSE_LABEL)
        return "脉冲数"

    def conditions_text(self) -> str:
        parts = [f"峰值能流 {self.peak_fluence_J_cm2:g} J/cm²"]
        if self.equivalent_pulse_count is not None:
            parts.append(f"{EQUIVALENT_PULSE_LABEL} {self.equivalent_pulse_count:g}")
        elif self.pulse_count is not None:
            parts.append(f"脉冲数 {self.pulse_count:g}")
        if self.wavelength_nm is not None:
            parts.append(f"{self.wavelength_nm:g} nm")
        if self.pulse_duration_fs is not None:
            parts.append(f"{self.pulse_duration_fs:g} fs")
        if self.repetition_rate_Hz is not None:
            parts.append(f"{self.repetition_rate_Hz/1000:g} kHz")
        return "、".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "materialName": self.material_name,
            "materialFamily": self.material_family,
            "pattern": self.pattern,
            "peakFluenceJcm2": self.peak_fluence_J_cm2,
            "depthUm": self.depth_um,
            "pulseCount": self.pulse_count,
            "equivalentPulseCount": self.equivalent_pulse_count,
            "pulseCountLabel": self.pulse_count_label,
            "lineWidthUm": self.line_width_um,
            "diameterXUm": self.diameter_x_um,
            "diameterYUm": self.diameter_y_um,
            "conditions": self.conditions_text(),
            "sourceDoi": self.source_doi,
            "sourceLocator": self.source_locator,
            "qualityNote": self.quality_note,
            "observationAccess": self.observation_access,
            "incrementAccess": self.increment_access,
            "isReplay": True,
            "NOT_a_model_prediction": True,
        }


def _num(row: Mapping[str, Any], key: str) -> float | None:
    v = row.get(key)
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("null", "none"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_ceramics_cases(measured_dir: str | Path) -> list[LiteratureCase]:
    """读 ``ceramics_dot_line_measured.csv``（4 组：两类材料 × 点坑/交叉线）。"""
    path = Path(measured_dir) / CERAMICS_FILE
    if not path.exists():
        return []
    rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    out: list[LiteratureCase] = []
    for r in rows:
        out.append(LiteratureCase(
            case_id=str(r.get("case_id", "")),
            material_name=str(r.get("material_name", "")),
            material_family=str(r.get("material_family", "")),
            pattern=str(r.get("pattern", "")),
            peak_fluence_J_cm2=float(_num(r, "peak_fluence_J_cm2") or 0.0),
            depth_um=float(_num(r, "depth_um") or 0.0),
            pulse_count=_num(r, "pulse_count"),
            equivalent_pulse_count=_num(r, "equivalent_pulse_count"),
            line_width_um=_num(r, "line_width_um"),
            diameter_x_um=_num(r, "diameter_x_um"),
            diameter_y_um=_num(r, "diameter_y_um"),
            wavelength_nm=_num(r, "wavelength_nm"),
            pulse_duration_fs=_num(r, "pulse_duration_fs"),
            repetition_rate_Hz=_num(r, "repetition_rate_Hz"),
            source_doi=str(r.get("source_doi", "")),
            source_locator=str(r.get("source_locator", "")),
            quality_note=str(r.get("quality_note", "") or ""),
        ))
    return out


@dataclass
class CaseReplay:
    """一组文献算例的回放视图（实测 + 可选模型对照）。"""

    cases: tuple[LiteratureCase, ...]
    source_file: str = CERAMICS_FILE
    notes: tuple[str, ...] = field(default_factory=tuple)

    #: 本视图**不提供**任何三维形貌；如将来加重建图层，必须用这个标签。
    reconstruction_label: str = RECONSTRUCTION_LABEL

    @classmethod
    def from_dir(cls, measured_dir: str | Path) -> "CaseReplay":
        cases = tuple(load_ceramics_cases(measured_dir))
        return cls(cases=cases, notes=(
            "已知条件回放：展示的是论文表格中的**实测条件与实测结果**，不经过模型。",
            "模型对照（若有）为叠加曲线，与实测点分开呈现，不混成一条线。",
            "无实测三维形貌：仅有端点（深度/宽度/直径），因此只画散点与误差；"
            f"任何由端点重建的图层必须标注「{RECONSTRUCTION_LABEL}」。",
            f"交叉线的脉冲计数为 {EQUIVALENT_PULSE_LABEL}，"
            "**不是**真实事件序列，也不得据此把累计深度折算成可复用的脉冲核。",
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "ufdemo.web.cases/1",
            "available": bool(self.cases),
            "sourceFile": self.source_file,
            "reconstructionLabel": self.reconstruction_label,
            "equivalentPulseLabel": EQUIVALENT_PULSE_LABEL,
            "cases": [c.to_dict() for c in self.cases],
            "notes": list(self.notes),
            # 明确声明：本视图不含 3D
            "has3dHeightfield": False,
            "isReplay": True,
        }

    # -- 汇总（报告用）-------------------------------------------------------
    def summary_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for c in self.cases:
            rows.append({
                "case_id": c.case_id,
                "material": c.material_name,
                "pattern": c.pattern,
                "peak_fluence_J_cm2": c.peak_fluence_J_cm2,
                "pulse_count": "" if c.pulse_count is None else c.pulse_count,
                "equivalent_pulse_count": "" if c.equivalent_pulse_count is None else c.equivalent_pulse_count,
                "pulse_count_label": c.pulse_count_label,
                "depth_um": c.depth_um,
                "line_width_um": "" if c.line_width_um is None else c.line_width_um,
                "source_doi": c.source_doi,
                "source_locator": c.source_locator,
                "observation_access": c.observation_access,
                "increment_access": c.increment_access,
                "note": c.quality_note,
            })
        return rows


def assert_cases_are_replay_only(cases: Sequence[LiteratureCase]) -> None:
    """回放算例**一律**无 ``increment_access``。

    它们报告的是**端点几何**（坑深/线宽/直径），不是逐事件增量；
    把它们接进主循环等于声称「已知单次事件的局部去除量」，那是没有依据的。
    """
    bad = [c.case_id for c in cases if c.increment_access]
    if bad:
        raise UFDemoError(
            CONFIG_INVALID,
            "文献回放算例不得取得 increment_access",
            field_path="cases.increment_access",
            actual=bad,
            requirement="全部为 False（回放的是端点几何，不是逐事件增量）",
            suggestion="要进主循环请走 U07 的 TabulatedEventLaw + 增量语义曲线。",
        )
