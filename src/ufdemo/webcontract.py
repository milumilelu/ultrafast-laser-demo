"""前后端契约层：把真实数据与求解结果转成 `webui/` 前端可用的 JSON。

**设计原则（不得违反）**

1. **不重写逻辑**：本模块只做「取数 + 序列化」。所有界面语义（图层可用性、
   诊断构建、措辞守卫、越界处理、水印）一律复用 :mod:`ufdemo.ui_service`，
   避免前后端各写一份而漂移。
2. **不导入 Streamlit / Plotly**：与 `ui_service` 同样的分层约定，
   本模块必须能在无浏览器环境下被断言。
3. **如实报不可用**：`threshold_only` 不返回数值 0，缺失图层给出**原因**，
   与 `ui_service.UNAVAILABLE_LAYERS` 同源。
4. **JSON 安全**：numpy 标量/数组、NaN/Inf、Path 一律转换；NaN/Inf → ``None``
   （JSON 没有 NaN，静默写 ``NaN`` 会让前端 ``JSON.parse`` 失败）。

契约形状与 `frontend-showcase/js/main.js` 的 ``mockSolve()`` 返回结构对齐，
因此前端从模拟数据切到真实求解时**不需要改渲染层**。
"""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import numpy as np

from . import ui_service as U
from .config import RunConfig, validate_run
from .errors import CONFIG_INVALID, UFDemoError
from .materials import load_material_card

# 前端图层选择器暴露的图层（顺序即界面顺序）
WEB_LAYERS: tuple[str, ...] = (
    "height",
    "depth",
    "cumulative_fluence",
    "illumination_count",
    "threshold_mask",
    "phase_id",
    "warning_mask",
)


class ContractError(Exception):
    """契约层错误（资源不存在 / 请求不合法 / 路径越界）。

    **为什么不复用 `UFDemoError`**：后者的错误码是一个**封闭集合**，
    只表达材料与求解语义（``CONFIG_INVALID``、``TABLE_OUT_OF_RANGE`` …）。
    "模板不存在"、"路径越界"属于 Web 层的资源定位问题，不属于那套语义，
    因此单独用本异常；由 :mod:`ufdemo.webapp` 映射成 HTTP 状态码。
    """

    def __init__(self, code: str, message: str, status: int = 400, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = int(status)
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field_path": self.extra.get("field_path"),
            "requirement": self.extra.get("requirement"),
            "suggestion": self.extra.get("suggestion"),
        }

# 快照里随传输的数组：时间轴只画形貌，故只带 ``height``。
# 每多带一个图层就多 ``帧数 × nx × ny`` 个数字（161² 一帧约 220 KB JSON），
# 而时间轴并不需要其余图层——需要时改用 ``snapshot_index`` 单独取该帧。
SNAPSHOT_ARRAYS: tuple[str, ...] = ("height",)


# ---------------------------------------------------------------------------
# JSON 安全转换
# ---------------------------------------------------------------------------


def jsonable(value: Any) -> Any:
    """递归把值转成 ``json.dumps`` 可安全序列化的形式。

    * numpy 标量 → Python 标量；``float32/64`` 保持双精度；
    * numpy 数组 → 嵌套 list（``float64`` 精度）；
    * ``NaN`` / ``±Inf`` → ``None``（**不**写 ``NaN``，否则前端解析失败）；
    * ``Path`` → ``str``；``tuple``/``set`` → ``list``；
    * ``bool`` 必须先于 ``int`` 判断（``bool`` 是 ``int`` 的子类）。
    """
    if value is None:
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, np.ndarray):
        flat = np.asarray(value)
        if flat.dtype.kind in "fc":
            out = flat.astype(np.float64)
            out = np.where(np.isfinite(out), out, np.nan)
            return [_num_or_none(x) for x in out.ravel().tolist()]
        return jsonable(flat.tolist())
    if isinstance(value, Path):
        return str(value)
    # dataclass（如 tables 的 TableLookup）必须展开成字典——
    # 否则会落到末尾的 str() 分支，变成 "TableLookup(curve_id='...')" 这种
    # 不可解析的 repr 字符串（前端读不到字段）。
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, str):
        return value
    return str(value)


def _num_or_none(x: Any) -> float | None:
    f = float(x)
    return f if math.isfinite(f) else None


def _array_payload(arr: Any) -> list[Any] | None:
    """数组 → 扁平 list（前端 ``Float64Array.from()`` 直接消费）。"""
    if arr is None:
        return None
    return jsonable(np.asarray(arr))


# ---------------------------------------------------------------------------
# 结果序列化：FrozenRun → 前端契约
# ---------------------------------------------------------------------------


def run_to_payload(
    frozen: U.FrozenRun,
    *,
    snapshot_index: int | None = None,
    material: Any = None,
) -> dict[str, Any]:
    """已提交/已读取的运行 → 前端契约。

    ``snapshot_index`` 为 ``None`` 时取最终状态；否则取该帧。
    ``blocked`` 直接来自 :meth:`FrozenRun.available_layers`，
    因此「不可用」永远带原因，不会是静默的空数组。
    """
    arrays = frozen.arrays_from(snapshot_index)
    avail = frozen.available_layers(snapshot_index)

    layers: dict[str, Any] = {}
    for name in WEB_LAYERS:
        if name not in arrays:
            continue
        if avail.get(name) is not None:
            continue  # 明确不可用 → 不进 layers，避免前端误当可用
        payload = _array_payload(arrays[name])
        if payload is not None:
            layers[name] = payload

    blocked = {k: v for k, v in avail.items() if v is not None}

    surface = frozen.surface()
    grid: dict[str, Any] = {}
    if surface is not None:
        grid = {
            "nx": int(surface.grid.nx),
            "ny": int(surface.grid.ny),
            "dx": float(surface.grid.dx_m),
            "dy": float(surface.grid.dy_m),
            "xs": jsonable(np.asarray(surface.x)),
            "ys": jsonable(np.asarray(surface.y)),
        }
    else:
        # **读历史**路径：result 为 None，surface 未重建，但盘上有网格信息。
        # 不补的话前端拿到 `grid={}`：时间轴 drawHeatmap 收到 undefined →
        # createImageData 抛 "Value is not of type 'long'"，回放画布永远空白；
        # 截面 Array.from(xs) 同样失败。见 ui_service._grid_meta_from_disk。
        gm = getattr(frozen, "grid_meta", None)
        if gm:
            grid = {
                "nx": int(gm["nx"]),
                "ny": int(gm["ny"]),
                "dx": float(gm.get("dx") or 0.0),
                "dy": float(gm.get("dy") or 0.0),
                "xs": jsonable(gm.get("xs")),
                "ys": jsonable(gm.get("ys")),
                "reconstructedFromDisk": True,
            }

    return {
        "schema": "ufdemo.web.run/1",
        "runId": frozen.run_id,
        "status": frozen.status,
        "statusZh": U.STATUS_ZH.get(frozen.status, frozen.status),
        "runMode": frozen.run_mode,
        "unitSystem": frozen.unit_system,
        "materialId": frozen.material_watermark.get("material_id"),
        "submittedAt": frozen.submitted_at,
        "inputHash": frozen.input_hash,
        "runDir": frozen.run_dir,
        "watermark": jsonable(frozen.material_watermark),
        "effectiveConfiguration": jsonable((getattr(frozen.result, "metadata", {}) or {}).get("effective_configuration")),
        "responseModel": jsonable((getattr(frozen.result, "metadata", {}) or {}).get("response_model")),
        "eventsProcessed": int(frozen.events_processed),
        "eventsTotal": int(frozen.events_total),
        "removalAvailable": bool(frozen.removal_available),
        "stats": jsonable(frozen.statistics),
        "roiStats": jsonable(getattr(frozen, "roi_statistics", [])),
        "warnings": jsonable(frozen.warnings),
        "errors": jsonable(frozen.errors),
        "elapsedS": jsonable(frozen.elapsed_s),
        "grid": grid,
        "layers": layers,
        "blocked": blocked,
        "snapshots": _snapshots_payload(frozen),
        "panels": {
            "threshold": jsonable(frozen.threshold_diagnostics),
            "acceleration": jsonable(frozen.acceleration_diagnostics),
            "geometry": jsonable(frozen.geometry_diagnostics),
            "structure": jsonable(frozen.structure_diagnostics),
        },
        "rows": {
            "threshold": jsonable(frozen.threshold_rows()),
            "acceleration": jsonable(U.acceleration_diagnostics_rows(frozen.acceleration_diagnostics)),
            "geometry": jsonable(U.geometry_diagnostics_rows(frozen.geometry_diagnostics)),
        },
        "provenance": {
            "verificationKind": "数值实现验证",
            "note": (
                "本结果为软件数值实现验证，**不含**实验复现结论；"
                "公式核查 / 数值实现验证 / 实验复现三栏分开记录。"
            ),
        },
    }


def _snapshots_payload(frozen: U.FrozenRun) -> list[dict[str, Any]]:
    """快照序列化：只带时间轴需要的数组，控制传输体积。"""
    out: list[dict[str, Any]] = []
    n = len(frozen.snapshots)
    for i in range(n):
        arrs = frozen._snapshot_arrays(i)
        meta = frozen.snapshots[i] if i < len(frozen.snapshots) else {}
        entry: dict[str, Any] = {
            "index": i,
            "final": i == n - 1,
            "label": jsonable({k: v for k, v in (meta or {}).items() if not hasattr(v, "shape")}),
        }
        for key in SNAPSHOT_ARRAYS:
            if key in arrs:
                entry[key] = _array_payload(arrs[key])
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# 求解：唯一调用求解器的入口
# ---------------------------------------------------------------------------


def _failure(errors: Iterable[Mapping[str, Any]], *, warnings: Iterable[str] = ()) -> dict[str, Any]:
    """失败结果：**如实**返回错误码，不静默降级、不自动换模式。"""
    return {
        "schema": "ufdemo.web.run/1",
        "status": "failed",
        "statusZh": U.STATUS_ZH.get("failed", "失败"),
        "errors": jsonable(list(errors)),
        "warnings": jsonable(list(warnings)),
        "removalAvailable": False,
        "layers": {},
        "blocked": {},
        "snapshots": [],
        "panels": {},
        "rows": {},
        "stats": {},
    }


def solve_payload(
    params: Mapping[str, Any],
    *,
    out_base: str | Path,
    project_root: str | Path,
    label: str = "web_run",
    curves_dir: str | Path | None = None,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """用**真实求解器**跑一次并返回契约。

    复用 :func:`ui_service.submit`（它内部完成准入校验、求解、落盘、
    以及全部诊断构建），因此这里不重复任何业务逻辑。
    校验或求解失败 → 返回 ``status="failed"`` 与错误列表，**不**伪造结果。
    """
    state = U.new_session(params)
    config_base = Path(project_root)
    # In a wheel ``project_root()`` points outside the installed resources;
    # use the packaged resource root for relative card paths in that case.
    if not (config_base / "data").is_dir():
        try:
            from . import resource_root
            config_base = resource_root()
        except Exception:
            pass
    try:
        # Resolve cards relative to the project/config root.  This keeps the
        # web entry point independent of the process working directory (and
        # therefore usable from an installed wheel).
        cfg = RunConfig.from_dict(dict(params), base_dir=str(config_base))
    except UFDemoError as err:
        return _failure([err.to_dict()])
    try:
        material = load_material_card(Path(cfg.material_card_file))
    except Exception as err:  # noqa: BLE001 - 材料卡缺失/损坏都要如实报
        return _failure(
            [
                {
                    "code": "MATERIAL_CARD_ERROR",
                    "message": f"材料卡加载失败：{err}",
                    "field_path": "material_card_file",
                    "requirement": "可读的材料卡 JSON",
                    "suggestion": "检查 material_card_file 路径与卡内容。",
                }
            ]
        )
    try:
        frozen = U.submit(
            state, material, out_base=out_base, project_root=project_root, label=label,
            curves_dir=curves_dir,   # U07：让 solver.response_curve 能被加载
            progress_callback=progress_callback,
        )
    except UFDemoError as err:
        # 准入失败 → 带上完整校验报告，前端逐条显示
        extra = list(getattr(state, "preview_errors", []) or [])
        errs = extra or [err.to_dict()]
        return _failure(errs)
    return run_to_payload(frozen, material=material)


def preview_payload(
    params: Mapping[str, Any], *, project_root: str | Path | None = None
) -> dict[str, Any]:
    """**只做准入校验**，不求解、不落盘（前端「预检」按钮用）。"""
    config_base = Path(project_root) if project_root is not None else None
    if config_base is not None and not (config_base / "data").is_dir():
        try:
            from . import resource_root
            config_base = resource_root()
        except Exception:
            pass
    try:
        cfg = RunConfig.from_dict(
            dict(params), base_dir=str(config_base) if config_base is not None else None
        )
    except UFDemoError as err:
        return {"ok": False, "errors": [err.to_dict()], "warnings": []}
    try:
        material = load_material_card(Path(cfg.material_card_file))
    except Exception as err:  # noqa: BLE001
        return {
            "ok": False,
            "errors": [
                {
                    "code": "MATERIAL_CARD_ERROR",
                    "message": f"材料卡加载失败：{err}",
                    "field_path": "material_card_file",
                    "requirement": "可读的材料卡 JSON",
                    "suggestion": "检查 material_card_file 路径。",
                }
            ],
            "warnings": [],
        }
    report = validate_run(cfg, material)
    return {
        "ok": bool(report.ok),
        "errors": jsonable(list(report.errors)),
        "warnings": jsonable(list(report.warnings)),
    }


# ---------------------------------------------------------------------------
# 清单类端点
# ---------------------------------------------------------------------------


def catalog_payload(material_dir: str | Path) -> list[dict[str, Any]]:
    """材料卡目录：身份 + 能力 + 逐模式门控（复用 ``ui_service.capability_gate``）。"""
    out: list[dict[str, Any]] = []
    material_root = Path(material_dir)
    card_files = list(material_root.glob("*.json"))
    # Calibration sidecars intentionally live outside data/materials so the
    # literature-card immutability ledger remains stable.  They are exposed in
    # the web catalog as selectable, explicitly experiment-conditioned cards.
    sidecar_dir = material_root.parent / "calibrations"
    if sidecar_dir.is_dir():
        card_files.extend(sidecar_dir.glob("*.json"))
    for card_file in sorted(card_files):
        try:
            m = load_material_card(card_file)
        except Exception:  # noqa: BLE001 - 坏卡跳过，不阻断整个目录
            out.append({"id": card_file.stem, "cardFile": card_file.name, "broken": True})
            continue
        caps: dict[str, Any] = {}
        for key, cap in (m.capabilities or {}).items():
            caps[str(key)] = {
                "available": bool(getattr(cap, "available", False)),
                "reason": getattr(cap, "reason", None),
                "missing": jsonable(getattr(cap, "missing", None)),
            }
        gates: dict[str, Any] = {}
        for mode in ("reference_case", "event_kernel", "threshold_only", "synthetic_demo", "multishot"):
            try:
                ok, reason = U.capability_gate(m, mode)
                gates[mode] = {"allowed": bool(ok), "reason": reason}
            except Exception:  # noqa: BLE001 - 门控缺失不致命
                gates[mode] = {"allowed": False, "reason": "门控不可用"}
        identity = dict(m.identity or {})
        modes = list(m.allowed_run_modes or ())
        out.append(
            {
                "id": m.id,
                "family": identity.get("family"),
                "grade": identity.get("grade"),
                "identityConfirmed": identity.get("material_identity_confirmed_by_user"),
                "evidenceStatus": m.evidence_status,
                "sourceType": m.source_type,
                "structureType": m.structure_type,
                "allowedRunModes": modes,
                # 能否输出**物理**深度：只有开放 reference_case（物理单位路径）才算。
                # 合成/阈值演示卡一律 False，界面据此禁止把 δ_ref 标成 µm。
                "physicalPredictionAllowed": "reference_case" in modes,
                "responseKind": (m.response or {}).get("kind"),
                "response": jsonable(m.response),
                "pulseDurationModels": jsonable(list(getattr(m, "pulse_duration_models", ()) or ())),
                "responseTrust": [
                    {
                        "id": str(model.get("id")),
                        "source": str(model.get("source", "experiment_calibration")),
                        "trustState": str(model.get("trust_state", "calibrated_interpolation")),
                        "validityDomain": jsonable(model.get("validity_domain") or {}),
                        "evidenceStatus": str(model.get("evidence_status", "engineering_effective_from_experiment")),
                    }
                    for model in (getattr(m, "pulse_duration_models", ()) or ())
                ],
                "thresholdCandidates": jsonable(list(m.threshold_candidates or ())),
                "multiResponse": jsonable(m.multi_response),
                "phases": jsonable(list(m.phases or ())),
                "validityDomain": jsonable(m.validity_domain),
                # 界面上「该卡在什么条件下才允许定量」——直接取自有效性域，
                # 不编造激光参数摘要（卡里没登记就不显示）。
                "validityScope": (m.validity_domain or {}).get("scope"),
                "validityNote": (m.validity_domain or {}).get("note"),
                "protocolId": (m.validity_domain or {}).get("protocol_id"),
                # 峰值能流是**源文献装置条件**，ADR-0021 起随协议存放，不再挂在卡片的
                # validity_domain 下；装配后的 reference_protocol 里读，输出字段名不变。
                "peakFluenceJm2": (m.reference_protocol or {}).get("reference_peak_fluence_J_m2"),
                "protocolFile": (m.reference_protocol or {}).get("protocol_file"),
                "sourceEquation": m.source_equation,
                "sourceFigureOrTable": m.source_figure_or_table,
                "limitations": jsonable((m.raw or {}).get("limitations")),
                "capabilities": caps,
                "gates": gates,
                "cardFile": m.raw.get("_card_file") if isinstance(m.raw, Mapping) else None,
                "cardPath": str(card_file),
                "cardSha256": m.card_sha256,
                "fixtureOnly": bool(m.fixture_only),
                "enabledByDefault": bool(m.enabled_by_default),
                "blockedReason": m.blocked_reason,
                "broken": False,
            }
        )
    return out


def materials_payload(material_dir: str | Path) -> dict[str, Any]:
    """材料卡目录 + 七材料能力入口（批次 H，含探针实跑结果）。"""
    rows = U.material_entry_rows(material_dir)
    summary = U.material_entry_summary(rows)
    return {
        "schema": "ufdemo.web.materials/1",
        "catalog": catalog_payload(material_dir),
        "entries": jsonable(rows),
        "summary": jsonable(summary),
        "capabilityGateNote": (
            "缺能力时在**配置层**拦截并显示原因，不自动降级、不静默填值。"
        ),
    }


def material_entries_payload(material_dir: str | Path) -> dict[str, Any]:
    """七材料能力入口（批次 H）：三类条目 + 探针实跑核验结果。

    三类含义（细则第 7 节）：

    * ``opened`` —— 已绑定真实能力，查得可用；
    * ``blocked`` —— 绑定 enforcement + 探针键 + 期望错误码，实跑须抛该码；
    * ``deferred`` —— **规格要求开放、实现尚未支持**的缺口，附探针证明当前打不开。

    ``deferred`` **不得**被读作已开放；``all_probes_hold`` 为假即表示拦截失效或缺口消失。
    """
    rows = U.material_entry_rows(material_dir)
    summary = U.material_entry_summary(rows)
    return {
        "schema": "ufdemo.web.materialEntries/1",
        "entries": jsonable(rows),
        "summary": jsonable(summary),
        "kinds": {
            "opened": "已绑真实能力并查得可用",
            "blocked": "已绑红线拦截（探针实跑须抛期望错误码）",
            "deferred": "规格要求开放但实现未支持——**缺口**，不得读作已开放",
        },
        "note": (
            "拦截失效或缺口消失时 all_probes_hold 会变为假；"
            "本表只反映探针实跑结果，不代替人工决策。"
        ),
    }


def examples_payload(examples_dir: str | Path) -> dict[str, Any]:
    """可用配置模板及其身份信息（前端「载入模板」用）。"""
    out: list[dict[str, Any]] = []
    for name in U.list_examples(examples_dir):
        try:
            raw = U.load_template(examples_dir, name)
        except Exception:  # noqa: BLE001 - 坏模板跳过并在列表里如实标注
            out.append({"name": name, "label": name, "broken": True})
            continue
        out.append(
            {
                "name": name,
                "label": raw.get("label") or name,
                "notes": raw.get("notes"),
                "runMode": raw.get("run_mode"),
                "materialId": raw.get("material_id"),
                "unitSystem": raw.get("unit_system"),
                "broken": False,
            }
        )
    return {"schema": "ufdemo.web.examples/1", "examples": out}


def curve_payload(curve: Any) -> dict[str, Any]:
    """曲线卡 → 契约（保留原始点、有效区间与能力行，**不做任何补值**）。"""
    return {
        "curveId": curve.curve_id,
        "materialId": curve.material_id,
        "materialIdentity": jsonable(getattr(curve, "material_identity", None)),
        "xQuantity": curve.x_quantity,
        "yQuantity": curve.y_quantity,
        "outputSemantics": curve.output_semantics,
        "fixedConditions": jsonable(curve.fixed_conditions),
        "protocol": jsonable(curve.protocol),
        "sourceFigureOrTable": curve.source_figure_or_table,
        "validRange": jsonable(curve.valid_range),
        "validRangeNote": curve.valid_range_note,
        "rawPoints": jsonable([list(p) for p in curve.raw_points]),
        "duplicateReport": jsonable(getattr(curve, "duplicate_report", None)),
        "evidenceStatus": curve.evidence_status,
        "sourceType": curve.source_type,
        "depthDirection": curve.depth_direction,
        "fluenceBasis": curve.fluence_basis,
        "notes": jsonable(curve.notes),
        "limitations": jsonable(curve.limitations),
        "capabilityRows": jsonable(U.curve_capability_rows(curve)),
    }


def curves_payload(curves_dir: str | Path) -> dict[str, Any]:
    """全部曲线卡（含原始点，CSV 是唯一来源）。"""
    out: list[dict[str, Any]] = []
    for card in U.list_curve_cards(curves_dir):
        try:
            curve = U.load_curve_card(curves_dir, card["name"])
        except Exception as err:  # noqa: BLE001
            out.append({"curveId": card.get("curve_id"), "name": card["name"], "broken": True,
                        "error": str(err)})
            continue
        payload = curve_payload(curve)
        payload["name"] = card["name"]
        # 入口分层（U04）：前端据此把 fixture（人工解析/合成/公式重算）
        # 从默认入口分流到「人工解析测试」，避免被读成材料实测数据。
        payload["entryClass"] = card.get("entry_class", "fixture")
        out.append(payload)
    return {"schema": "ufdemo.web.curves/1", "curves": out}


def datasets_payload(measured_dir: str | Path) -> dict[str, Any]:
    """实测数据集清单与**权限**（U05）。

    权限判定**复用 `ui_service.list_measured_datasets`**，后者又复用
    `datasets.evaluate` —— 全链路只有**一套**判定，界面与接口不会漂移。

    注册表缺失时返回 `available=False` + 原因，**不抛、不伪造**。
    """
    payload = U.list_measured_datasets(measured_dir)
    return {"schema": "ufdemo.web.datasets/1", **payload}


def cases_payload(measured_dir: str | Path) -> dict[str, Any]:
    """文献算例回放（U08）。

    **已知条件回放**：展示论文表格的实测条件与结果，**不经过模型**。
    不含三维形貌；等效脉冲数带强制标签（非真实事件序列）。
    """
    from .cases import CaseReplay, assert_cases_are_replay_only

    replay = CaseReplay.from_dir(measured_dir)
    assert_cases_are_replay_only(replay.cases)   # 红线：回放一律无 increment_access
    return replay.to_dict()


def diamond_evaluator_payload(measured_dir: str | Path) -> dict[str, Any]:
    """金刚石过程响应评估器摘要（U06）。

    **辅助手段，不替代逐事件物理引擎的验证**；不产生形貌、不求解网格。
    指标三档并报（训练 / 分组五折 / 留出），避免只报好看的留出数。
    """
    payload = U.diamond_evaluator_summary(measured_dir)
    return {"schema": "ufdemo.web.diamond_evaluator/1", **payload}


def _safe_curve_name(curves_dir: str | Path, name: str) -> str:
    """Validate a curve card selector and keep it below ``curves_dir``."""
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise ContractError("CURVE_NOT_FOUND", "曲线卡名称不合法", status=404, field_path="curve")
    if not name.endswith(".curve.json"):
        raise ContractError("CURVE_NOT_FOUND", "曲线卡名称不合法", status=404, field_path="curve")
    root = Path(curves_dir).resolve()
    candidate = (root / name).resolve()
    if candidate.parent != root:
        raise ContractError("CURVE_NOT_FOUND", "曲线路径越界", status=404, field_path="curve")
    if not candidate.is_file():
        raise ContractError("CURVE_NOT_FOUND", f"曲线卡不存在：{name}", status=404, field_path="curve")
    return name


def example_payload(examples_dir: str | Path, name: str) -> dict[str, Any]:
    """单个配置模板的**完整参数**（前端「载入/重置为该模板」用）。

    按需加载而非在清单里内联全部参数：清单只给元信息，避免一次传输
    11 个模板的完整配置。
    """
    safe = Path(name).name  # 防目录穿越：只取文件名部分
    if not safe.endswith(".json"):
        raise ContractError(
            "EXAMPLE_NOT_FOUND",
            "模板名必须是 .json",
            status=400,
            field_path="name",
            requirement="examples/ 下的 .json 文件名",
        )
    path = Path(examples_dir) / safe
    if not path.exists():
        raise ContractError(
            "EXAMPLE_NOT_FOUND",
            f"模板不存在：{safe}",
            status=404,
            field_path="name",
            requirement="examples/ 下已存在的模板",
            suggestion="从 /api/examples 的列表中选取。",
        )
    return {
        "schema": "ufdemo.web.example/1",
        "name": safe,
        "params": jsonable(U.load_template(examples_dir, safe)),
    }


def references_payload(examples_dir: str | Path) -> dict[str, Any]:
    """文献参考算例清单（批次 D；只复现公式，不求解网格）。"""
    from . import references as REF

    cases: list[dict[str, Any]] = []
    for name in U.list_examples(examples_dir):
        if "reference_case" not in name:
            continue
        try:
            case = REF.load_reference_case(Path(examples_dir) / name)
        except Exception:  # noqa: BLE001
            continue
        cases.append(
            {
                "id": name,
                "label": case.get("label") or name,
                "materialId": case.get("material_id"),
                "notes": case.get("notes"),
            }
        )
    return {
        "schema": "ufdemo.web.references/1",
        "cases": cases,
        "note": "参考评估器**不求解网格**、不产生形貌；平均率与累计深度不得进入逐事件主循环。",
    }


def runs_payload(runs_dir: str | Path, *, max_depth: int = 2) -> dict[str, Any]:
    """历史运行清单（读取不重算，因此这里不得调用 ``solve``）。"""
    return {
        "schema": "ufdemo.web.runs/1",
        "runs": jsonable(U.list_runs(runs_dir, max_depth=max_depth)),
    }


def read_run_payload(runs_dir: str | Path, run_id_or_dir: str) -> dict[str, Any]:
    """读取一个已有运行并返回契约（**只读盘，不重算**）。"""
    base = Path(runs_dir)
    target = Path(run_id_or_dir)
    if not target.is_absolute():
        candidate = base / run_id_or_dir
        if candidate.exists():
            target = candidate
        else:
            # ``/api/runs`` exposes the metadata run_id even for nested
            # layouts (runs/<group>/<id>).  Resolve that id by inspecting the
            # small metadata files rather than dropping the parent directory.
            target = base / run_id_or_dir.split("/")[-1]
            if not target.exists() and base.exists():
                for md_path in base.glob("*/metadata.json"):
                    try:
                        md = json.loads(md_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if str(md.get("run_id", "")) == run_id_or_dir:
                        target = md_path.parent
                        break
                if not target.exists():
                    for md_path in base.glob("*/*/metadata.json"):
                        try:
                            md = json.loads(md_path.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            continue
                        if str(md.get("run_id", "")) == run_id_or_dir:
                            target = md_path.parent
                            break
    # 防目录穿越：只允许 runs 根目录之下
    try:
        resolved = target.resolve()
        root = base.resolve()
        if root not in resolved.parents and resolved != root:
            raise ContractError(
                "RUN_NOT_FOUND",
                "运行目录不在 runs 根目录之下",
                status=404,
                field_path="run_dir",
                requirement="runs/ 之下的运行目录",
                suggestion="从 /api/runs 提供的列表中选取。",
            )
    except OSError:
        pass
    if not (target / "metadata.json").exists():
        raise ContractError(
            "RUN_NOT_FOUND",
            f"该目录不是有效运行（缺 metadata.json）：{target}",
            status=404,
            field_path="run_dir",
            requirement="含 metadata.json 的运行目录",
            suggestion="从 /api/runs 提供的列表中选取。",
        )
    state = U.new_session({})
    frozen = U.read_existing_run(state, target)
    payload = run_to_payload(frozen)
    payload["readFromDisk"] = True
    return payload


# ---------------------------------------------------------------------------
# 查表（越界绝不返回 0）
# ---------------------------------------------------------------------------


def lookup_payload(
    curves_dir: str | Path,
    curve_name: str,
    xs: Iterable[float],
    *,
    method: str = "linear",
    allow_out_of_range: bool = False,
) -> dict[str, Any]:
    """查表：越界项由 :func:`ui_service.table_lookup` 决定（``None`` 或抛错）。

    唯一越界错误码 ``TABLE_OUT_OF_RANGE``；``allow_out_of_range=True`` 时
    越界项返回 ``None``，**绝不返回 0、不外推、不钳端点**。
    """
    curve = U.load_curve_card(curves_dir, _safe_curve_name(curves_dir, curve_name))
    state = U.new_session({})
    solve_before = state.solve_count
    try:
        result = U.table_lookup(
            state, curve, list(xs), method=method, allow_out_of_range=allow_out_of_range
        )
    except UFDemoError as err:
        return {"ok": False, "errors": [err.to_dict()], "curveId": curve.curve_id}
    if state.solve_count != solve_before:
        raise AssertionError("查表不得触发求解：solve_count 发生了变化")
    return {
        "ok": True,
        "schema": "ufdemo.web.lookup/1",
        "curveId": curve.curve_id,
        "method": method,
        "allowOutOfRange": bool(allow_out_of_range),
        "result": jsonable(result),
        "solveCountUnchanged": True,
    }


def curve_grid_payload(
    curves_dir: str | Path, curve_name: str, *, n: int = 200, method: str = "linear"
) -> dict[str, Any]:
    """插值图数据（不触发求解）。"""
    if isinstance(n, bool) or not isinstance(n, int) or n < 2 or n > 5000:
        raise ContractError(
            "BAD_REQUEST",
            "n 必须是 2..5000 的整数",
            status=400,
            field_path="n",
            requirement="整数采样点数，范围 2..5000",
        )
    curve = U.load_curve_card(curves_dir, _safe_curve_name(curves_dir, curve_name))
    state = U.new_session({})
    before = state.solve_count
    grid = U.table_grid(state, curve, n=n, method=method)
    if state.solve_count != before:
        raise AssertionError("绘图网格不得触发求解：solve_count 发生了变化")
    return {"ok": True, "curveId": curve.curve_id, "grid": jsonable(grid)}


def reference_payload(
    examples_dir: str | Path, case_name: str, *, project_root: str | Path | None = None
) -> dict[str, Any]:
    """文献参考算例评估（批次 D）：只复现公式与协议量，**不求解网格**。

    ``values`` 在契约里转成 ``[[名称, 值, 单位], ...]``：参考量的键名是给程序看的，
    界面需要可读标签与单位，转换在契约层做一次，前端不再猜。
    """
    from . import references as REF
    from . import project_root as _pr

    root = Path(project_root) if project_root is not None else _pr()
    safe = Path(case_name).name
    path = Path(examples_dir) / safe
    if not path.exists():
        raise ContractError(
            "REFERENCE_CASE_NOT_FOUND",
            f"参考算例不存在：{safe}",
            status=404,
            field_path="case",
            requirement="examples/*_reference_case.json",
            suggestion="从 /api/references 的列表中选取。",
        )
    case = REF.load_reference_case(path)
    card_file = case.get("material_card_file")
    material = None
    if card_file:
        cand = Path(card_file)
        material = load_material_card(cand if cand.is_absolute() else (root / cand))
    res = REF.ReferenceEvaluator.evaluate_case(case, material)

    values = dict(getattr(res, "values", {}) or {})
    units = dict(getattr(res, "units", {}) or {})
    labels = dict(getattr(res, "labels", {}) or {})
    rows: list[list[Any]] = []
    for key, val in values.items():
        if isinstance(val, (list, tuple, dict)):
            continue  # 嵌套结构另由 sweep 等字段承载
        rows.append([
            labels.get(key) or key,
            jsonable(val),
            units.get(key, ""),
        ])
    return {
        "schema": "ufdemo.web.reference/1",
        "caseId": getattr(res, "case_id", safe),
        "materialId": getattr(res, "material_id", None),
        "referenceKind": getattr(res, "reference_kind", None),
        "outputSemantics": getattr(res, "output_semantics", None),
        "protocolId": getattr(res, "protocol_id", None),
        "evidenceStatus": getattr(res, "evidence_status", None),
        "conditionMatch": getattr(res, "condition_match", None),
        "values": jsonable(rows),
        "rawValues": jsonable(values),
        "effectiveCount": jsonable(getattr(res, "effective_count", None)),
        "eventCount": jsonable(getattr(res, "event_count", None)),
        "sourceEquation": getattr(res, "source_equation", None),
        "sourceFigureOrTable": getattr(res, "source_figure_or_table", None),
        "engineeringExtension": getattr(res, "engineering_extension", None),
        "verifiedByFormula": bool(getattr(res, "verified_by_formula", False)),
        "verifiedByExperiment": bool(getattr(res, "verified_by_experiment", False)),
        "eventKernelAllowed": bool(getattr(res, "event_kernel_allowed", False)),
        "notes": jsonable(list(getattr(res, "notes", []) or [])),
        "warnings": jsonable(list(getattr(res, "warnings", []) or [])),
        "provenance": {
            "verificationKind": "公式核查",
            "note": "参考评估器只做**公式核查**，不求解网格、不产生形貌；实验复现栏为空。",
        },
    }


def guard_depth_export_payload(unit_system: str, filename: str) -> dict[str, Any]:
    """导出前口径检查（合成模式禁止导出物理 ``depth_um``）。"""
    ctx = U.UnitContext.from_config(
        {
            "unit_system": unit_system,
            "reference_scales": {"L_ref_m": 1e-5, "F_ref_J_m2": 1e4, "delta_ref_m": 1e-7},
        }
    )
    try:
        name = U.guard_depth_export(ctx, filename)
        return {"ok": True, "filename": name, "depthLabel": U.depth_export_label(ctx)}
    except UFDemoError as err:
        return {"ok": False, "errors": [err.to_dict()]}


# ===========================================================================
# V2（C2–C5）：三工作区所需的契约
# ===========================================================================
#
# 设计原则（任务书 §7）：
# * **薄 HTTP 层 → 单一业务服务 → 数值核心** —— 这里只做"参数 → 数值核心 →
#   前端可消费的 dict"，不写业务逻辑（业务在 calibration/planning/upstream 里）；
# * 实验表在仓库**外**（上层 `数据/`），找不到时**如实返回空 + 说明**，
#   不伪造数据；
# * 上游样例可能带 ``is_synthetic``，**必须原样透传**，让界面能显示"虚拟输入"。


def experiment_tables_payload(exp_dir: str | Path) -> dict[str, Any]:
    """列出可导入的实验 CSV（C4）。

    实验表通常放在**仓库外**（`../数据/`）。找不到时返回空列表 + 原因，
    而不是编一张假表 —— 界面据此显示"未找到实验数据目录"。
    """
    d = Path(exp_dir)
    if not d.exists():
        return {
            "dir": str(d), "found": False,
            "tables": [],
            "note": f"未找到实验数据目录：{d}（实验 CSV 在仓库外，属正常情况）",
        }
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob("*.csv")):
        item: dict[str, Any] = {"file": p.name, "ok": False}
        try:
            from .calibration import load_experiment_csv

            t = load_experiment_csv(p)
            item.update({
                "ok": True,
                "encoding": t.encoding,
                "nRows": t.n_used,
                "nRaw": t.n_raw_rows,
                "nSkipped": len(t.skipped_rows),
                "nNegative": sum(1 for r in t.rows if r.mean_depth_um < 0),
                "notes": list(t.notes),
                "pulseDurationsFs": sorted({r.pulse_duration_fs for r in t.rows}),
                "frequenciesKHz": sorted({r.repetition_rate_kHz for r in t.rows}),
                "spacingsUm": sorted({r.hatch_spacing_um for r in t.rows}),
                "passCounts": sorted({r.pass_count for r in t.rows}),
            })
        except Exception as exc:  # 单张表读不了不该拖垮整个列表
            item["error"] = f"{type(exc).__name__}: {exc}"
        out.append(item)
    return {"dir": str(d), "found": True, "tables": out, "note": ""}


def baselines_payload(baselines_dir: str | Path) -> dict[str, Any]:
    """列出已反推的候选基线（C3 产物）。"""
    d = Path(baselines_dir)
    items: list[dict[str, Any]] = []
    if d.exists():
        for p in sorted(d.glob("*.json")):
            try:
                items.append({"file": p.name, **json.loads(p.read_text(encoding="utf-8"))})
            except Exception as exc:
                items.append({"file": p.name, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "dir": str(d), "baselines": items,
        "note": "候选基线是**工程有效参数**（对同批实验拟合所得），"
                "不是独立实测材料常数；是否升级材料卡证据状态需人工确认。",
    }


def upstream_payload(upstream_dir: str | Path) -> dict[str, Any]:
    """列出可用的上游轮廓样例（C2）。"""
    d = Path(upstream_dir)
    items: list[dict[str, Any]] = []
    if d.exists():
        for case in sorted(d.glob("*/upstream_case.json")):
            try:
                from .upstream import load_upstream_case

                prof, wnote = load_upstream_case(case)
                items.append({
                    "name": case.parent.name,
                    "case": prof.to_dict(),
                    "radiusNote": wnote,
                })
            except Exception as exc:
                items.append({"name": case.parent.name,
                              "error": f"{type(exc).__name__}: {exc}"})
    return {"dir": str(d), "cases": items,
            "note": "带 isSynthetic=true 的是**虚拟输入**，仅用于展示与链路测试。"}


def upstream_compare_payload(body: Mapping[str, Any], *, project_root: str | Path) -> dict[str, Any]:
    """上下游**同工况**对照（C2）。下游用同一套求解器重放。"""
    import json as _json

    from .materials import MaterialSpec, load_material_card
    from .solver import solve
    from .upstream import UpstreamProfile, compare_profiles, load_upstream_case

    d = Path(str(body.get("caseDir") or ""))
    case_json = d / "upstream_case.json"
    if not case_json.exists():
        raise UFDemoError(CONFIG_INVALID, "找不到上游工况文件",
                          field_path="upstream.caseDir", actual=str(d),
                          requirement="目录内含 upstream_case.json")
    up, _ = load_upstream_case(case_json)

    cfg_raw = dict(body.get("params") or {})
    if not cfg_raw:
        raise UFDemoError(CONFIG_INVALID, "缺少下游参数",
                          field_path="upstream.params", actual=None, requirement="RunConfig 字典")
    cfg = RunConfig.from_dict(cfg_raw, base_dir=str(project_root))
    mat_path = Path(cfg.material_card_file)
    material = load_material_card(mat_path)

    res = solve(cfg, material)
    if res.status != "completed" or res.surface is None:
        return {"ok": False, "status": res.status,
                "errors": [e.get("code") for e in (res.errors or [])]}

    import numpy as np

    drop = res.surface.initial_height - res.surface.height
    nx = drop.shape[1]
    xs = (np.arange(nx) - nx // 2) * cfg.grid.dx_m
    down = UpstreamProfile(
        case_id="downstream", geometry=up.geometry,
        x_um=[float(v) * 1e6 for v in xs],
        depth_um=[float(v) * 1e6 for v in drop[drop.shape[0] // 2]],
        x_axis=up.x_axis,
    )
    m = compare_profiles(up, down)
    return {
        "ok": True,
        "upstream": up.to_dict(),
        "isSynthetic": up.is_synthetic,
        "metrics": m.to_dict(),
        "warning": ("上游为**虚拟输入**：该对照只验证链路，不构成上游验证结论。"
                    if up.is_synthetic else ""),
    }


def calibrate_payload(body: Mapping[str, Any], *, project_root: str | Path) -> dict[str, Any]:
    """执行一次实验标定（C4）。**参数真正改变求解过程**。"""
    from .calibration import (
        ObservationSpec, PredictionSpec, calibrate_gain, group_split,
        load_experiment_csv,
    )

    exp_file = Path(str(body.get("experimentFile") or ""))
    if not exp_file.exists():
        raise UFDemoError(CONFIG_INVALID, "找不到实验 CSV",
                          field_path="calibration.experimentFile", actual=str(exp_file),
                          requirement="文件存在")
    t = load_experiment_csv(exp_file)
    tau = float(body.get("pulseDurationFs") or 0.0)
    if tau <= 0:
        cands = sorted({r.pulse_duration_fs for r in t.rows})
        if not cands:
            raise UFDemoError(CONFIG_INVALID, "实验表没有可用行",
                              field_path="calibration.experimentFile", actual=0,
                              requirement="≥ 1 行")
        tau = cands[0]
    rows = [r for r in t.rows if abs(r.pulse_duration_fs - tau) < 1e-9]
    fit_rows = [r for r in rows if r.use_for_fit is not False and r.mean_depth_um > 0]
    quality = {
        "nRawRows": t.n_raw_rows,
        "nParsedRows": len(t.rows),
        "nSelectedPulseDuration": len(rows),
        "nNonPositiveRetained": sum(1 for r in rows if r.mean_depth_um <= 0),
        "nExcludedByQuality": sum(1 for r in rows if r.use_for_fit is False),
        "excludedReasons": [
            {"sampleId": r.sample_id, "sourceRow": r.source_row,
             "reason": ("use_for_fit=false" if r.use_for_fit is False else "non_positive_depth")}
            for r in rows if r.use_for_fit is False or r.mean_depth_um <= 0
        ],
    }
    if len(fit_rows) < 4:
        return {"ok": False,
                "quality": quality,
                "errors": [{"code": "INSUFFICIENT_ROWS",
                            "message": f"脉宽 {tau:g} fs 的可用拟合行不足（{len(fit_rows)}）"}]}
    card = str(body.get("materialCardFile") or "")
    if not card:
        raise UFDemoError(CONFIG_INVALID, "缺少基座材料卡",
                          field_path="calibration.materialCardFile", actual=None,
                          requirement="卡路径")
    override = dict(body.get("responseOverride") or {}) or None
    # 多遍终态必须按完整加工轨迹分组；不能让同一工艺的 N=1…N
    # 被随机拆到训练与留出两侧。
    tr, ho, info = group_split(
        fit_rows, holdout_groups=max(1, len(fit_rows) // 5), group_by_trajectory=True
    )
    # --- 观测口径与加工几何：**显式声明，不得静默用默认**（ADR-0021）----------
    # 实验表里**没有**"加工区域多大 / 平均深度是哪个窗口的均值"这两列。
    # 曾经硬编码 window_um=20 ⇒ 标定实际仿真的是 **20×20 μm 的弓字形面扫描**，
    # 且统计口径 = 全区域均值 —— 与 CSV 里声明的平均深度是不是同一个东西，
    # 取决于实验实际扫了多大、怎么统计的。这两件事只能由**使用者声明**。
    # 口径优先级：**显式传入 > 背景声明的实验统计口径 > 20 μm 假设**
    from .config import load_shared_background as _lsb

    _bg_decl = _lsb()
    _declared_region = _bg_decl.experiment_machined_region_um
    _declared_stat = _bg_decl.experiment_depth_statistic
    win_in = float(body.get("calibrationWindowUm") or 0.0)
    reg_in = body.get("calibrationRegionUm")
    obs_kind = str(body.get("calibrationObservationKind")
                   or _declared_stat or "full_region_mean")
    if obs_kind not in ("full_region_mean", "center_window_mean"):
        raise UFDemoError(
            CONFIG_INVALID, "calibrationObservationKind 取值非法",
            field_path="calibration.calibrationObservationKind", actual=obs_kind,
            requirement="full_region_mean | center_window_mean",
        )
    cw = body.get("calibrationCenterWindowUm")
    center_window = (float(cw[0]), float(cw[1])) if cw else None
    if obs_kind == "center_window_mean" and not center_window:
        raise UFDemoError(
            CONFIG_INVALID, "center_window_mean 必须给出窗口尺寸",
            field_path="calibration.calibrationCenterWindowUm", actual=cw,
            requirement="[wx, wy]（μm）",
        )
    if reg_in is not None:
        if isinstance(reg_in, (list, tuple)):
            _region_pair = (float(reg_in[0]), float(reg_in[1]))
        else:
            _region_pair = (float(reg_in), float(reg_in))
        _region_source = "explicit"
    elif _declared_region is not None:
        _region_pair = _declared_region
        _region_source = "declared_in_background"
    else:
        _region_pair = (20.0, 20.0)
        _region_source = "assumed"
    window = win_in if win_in > 0 else max(_region_pair)
    if window < max(_region_pair):
        raise UFDemoError(
            CONFIG_INVALID, "标定仿真域必须 ≥ 加工区",
            field_path="calibration.calibrationWindowUm",
            actual={"window_um": window, "region_um": list(_region_pair)},
            requirement="window_um ≥ max(加工区)",
        )
    _region_source_desc = {
        "explicit": "使用者显式传入",
        "declared_in_background": "背景声明的实验统计口径（用户 2026-09-15 确认 200×200 μm 全区均值）",
        "assumed": "20 μm 假设",
    }[_region_source]
    assumptions: dict[str, Any] = {
        "windowUm": window,
        "regionUm": list(_region_pair),
        "observationKind": obs_kind,
        "centerWindowUm": list(center_window) if center_window else None,
        "regionSource": _region_source,
        "assumed": _region_source == "assumed",
    }
    spec = PredictionSpec(
        material_card_file=card,
        window_um=assumptions["windowUm"], dx_um=0.5,
        machining_region_um=assumptions["regionUm"],
        observation=ObservationSpec(kind=obs_kind, center_window_um=center_window),
        response_override=override,
    )
    res = calibrate_gain(tr, spec=spec, holdout_rows=ho,
                         bounds=(float(body.get("gainMin") or 0.05),
                                 float(body.get("gainMax") or 20.0)),
                         residual_scale_um=float(body.get("residualScaleUm") or 5.0),
                         material_id=str(body.get("materialId") or ""))
    d = res.to_dict()
    d["ok"] = True
    d["quality"] = quality
    d["split"] = info
    d["pulseDurationFs"] = tau
    d["observationBasis"] = {
        **assumptions,
        "note": (
            "标定仿真的是上述尺寸的弓字形面扫描，统计口径见 observationKind。"
            + ("⚠️ **加工区域未声明**，按 20 μm 假设 —— 实验实际扫了多大、"
               "平均深度怎么统计的，需要使用者确认后显式传入。"
               if assumptions["assumed"] else
               f"（加工区域来自{_region_source_desc}）")
        ),
    }
    # 过拟合判定放在**服务层**做一次，界面直接显示，避免前端各写一套
    hb = (res.metrics()["holdout"]["mae_before_um"], res.metrics()["holdout"]["mae_after_um"])
    d["overfitWarning"] = bool(hb[0] is not None and hb[1] is not None and hb[1] > hb[0])
    if d["overfitWarning"]:
        d["overfitMessage"] = (
            "留出集在标定后**变差** → 过拟合。训练集变好不代表模型更可信；"
            "应改用更细的分组标定，而不是继续调全局增益。"
        )
    return d


def plan_payload(body: Mapping[str, Any], *, project_root: str | Path) -> dict[str, Any]:
    """执行 h/N 目标筛选（C5）。每个候选都真的跑同一套已标定求解器。"""
    from .planning import plan_for_target

    card = str(body.get("materialCardFile") or "")
    if not card:
        raise UFDemoError(CONFIG_INVALID, "缺少基座材料卡",
                          field_path="planning.materialCardFile", actual=None,
                          requirement="卡路径")
    region = body.get("regionUm") or [200.0, 200.0]
    domain = body.get("domainUm") or region
    res = plan_for_target(
        material_card_file=card,
        target_depth_um=float(body.get("targetDepthUm") or 0.0),
        tolerance_um=float(body.get("toleranceUm") or 0.0),
        pulse_duration_fs=float(body.get("pulseDurationFs") or 500.0),
        repetition_rate_kHz=float(body.get("repetitionRateKHz") or 10.0),
        scan_speed_mm_s=float(body.get("scanSpeedMmS") or 50.0),
        region_um=(float(region[0]), float(region[1])),
        domain_um=(float(domain[0]), float(domain[1])),
        dx_um=float(body.get("dxUm") or 0.5),
        auto_coarsen=bool(body.get("autoCoarsen", True)),
        gain=float(body.get("gain") or 1.0),
        response_override=dict(body.get("responseOverride") or {}) or None,
        # 窗口半径策略（性能开关，默认关闭；ADR-0017）。启用后枚举**快一个量级**，
        # 去除量逐位不变，但剂量观测量口径随结果一并上报（见 plan 结果的
        # windowRadiusPolicy / 各候选的 windowCellsSkippedFraction）。
        window_radius_policy=str(body.get("windowRadiusPolicy") or "tail_epsilon"),
        window_threshold_margin=float(body.get("windowThresholdMargin") or 1.25),
    )
    d = res.to_dict()
    d["ok"] = True
    return d


def demo_rect_payload(
    body: Mapping[str, Any],
    *,
    out_base: str | Path,
    project_root: str | Path,
    curves_dir: str | Path | None = None,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    estimate_only: bool = False,
) -> dict[str, Any]:
    """**极简演示端点**：给材料卡 + 间距/层数/区域，跑一次矩形槽。

    为什么另开一个端点而不是让前端拼完整配置：
    协议参数（波长/脉宽/频率/光斑）必须与材料卡的 ``reference_protocol`` 一致，
    **手填极易不匹配而被门控拒绝**（实测：500 fs / 20 kHz / 0.87 µm 全被拒）。
    所以这里**从卡里自动读**，速度再由 ``N_eff=(π/4)(2w₀f)/v`` 反推，
    保证"演示怎么点都能跑通"，且跑出来的仍是**真实材料卡的定量结果**。
    """
    import math

    from .materials import load_material_card
    from .planning import serpentine_plan

    card_rel = str(body.get("materialCardFile") or "")
    if not card_rel:
        raise UFDemoError(CONFIG_INVALID, "缺少材料卡", field_path="materialCardFile")
    base = Path(project_root)
    card_path = Path(card_rel)
    if not card_path.is_absolute():
        card_path = base / card_rel
    spec = load_material_card(card_path)

    rp = spec.reference_protocol or {}
    rl = rp.get("required_laser") or {}
    process_mode = str(body.get("processMode") or "reference").strip().lower()
    actual_process = process_mode in {"actual", "machine", "local", "calibrated"}
    response_model_id = str(body.get("responseModelId") or body.get("calibrationModelId") or "").strip() or None
    # 「源文献装置描述」（w0/f 等）—— ADR-0021 补记后不再参与门禁，
    # 但 demo 仍从这里取 w0/f 来装配能流场与扫描速度（保持既有演示行为不变）。
    sb = rp.get("source_beam") or {}

    def _v(key: str) -> float:
        x = (rl.get(key) or {}).get("value")
        if not (isinstance(x, (int, float)) and x > 0):
            x = (sb.get(key) or {}).get("value") if isinstance(sb.get(key), Mapping) else sb.get(key)
        if not isinstance(x, (int, float)) or x <= 0:
            raise UFDemoError(
                CONFIG_INVALID,
                f"材料卡协议未声明 {key}（required_laser / source_beam 均无），无法自动配置",
                field_path=f"reference_protocol.required_laser.{key}",
                requirement="带正 value 的条目",
                suggestion="换用带完整 reference_protocol 的材料卡，或走完整版界面手填参数。",
            )
        return float(x)

    # The canonical actual-process branch inherits the shared instrument
    # background.  The legacy/reference branch remains available explicitly
    # for literature-card demonstrations and backwards-compatible clients.
    bg = None
    if actual_process:
        from .config import load_shared_background, shared_background_patch

        bg = load_shared_background()
        w0 = bg.derived_waist_m()
        lam = bg.wavelength_m
    else:
        w0 = _v("spot_radius_m")
        lam = _v("wavelength_m")

    # 允许请求**覆盖**脉宽与重复频率（演示页要手动调参）。
    # ⚠️ 覆盖 ≠ 绕过门禁：τ 与 N_eff 正是 validate_run 要校验的量，下面的实际值照样
    #    写进 laser / reference_conditions —— 对就放行，错就以 CONDITION_MISMATCH 拒绝。
    _tau_req = float(body.get("pulseDurationFs") or 0.0)
    _freq_req = float(body.get("repetitionRateKHz") or 0.0)
    if _tau_req > 0:
        tau = _tau_req * 1e-15
    else:
        try:
            tau = _v("pulse_duration_s")
        except UFDemoError:
            # The shared background deliberately does not invent a pulse
            # duration.  Keep the UI default explicit when a card has no
            # duration entry; it is recorded in the effective configuration.
            tau = 500.0e-15
    if _freq_req > 0:
        freq = _freq_req * 1e3
    else:
        freq = _v("repetition_rate_Hz")

    card_n_eff = (rp.get("required_history") or {}).get("effective_count")
    card_n_eff = (float(card_n_eff)
                  if isinstance(card_n_eff, (int, float)) and card_n_eff > 0 else 3.0)
    _speed_req = float(body.get("speedMmS") or 0.0)
    if _speed_req > 0:
        speed_m_s = _speed_req * 1e-3
    else:
        # 默认速度取**源文献装置的扫描速度**（协议 source_beam 里声明）。
        # ⚠️ 不再用 N_eff 反推速度 —— 那是旧的联动逻辑，会把 f 与 v 绑死。
        # 只在协议没声明速度时才回退到反推（兼容尚未补该字段的卡）。
        _sb_speed = sb.get("scan_speed_mm_s")
        if isinstance(_sb_speed, (int, float)) and float(_sb_speed) > 0:
            speed_m_s = float(_sb_speed) * 1e-3
        else:
            if actual_process:
                raise UFDemoError(
                    CONFIG_INVALID,
                    "实际工艺模式缺少扫描速度",
                    field_path="scanSpeedMmS",
                    actual=None,
                    requirement="请求给出正的 scanSpeedMmS，或材料 source_beam 声明 scan_speed_mm_s",
                    suggestion="不使用 N_eff 反推实际扫描速度；补齐设备工艺输入。",
                )
            speed_m_s = (math.pi / 4.0) * (2.0 * w0 * freq) / card_n_eff
    # N_eff 现在只是**由路径导出的参考量**（用于回显），不再是任何条件
    n_eff = (math.pi / 4.0) * (2.0 * w0 * freq) / speed_m_s

    # 峰值能流属**源文献装置条件**：ADR-0021 起随协议存放（装配后从 reference_protocol 读）
    if actual_process:
        assert bg is not None
        actual_optics_patch = shared_background_patch(bg, repetition_rate_Hz=freq)
        energy = float(actual_optics_patch["laser"]["pulse_energy_J"])
        w0 = float(actual_optics_patch["laser"]["spot_radius_m"])
        peak = 2.0 * energy / (math.pi * w0 * w0)
        rayleigh_m = float(actual_optics_patch["laser"]["rayleigh_range_m"])
        geometry_feedback = bg.geometry_feedback
        m2_value = bg.m2
        # Keep the physical reference-case gate active.  ``processMode`` only
        # selects the instrument/path assembly; it must not bypass wavelength
        # or pulse-duration applicability checks in the material card.
        run_mode = "reference_case"
    else:
        peak = rp.get("reference_peak_fluence_J_m2")
        if not isinstance(peak, (int, float)) or peak <= 0:
            raise UFDemoError(
                CONFIG_INVALID, "参考协议未声明 reference_peak_fluence_J_m2，无法自动配置脉冲能量",
                field_path="reference_protocol.reference_peak_fluence_J_m2",
                requirement="正数（J/m²）",
                suggestion="在 data/protocols/<protocol_id>.json 里补 reference_peak_fluence_J_m2，"
                           "或走完整版界面手填脉冲能量。",
            )
        peak = float(peak)
        energy = peak * math.pi * w0 * w0 / 2.0   # F0 = 2E/(πw₀²)
        actual_optics_patch = None
        rayleigh_m = None
        geometry_feedback = "fixed_geometry"
        m2_value = None
        run_mode = "reference_case"

    resp = spec.response or {}
    region = float(body.get("regionUm") or 200.0)
    hatch = float(body.get("hatchUm") or 2.0)
    passes = int(body.get("passes") or 4)
    dx = float(body.get("dxUm") or 0.25)
    margin = float(body.get("marginUm") or 10.0)
    domain = region + 2.0 * margin

    plan = serpentine_plan(
        region_um=(region, region), spacing_um=hatch,
        pass_count=passes, scan_speed_mm_s=speed_m_s * 1e3,
    )
    segs = plan.to_segments_config()
    n = max(4, int(round(domain * 1e-6 / (dx * 1e-6))))

    cfg: dict[str, Any] = {
        "schema_version": "1.0",   # 与 config.SCHEMA_VERSION 一致
        "label": str(body.get("label") or "demo_rect"),
        "run_mode": run_mode,
        "unit_system": "SI",
        "material_id": spec.id,
        "material_card_file": str(card_path),
        "seed": int(body.get("seed") or 20260917),
        "grid": {
            "nx": n, "ny": n, "dx_m": dx * 1e-6, "dy_m": dx * 1e-6,
            "center_x_m": 0.0, "center_y_m": 0.0, "origin": "cell_center",
            "initial_surface": "flat", "initial_height_m": 0.0,
        },
        "laser": {
            "wavelength_m": lam, "pulse_duration_s": tau,
            "pulse_energy_J": energy, "repetition_rate_Hz": freq,
            "spot_radius_m": w0, "focus_xyz_m": [0.0, 0.0, 0.0],
            "direction_unit": [0.0, 0.0, 1.0],
            "rayleigh_range_m": rayleigh_m, "m2": m2_value,
            "power_measurement_location": "sample_surface",
            "parameter_sources": (["shared_experiment_background"] if actual_process
                                   else [f"reference_protocol:{rp.get('protocol_id')}"]),
        },
        "path": {"t0_s": 0.0, "time_tolerance_s": 1e-12, "segments": segs},
        "solver": {
            "mode": "reference", "geometry_feedback": geometry_feedback,
            "history_enabled": False, "tail_epsilon": 1e-08,
            "memory_budget_bytes": 2147483648, "budget_safety_factor": 1.5,
            "cancel_check_interval": 256, "acceleration": "off",
            "multiline_incubation": False, "structured_interface": False,
            "response_model": response_model_id,
        },
        "output": {
            "snapshot_policy": "passes", "snapshot_every_n_passes": 1,
            "max_snapshots": 8, "max_snapshot_bytes": 268435456,
            "cross_section": {"axis": "x", "offsets_m": [0.0, region * 1e-6 / 5.0]},
            "roi": ([{"name": "machining_region", "bounds_xy_m":
                       [-0.5 * region * 1e-6, 0.5 * region * 1e-6,
                        -0.5 * region * 1e-6, 0.5 * region * 1e-6]}]
                    if actual_process else
                    [{"name": "center", "radius_m": region * 1e-6 / 5.0,
                      "center_xy_m": [0.0, 0.0]}]),
        },
        "reference_conditions": {
            "protocol_id": rp.get("protocol_id"),
            "fluence_basis": "incident_peak_fluence",
            "effective_count": n_eff,
            "threshold_kind": resp.get("threshold_kind"),
            "threshold_J_m2": resp.get("threshold_J_m2"),
            "spot_radius_m": w0, "repetition_rate_Hz": freq,
            "peak_fluence_J_m2": peak,
            "focus_strategy": (bg.focus_strategy if actual_process and bg is not None else "reference_protocol"),
            "geometry_feedback": geometry_feedback,
            "power_rule": ("P_post_objective/f" if actual_process else "reference_peak_fluence"),
        },
    }

    if estimate_only:
        # 预估阶段只构造并校验同一份配置，不调用求解器、不落盘。
        try:
            estimate_cfg = RunConfig.from_dict(cfg, base_dir=str(project_root))
            estimate_report = validate_run(estimate_cfg, spec)
        except UFDemoError as err:
            return _failure([err.to_dict()])
        if not estimate_report.ok:
            return _failure(estimate_report.errors, warnings=estimate_report.warnings)
        events = int(estimate_report.estimated_events or 0)
        cells = int(n * n)
        # 这是用于交互确认的保守工程估计，不是精度结论；实际进度仍以后端
        # 逐事件回调为准。事件数和网格规模越大，估计时间相应增加。
        estimated_seconds = max(0.2, min(3600.0, events * cells * 3.0e-8))
        return {
            "schema": "ufdemo.web.progress_estimate/1",
            "status": "estimated",
            "estimatedEvents": events,
            "gridCells": cells,
            "grid": {"nx": n, "ny": n, "dxUm": dx},
            "estimatedMemoryBytes": int(estimate_report.estimated_memory_bytes or 0),
            "estimatedSeconds": estimated_seconds,
            "estimatedMinutes": estimated_seconds / 60.0,
            "estimateBasis": "基于当前网格单元数与路径事件数的保守工程估计；实际进度以后端事件回调为准。",
            "request": {
                "regionUm": region,
                "hatchUm": hatch,
                "passes": passes,
                "speedMmS": speed_m_s * 1e3,
                "repetitionRateKHz": freq / 1e3,
            },
        }

    out = solve_payload(
        cfg,   # 注意：solve_payload 收的就是配置本身（拆 params 是 webapp 路由的活）
        out_base=out_base, project_root=project_root,
        label=str(cfg["label"]), curves_dir=curves_dir,
        progress_callback=progress_callback,
    )
    # 把"这次用的协议参数"一并回给前端，演示时可以直接展示"输入是什么"
    _inc = dict(resp.get("incubation") or {})
    _s_spec = dict(_inc.get("S_f_kHz") or {})
    incubation_info = None
    if _inc.get("enabled") and _s_spec:
        incubation_info = {
            "enabled": True,
            "model": str(_inc.get("model") or "Fth(N) = Fth1 * N^(S-1)"),
            "Fth1Jm2": float(resp.get("threshold_J_m2") or 0.0),
            "S": float(_s_spec["intercept"]) + float(_s_spec["slope"]) * (freq / 1e3),
            "note": "阈值随各点累积照射次数逐事件计算（首脉冲用 N=1）",
        }
    out["demo"] = {
        "materialId": spec.id,
        "processMode": "actual" if actual_process else "reference",
        "protocolId": rp.get("protocol_id"),
        "wavelengthNm": lam * 1e9,
        "pulseDurationFs": tau * 1e15,
        "repetitionRateKHz": freq / 1e3,
        "spotRadiusUm": w0 * 1e6,
        "speedMmS": speed_m_s * 1e3,
        "peakFluenceJcm2": peak / 1e4,
        "pulseEnergyUJ": energy * 1e6,
        "effectiveCount": n_eff,
        "regionUm": region, "hatchUm": hatch, "passes": passes, "dxUm": dx,
        "nSegments": len(segs),
        "opticsSource": ("shared_experiment_background" if actual_process else "material_reference_protocol"),
        "powerRule": ("P_post_objective/f" if actual_process else "reference_peak_fluence"),
        "geometryFeedback": geometry_feedback,
        "focusStrategy": (bg.focus_strategy if actual_process and bg is not None else "reference_protocol"),
        "roiObservation": ("machining_region_full_rectangle" if actual_process else "center_circle"),
        # 阈值模型回显：界面用它说明「为什么工艺参数可以自由设」——
        # 阈值不是某个 N 处的常量，而是按 Fth(N)=Fth1·N^(S-1) 逐点算出来的。
        "incubation": incubation_info,
        "responseModel": (out.get("watermark") or {}).get("response_model"),
        "responseModelId": response_model_id,
        # 协议基准（**不受本次覆盖影响**）：前端用它做容差提示，
        # 否则用户改一次参数就把基准污染了，后面再也判断不出"偏离了多少"。
        "baseline": {
            "pulseDurationFs": _v("pulse_duration_s") * 1e15,
            "repetitionRateKHz": _v("repetition_rate_Hz") / 1e3,
            "spotRadiusUm": w0 * 1e6,
            "effectiveCount": card_n_eff,
            "speedMmS": float(
                sb.get("scan_speed_mm_s")
                or ((math.pi / 4.0) * (2.0 * w0 * _v("repetition_rate_Hz"))
                    / card_n_eff) * 1e3),
        },
    }
    return out


def shared_background_payload() -> dict[str, Any]:
    """共用实验背景摘要（C1）。界面「数据与工况」顶部直接显示。"""
    from .config import load_shared_background

    try:
        bg = load_shared_background()
        r = bg.check_self_consistency()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "postObjectivePowerW": bg.post_objective_power_W,
        "softwareSetpointW": bg.software_setpoint_W,
        "wavelengthNm": bg.wavelength_m * 1e9,
        "numericalAperture": bg.numerical_aperture,
        "m2": bg.m2,
        "waistUm": r["waist_derived_um"],
        "rayleighUm": r["rayleigh_derived_um"],
        "selfConsistent": r["ok"],
        "waistRelDiff": r["waist_rel_diff"],
        "rayleighRelDiff": r["rayleigh_rel_diff"],
        "focusStrategy": bg.focus_strategy,
        "geometryFeedback": bg.geometry_feedback,
        "dynamicAngle": bool(bg.raw["focus"]["dynamic_angle"]),
        "notShared": list(bg.raw["inheritance_scope"]["NOT_shared"]),
        "provenance": dict(bg.raw.get("provenance") or {}),
    }


def pulse_energy_payload(repetition_rate_Hz: float) -> dict[str, Any]:
    """按共用背景算脉冲能量（E = P_物镜后 / f）—— 界面只读显示，不让用户填两个可冲突的值。"""
    from .config import load_shared_background

    bg = load_shared_background()
    try:
        e = bg.pulse_energy_J(float(repetition_rate_Hz))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "pulseEnergyJ": e, "pulseEnergyUJ": e * 1e6,
            "postObjectivePowerW": bg.post_objective_power_W,
            "repetitionRateHz": float(repetition_rate_Hz)}
