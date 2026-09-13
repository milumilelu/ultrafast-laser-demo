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
from typing import Any, Iterable, Mapping

import numpy as np

from . import ui_service as U
from .config import RunConfig, validate_run
from .errors import UFDemoError
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
        "eventsProcessed": int(frozen.events_processed),
        "eventsTotal": int(frozen.events_total),
        "removalAvailable": bool(frozen.removal_available),
        "stats": jsonable(frozen.statistics),
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
            state, material, out_base=out_base, project_root=project_root, label=label
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
    for card_file in sorted(Path(material_dir).glob("*.json")):
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
                "thresholdCandidates": jsonable(list(m.threshold_candidates or ())),
                "multiResponse": jsonable(m.multi_response),
                "phases": jsonable(list(m.phases or ())),
                "validityDomain": jsonable(m.validity_domain),
                # 界面上「该卡在什么条件下才允许定量」——直接取自有效性域，
                # 不编造激光参数摘要（卡里没登记就不显示）。
                "validityScope": (m.validity_domain or {}).get("scope"),
                "validityNote": (m.validity_domain or {}).get("note"),
                "protocolId": (m.validity_domain or {}).get("protocol_id"),
                "peakFluenceJm2": (m.validity_domain or {}).get("peak_fluence_J_m2"),
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
