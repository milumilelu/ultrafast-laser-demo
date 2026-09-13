"""数据集注册与**权限判定**（U05）。

本模块回答一个问题：**一条实测记录，允许被怎么用？**

两种权限必须分开（这是 U05 的核心）

* ``observation_access`` —— 能否**浏览 / 回放 / 画散点 / 查来源**；
* ``increment_access``  —— 能否进**逐事件增量主循环**（改变形貌）。

历史问题（F06 的准入部分）是：一缺信息就**全禁**，于是「牌号未确认」这种
**工况完整度**问题把**观测**也一起挡掉了，真实数据进不来。
反过来，若为了放宽而无差别放行，又会把「累计深度当增量」这类**语义红线**松开。

所以本模块按两级判定：

硬门槛（**必须拦，不降级**）
    1. **单位冲突** —— 成对单位（µm/m、J/cm²/J/m²）彼此不自洽；
    2. **语义不明** —— ``output_semantics`` 缺失、空白，或不在已登记的观测语义内；
    3. **把模拟量当实验量** —— ``data_kind`` 声明为 model/synthetic/formula；
    4. **把累计量当增量** —— 见 :func:`assert_increment_access`，走既有闸门
       :func:`ufdemo.response.assert_increment_semantics`，**抛既定错误码
       ``RESPONSE_SEMANTICS_INVALID``，不新造码**。

软门槛（**只降权限 + 提示，不阻塞观测**）
    牌号未知 / 脉宽未确认 / 无重复次数 / 缺光斑定义 / 数字化读数容差等 →
    记入 ``risk_notes``，``observation_access`` 仍为 ``True``。

设计约束
--------
* **不导入 Streamlit**（本模块是纯逻辑，可被 UI 与契约层复用）；
* 语义常量与 :mod:`ufdemo.config` **共用同一来源**，不另立一套字符串；
* ``OBSERVATION_SEMANTICS`` 与 ``config.ALL_SEMANTICS``（曲线枚举，**不扩容**）
  是**两个概念轴**：前者只用于数据集观测，后者管曲线去向与事件核闸门。
  实测数据的 ``*_endpoint`` 语义**永不**进曲线枚举，**永不**进事件核。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .config import (
    SEMANTIC_CUMULATIVE,
    SEMANTIC_EVENT_INCREMENT,
    SEMANTIC_MEAN_RATE,
    SEMANTIC_THRESHOLD_ONLY,
    SEMANTIC_TRACK_PASS,
    SEMANTIC_VOLUME_PER_ENERGY,
)
from .errors import CONFIG_INVALID, UFDemoError

# ---------------------------------------------------------------------------
# 观测语义（**与 config.ALL_SEMANTICS 是两个轴**，不要把两者合并）
# ---------------------------------------------------------------------------

# 实测数据包声明的「端点几何量」语义：真实实验报告的是某个坑/某条线/某个孔
# 加工完的**端点**结果，不是逐事件增量。它们只服务观测入口。
SEMANTIC_SINGLE_PULSE_CRATER = "single_pulse_crater_endpoint"
SEMANTIC_SURFACE_ROUGHNESS = "surface_roughness_endpoint"
SEMANTIC_THROUGH_HOLE_GEOMETRY = "through_hole_geometry_endpoint"

#: 已登记的**观测语义**全集（= config 里除增量外的语义 + 数据包端点语义）。
#: 不在此集合且非空白的 ``output_semantics`` → 按「语义不明」**硬拦**。
#: 需要新增时**显式登记**（可审计的一步），不要静默放行。
OBSERVATION_SEMANTICS: tuple[str, ...] = (
    SEMANTIC_CUMULATIVE,
    SEMANTIC_MEAN_RATE,
    SEMANTIC_TRACK_PASS,
    SEMANTIC_VOLUME_PER_ENERGY,
    SEMANTIC_THRESHOLD_ONLY,
    SEMANTIC_SINGLE_PULSE_CRATER,
    SEMANTIC_SURFACE_ROUGHNESS,
    SEMANTIC_THROUGH_HOLE_GEOMETRY,
)

#: 观测语义的中文说明（用于界面与报告，避免用户误读）。
OBSERVATION_SEMANTICS_ZH: dict[str, str] = {
    SEMANTIC_CUMULATIVE: "某一完整照射协议的累计深度（不可重复累加）",
    SEMANTIC_MEAN_RATE: "给定历史下的平均去除率（不可直接加到表面）",
    SEMANTIC_TRACK_PASS: "单轨/单遍加工结果（不可再按脉冲重复应用）",
    SEMANTIC_VOLUME_PER_ENERGY: "总体体积效率（不可唯一确定局部坑形）",
    SEMANTIC_THRESHOLD_ONLY: "阈值/分类观测（不产生深度）",
    SEMANTIC_SINGLE_PULSE_CRATER: "单脉冲坑的端点几何（坑径/坑深），非逐事件增量",
    SEMANTIC_SURFACE_ROUGHNESS: "表面粗糙度端点（Sa/Ra），不是去除深度",
    SEMANTIC_THROUGH_HOLE_GEOMETRY: "贯穿孔端点几何（进/出口直径），非浅表 2.5D 形貌",
}

#: ``data_kind`` 中出现这些子串 → 视为「模拟量冒充实验量」，硬拦。
_NON_MEASURED_KIND_MARKERS = ("model", "synthetic", "formula", "simulated")

#: 成对单位：(惯用列, SI 列, 换算系数, 说明)。与 tools/measured_data_report.py 同一口径。
UNIT_PAIRS: tuple[tuple[str, str, float, str], ...] = (
    ("depth_um", "depth_m", 1e-6, "1 µm = 1e-6 m"),
    ("width_um", "width_m", 1e-6, "1 µm = 1e-6 m"),
    ("Ra_um", "Ra_m", 1e-6, "1 µm = 1e-6 m"),
    ("Sa_um", "Sa_m", 1e-6, "1 µm = 1e-6 m"),
    ("diameter_um", "diameter_m", 1e-6, "1 µm = 1e-6 m"),
    ("peak_fluence_J_cm2", "peak_fluence_J_m2", 1e4, "1 J/cm² = 1e4 J/m²"),
    ("efficiency_um3_per_uJ", "efficiency_m3_per_J", 1e-12, "1 µm³/µJ = 1e-12 m³/J"),
)

_UNIT_REL_TOL = 1e-9


class DatasetAccessError(UFDemoError):
    """数据集权限判定的错误。**沿用既有错误码**，不新造码。"""


@dataclass(frozen=True)
class AccessDecision:
    """一条数据集的权限判定结果。"""

    case_id: str | None
    observation_access: bool
    increment_access: bool
    output_semantics: str | None
    data_kind: str | None
    semantics_zh: str | None = None
    risk_notes: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "observation_access": self.observation_access,
            "increment_access": self.increment_access,
            "output_semantics": self.output_semantics,
            "output_semantics_zh": self.semantics_zh,
            "data_kind": self.data_kind,
            "risk_notes": list(self.risk_notes),
            "missing_fields": list(self.missing_fields),
            "reasons": list(self.reasons),
        }


def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    s = str(v).strip()
    return s == "" or s.lower() in ("null", "none", "nan")


def _num_or_none(v: Any) -> float | None:
    if _is_blank(v):
        return None
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def check_units(record: Mapping[str, Any]) -> list[str]:
    """成对单位自洽性检查。

    返回冲突描述列表；空列表表示通过。
    **只核对两份记录彼此自洽，不做任何隐式换算** ——
    缺一列不算冲突（那是「未提供」，按缺失处理）。
    """
    conflicts: list[str] = []
    for conv_col, si_col, factor, desc in UNIT_PAIRS:
        if conv_col not in record or si_col not in record:
            continue
        a, b = _num_or_none(record.get(conv_col)), _num_or_none(record.get(si_col))
        if a is None or b is None:
            continue
        expect = a * factor
        scale = max(abs(expect), abs(b))
        if scale > 0.0 and abs(expect - b) / scale > _UNIT_REL_TOL:
            conflicts.append(
                f"{conv_col}={a:g} × ({desc}) → {expect:g}，但 {si_col}={b:g}"
            )
    return conflicts


def check_data_kind(data_kind: Any) -> str | None:
    """``data_kind`` 是否如实声明为实测。返回问题描述或 ``None``。"""
    if _is_blank(data_kind):
        return "data_kind 缺失"
    k = str(data_kind).lower()
    for marker in _NON_MEASURED_KIND_MARKERS:
        if marker in k:
            return f"data_kind={data_kind!r} 含 {marker!r}，不得作为实测数据导入"
    return None


def evaluate(record: Mapping[str, Any]) -> AccessDecision:
    """判定单条记录的权限。**纯函数**，不触碰文件系统、不导入 Streamlit。

    硬门槛任一未过 → 抛 :class:`DatasetAccessError`（**既有错误码**）。
    软门槛只影响 ``risk_notes``，``observation_access`` 仍为 ``True``。
    """
    case_id = record.get("case_id")
    semantics = record.get("output_semantics")
    data_kind = record.get("data_kind")

    # --- 硬门槛 1：语义不明 ------------------------------------------------
    if _is_blank(semantics):
        raise DatasetAccessError(
            CONFIG_INVALID,
            "数据集缺少 output_semantics，无法判定权限",
            field_path="output_semantics",
            actual=semantics,
            requirement=f"非空，且取值属于已登记的观测语义 {list(OBSERVATION_SEMANTICS)}",
            suggestion="补齐语义；缺失语义的数据不得进入观测入口。",
        )
    semantics_s = str(semantics).strip()
    if semantics_s == SEMANTIC_EVENT_INCREMENT:
        # 增量语义出现在**数据集**里本身可疑：实测数据包不应声称逐事件增量。
        raise DatasetAccessError(
            CONFIG_INVALID,
            "数据集不得声明 event_depth_increment（那是曲线/响应的语义）",
            field_path="output_semantics",
            actual=semantics_s,
            requirement="实测数据集应声明端点观测语义",
            suggestion="逐事件增量属于曲线与响应核；实测端点数据请用 endpoint 语义。",
        )
    if semantics_s not in OBSERVATION_SEMANTICS:
        raise DatasetAccessError(
            CONFIG_INVALID,
            f"未登记的观测语义 {semantics_s!r}",
            field_path="output_semantics",
            actual=semantics_s,
            requirement=f"取值属于 {list(OBSERVATION_SEMANTICS)}",
            suggestion="新增语义需显式登记（可审计的一步），不要静默放行。",
        )

    # --- 硬门槛 2：模拟量冒充实验量 ---------------------------------------
    kind_problem = check_data_kind(data_kind)
    if kind_problem is not None:
        raise DatasetAccessError(
            CONFIG_INVALID,
            kind_problem,
            field_path="data_kind",
            actual=data_kind,
            requirement="声明为实测来源；模拟/公式生成的行不得混入实测数据集",
        )

    # --- 硬门槛 3：单位冲突 ------------------------------------------------
    conflicts = check_units(record)
    if conflicts:
        raise DatasetAccessError(
            CONFIG_INVALID,
            "同一记录内的成对单位不自洽",
            field_path="units",
            actual="；".join(conflicts),
            requirement="成对单位必须彼此自洽（不做隐式换算）",
            suggestion="核对原文单位与换算后重新转录。",
        )

    # --- 软门槛：完整度 → risk_notes（**不阻塞观测**）---------------------
    risks: list[str] = []
    missing: list[str] = []

    grade = record.get("grade")
    grade_required = str(record.get("grade_confirmation_required", "")).strip().lower()
    if _is_blank(grade):
        missing.append("grade")
        risks.append("牌号未提供/未确认：可浏览与回放，但不得声称代表用户实际工件")
    if grade_required in ("true", "1", "yes"):
        risks.append("原文要求确认牌号（grade_confirmation_required=True）")

    pd_fs = _num_or_none(record.get("pulse_duration_fs"))
    if pd_fs is None:
        missing.append("pulse_duration_fs")
        eq_min = _num_or_none(record.get("equipment_min_pulse_duration_fs"))
        if eq_min is not None:
            risks.append(
                f"脉宽未给出；原文只给设备最小脉宽 {eq_min:g} fs —— "
                "**不得**把设备最小值当作实际脉宽，也不得据此反推峰值强度"
            )
        else:
            risks.append("脉宽未给出：不得据此反推峰值强度或通量")

    if _num_or_none(record.get("repetition_rate_Hz")) is None:
        missing.append("repetition_rate_Hz")
        risks.append("重复频率未给出")

    if _is_blank(record.get("spot_diameter_um")) and _is_blank(record.get("reported_spot_diameter_um")):
        missing.append("spot_diameter")
        risks.append("光斑定义缺失：只能用原文能流查表，**不得**换算成脉冲能量")

    if _is_blank(record.get("wavelength_nm")):
        missing.append("wavelength_nm")
        risks.append("波长未给出")

    extraction = str(record.get("extraction_method", "") or "")
    if "digitized" in extraction.lower():
        risks.append("图为人工数字化读数，非仪器原始值；容差不等于实验标准差")

    note = record.get("quality_note")
    if not _is_blank(note) and "FLAG" in str(note).upper():
        risks.append(f"原始转录已标记异常：{str(note).strip()}")

    # 观测恒可（硬门槛已过）；增量**永不可**（实测端点语义不是逐事件增量）
    return AccessDecision(
        case_id=str(case_id) if case_id is not None else None,
        observation_access=True,
        increment_access=False,
        output_semantics=semantics_s,
        data_kind=str(data_kind) if data_kind is not None else None,
        semantics_zh=OBSERVATION_SEMANTICS_ZH.get(semantics_s),
        risk_notes=tuple(risks),
        missing_fields=tuple(missing),
        reasons=(
            "已过硬门槛（语义已登记、来源为实测、成对单位自洽）",
            "语义为端点观测语义，非逐事件增量 → increment_access=False",
        ),
    )


def assert_increment_access(record: Mapping[str, Any]) -> None:
    """请求**增量权限**时的闸门。

    实测端点数据一律拒绝，并**沿用既有语义闸门**抛出**既定错误码**
    ``RESPONSE_SEMANTICS_INVALID``（不是新造的码）。
    """
    from .response import assert_increment_semantics  # 延迟导入，避免环

    semantics = record.get("output_semantics")
    # 直接委派给既有闸门：它不是 event_depth_increment，必然被拒。
    assert_increment_semantics(semantics, field_path="dataset.output_semantics")


def evaluate_all(records: Iterable[Mapping[str, Any]]) -> list[AccessDecision]:
    """批量判定（任一硬门槛未过即抛，**不做部分放行**）。"""
    return [evaluate(r) for r in records]


def summarize(decisions: Sequence[AccessDecision]) -> dict[str, Any]:
    """汇总权限分布，供报告与 QA 使用。"""
    n = len(decisions)
    n_inc = sum(1 for d in decisions if d.increment_access)
    by_sem: dict[str, int] = {}
    for d in decisions:
        key = d.output_semantics or "(缺失)"
        by_sem[key] = by_sem.get(key, 0) + 1
    n_risky = sum(1 for d in decisions if d.risk_notes)
    return {
        "records": n,
        "observation_access_true": sum(1 for d in decisions if d.observation_access),
        "increment_access_true": n_inc,
        "records_with_risk_notes": n_risky,
        "by_output_semantics": dict(sorted(by_sem.items())),
    }


# ---------------------------------------------------------------------------
# 注册表：读 / 写 / 生成
# ---------------------------------------------------------------------------

REGISTRY_SCHEMA = "ufdemo.dataset_registry/1"


def registry_path(measured_dir: str | Path) -> Path:
    return Path(measured_dir) / "registry.json"


def load_registry(measured_dir: str | Path) -> dict[str, Any]:
    """读注册表。文件不存在时返回带 ``datasets=[]`` 的空壳（不抛）。"""
    p = registry_path(measured_dir)
    if not p.exists():
        return {"schema": REGISTRY_SCHEMA, "datasets": []}
    return json.loads(p.read_text(encoding="utf-8"))


def _measured_rows(measured_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    """读入全部实测 CSV 行（含文件名），跳过 jsonl（同一数据的另一表示）。"""
    import csv

    out: list[tuple[str, dict[str, Any]]] = []
    for p in sorted(measured_dir.glob("*.csv")):
        text = p.read_text(encoding="utf-8-sig")  # 去 BOM
        for row in csv.DictReader(text.splitlines()):
            out.append((p.name, dict(row)))
    return out


def build_registry(measured_dir: str | Path) -> dict[str, Any]:
    """从实测 CSV **生成**注册表。

    注册表是**产物**，不是手写真相：这样它与数据不会各自漂移。
    QA 会校验 ``registry.json`` 与当前数据一致（见 tools/measured_data_report.py）。
    """
    import hashlib

    measured = Path(measured_dir)
    rows = _measured_rows(measured)
    datasets: list[dict[str, Any]] = []
    decisions: list[AccessDecision] = []
    for filename, rec in rows:
        d = evaluate(rec)  # 硬门槛未过会抛 —— 生成阶段就暴露，而不是等到用
        decisions.append(d)  # **保留原始判定**，别在汇总时重建（会丢 risk_notes）
        datasets.append({
            "case_id": d.case_id,
            "file": filename,
            "material_family": rec.get("material_family"),
            "grade": rec.get("grade"),
            "material_name": rec.get("material_name"),
            "source_id": rec.get("source_id"),
            "source_doi": rec.get("source_doi"),
            "source_locator": rec.get("source_locator"),
            "data_kind": rec.get("data_kind"),
            "output_semantics": rec.get("output_semantics"),
            "output_semantics_zh": d.semantics_zh,
            "observation_access": d.observation_access,
            "increment_access": d.increment_access,
            "missing_fields": list(d.missing_fields),
            "risk_notes": list(d.risk_notes),
        })

    files: dict[str, str] = {}
    for p in sorted(measured.glob("*.csv")):
        files[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()

    return {
        "schema": REGISTRY_SCHEMA,
        "note": (
            "由 tools/measured_data_report.py --write-registry 生成，不要手改。"
            "observation_access=可浏览/回放；increment_access=可进逐事件核。"
            "实测端点语义一律无增量权限。"
        ),
        "observation_semantics": list(OBSERVATION_SEMANTICS),
        "counting_note": "每条记录=已发表条件/结果记录；非独立数据集、非材料卡。",
        "summary": summarize(decisions),
        "files_sha256": files,
        "datasets": datasets,
    }
