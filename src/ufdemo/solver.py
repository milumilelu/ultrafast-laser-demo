"""逐事件编排、取消与快照（执行细则 5.1、5.2 节）。

固定顺序（细则 5.1）：
配置解析 → 单位归一 → 卡和模式准入 → 路径与网格检查 → 事件数量及内存预估
→ 新建运行目录 → 保存输入快照 → 初始化表面。

单事件流水线（细则 5.2）严格按第 1–9 步执行；同一事件的光束几何只计算一次，
高度、历史和暴露相**一次提交**，不做边更新边重算。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .beam import BeamOptions, beam_patch
from .config import (
    MAX_SUPPORTED_INCIDENCE_DEG,
    MIN_SUPPORTED_NZ,
    LaserConfig,
    PathConfig,
    RunConfig,
    SolverConfig,
    validate_run,
)
from .closures import EfficiencyClosure, apply_closure, build_features
from .errors import (
    CONFIG_INVALID,
    NUMERIC_NONFINITE,
    RESPONSE_SEMANTICS_INVALID,
    RESOURCE_BUDGET_EXCEEDED,
    UFDemoError,
)
from .metrics import RoiSpec, cross_section, domain_statistics, removal_volume, roi_statistics
from .materials import CAP_EVENT_INCREMENT, MaterialSpec, build_watermark, resolve_material
from .paths import PulseEvent, iter_events
from .response import FixedThresholdLogLaw, HistoryState, build_pulse_law
from .structure import load_structure
from .surface import SurfaceState
from .thresholds import build_threshold_protocol, classify_exceedance


@dataclass
class CancelToken:
    """进程内取消令牌。批次 E 的界面按钮复用同一语义。"""

    _cancelled: bool = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def is_set(self) -> bool:
        return self._cancelled


def _is_cancelled(token: Any) -> bool:
    if token is None:
        return False
    if callable(token):
        return bool(token())
    if hasattr(token, "is_set"):
        return bool(token.is_set())
    return bool(getattr(token, "cancelled", False))


def _per_phase_candidate(
    surface: SurfaceState,
    patch: Any,
    section: tuple[int, int, int, int],
    phase_laws: Mapping[int, Any],
    history: HistoryState,
    material: Any,
) -> Any:
    """按**当前暴露相**分派响应核。

    细则 8 节界面更新规则 1：当前脉冲只调用当前暴露相的响应。这里对每个相
    各调用一次核，且每个单元只被**一个**核处理——因此不存在把同一个物理脉冲
    拆成两个伪脉冲分别施加给两相的实现（任务书 G06 红线）。
    """
    import numpy as np

    iy0, iy1, ix0, ix1 = section
    pid = surface.phase_id[iy0:iy1, ix0:ix1]
    F = np.asarray(patch.fluence, dtype=np.float64)
    mask = np.asarray(patch.mask, dtype=bool)
    if F.shape != pid.shape or mask.shape != pid.shape:
        raise UFDemoError(
            NUMERIC_NONFINITE,
            "光斑窗口与相标签形状不匹配",
            field_path="structure.phase_at",
            actual={"fluence": list(F.shape), "phase_id": list(pid.shape)},
            requirement="两者一致",
        )

    present = {int(v) for v in np.unique(pid[mask]).tolist()} if mask.any() else set()
    unknown = sorted(present - set(int(k) for k in phase_laws))
    if unknown:
        raise UFDemoError(
            RESPONSE_SEMANTICS_INVALID,
            f"受照单元落在未定义响应的相标签上：{unknown}",
            field_path="structure.phases",
            actual=unknown,
            requirement=f"已定义相 id：{sorted(int(k) for k in phase_laws)}",
            suggestion="补齐该相的阈值与去除尺度；不得用邻近相或父卡参数顶替。",
        )

    out = np.zeros(F.shape, dtype=np.float64)
    for pid_value, law in phase_laws.items():
        sel = (pid == int(pid_value)) & mask
        if not np.any(sel):
            continue
        incr = law.increment(np.where(sel, F, 0.0), history, material)
        out = np.where(sel, incr.values, out)
    return out


def _closure_threshold_array(law: Any, history: HistoryState, shape: tuple[int, ...]) -> Any:
    """Return the event-start threshold used by a response law.

    ``FixedThresholdLogLaw`` exposes the incubation parameters explicitly.  A
    tabulated law may not expose a threshold; in that case the closure receives
    a neutral ratio reference (1.0) and the run metadata records that the
    threshold feature is unavailable.  This keeps the learned correction from
    silently inventing a material threshold.
    """

    import numpy as np

    incubation = getattr(law, "incubation", None)
    if isinstance(incubation, Mapping):
        fth1 = float(incubation["Fth1_internal"])
        exponent = float(incubation["S"]) - 1.0
        counts = (
            np.zeros(shape, dtype=float)
            if history.exposure_count is None
            else np.asarray(history.exposure_count, dtype=float)
        )
        return fth1 * np.power(np.maximum(counts, 1.0), exponent)
    threshold = getattr(law, "threshold_internal", None)
    if isinstance(threshold, (int, float)) and math.isfinite(float(threshold)) and float(threshold) > 0.0:
        return np.full(shape, float(threshold), dtype=float)
    return np.ones(shape, dtype=float)


def _closure_metadata(closure: Any) -> dict[str, Any]:
    """Serialize closure identity without requiring a concrete implementation."""

    if closure is None:
        return {"enabled": False, "kind": None}
    method = getattr(closure, "metadata", None)
    details = method() if callable(method) else {"kind": type(closure).__name__}
    return {"enabled": True, **dict(details)}


@dataclass
class RunResult:

    status: str  # running/completed/cancelled/failed
    run_id: str
    config_snapshot: Mapping[str, Any]
    material_snapshot: Mapping[str, Any]
    metadata: Mapping[str, Any]
    statistics: Mapping[str, Any]
    rois: list[Any] = field(default_factory=list)
    profiles: list[Any] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    events_processed: int = 0
    events_total: int = 0
    events_rows: list[dict[str, Any]] = field(default_factory=list)
    removal_available: bool = True
    unit: Any = None
    surface: SurfaceState | None = None
    elapsed_s: float = 0.0
    run_dir: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def summary(self) -> dict[str, Any]:
        u = self.unit
        return {
            "status": self.status,
            "run_id": self.run_id,
            "run_mode": self.config_snapshot.get("run_mode"),
            "material_id": self.config_snapshot.get("material_id"),
            "unit_system": getattr(u, "mode", None),
            "events_processed": self.events_processed,
            "events_total": self.events_total,
            "removal_available": self.removal_available,
            "statistics": self.statistics,
            "warnings": self.warnings,
            "errors": self.errors,
            "elapsed_s": self.elapsed_s,
        }


def _watermark(material: MaterialSpec, config: RunConfig) -> dict[str, Any]:
    """完整水印（材料身份 + 模式 + 单位 + 实际求解器开关），导出与回放同源。"""
    wm = build_watermark(material, config.unit, run_mode=config.run_mode)
    wm["geometry_feedback"] = config.solver.geometry_feedback
    wm["acceleration"] = config.solver.acceleration
    return wm


def solve(
    config: RunConfig,
    material: MaterialSpec | None = None,
    cancel_token: Any = None,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    *,
    material_dir: str | None = None,
    curves_dir: str | None = None,
    efficiency_closure: EfficiencyClosure | None = None,
    efficiency_closure_path: str | None = None,
) -> RunResult:
    """执行一次求解。

    Parameters
    ----------
    config:
        已解析的 :class:`RunConfig`。
    material:
        材料卡；为 ``None`` 时按 ``config.material_card_file`` 或 ``material_dir`` 解析。
    cancel_token:
        取消令牌；支持 ``CancelToken``、``threading.Event`` 或返回 bool 的可调用对象。
    progress_callback:
        每个快照节点或进度节点调用一次，参数为状态字典。
    efficiency_closure:
        Optional callable returning a bounded positive net-removal efficiency
        for each local event cell. It is applied after the mechanistic response
        and before ``SurfaceState.apply_increment``. When omitted, the baseline
        solver is unchanged.
    """
    import numpy as np

    if efficiency_closure is not None and efficiency_closure_path:
        raise UFDemoError(CONFIG_INVALID, "不能同时提供 efficiency_closure 与 efficiency_closure_path", field_path="efficiency_closure")
    if efficiency_closure is None and efficiency_closure_path:
        from .closures import load_efficiency_closure

        efficiency_closure = load_efficiency_closure(str(efficiency_closure_path))

    t_start = time.perf_counter()

    if material is None:
        if material_dir is None:
            raise UFDemoError(
                RESOURCE_BUDGET_EXCEEDED,
                "未提供材料卡或材料目录",
                field_path="material_card_file",
                suggestion="传 material=...，或 material_dir='data/materials'。",
            )
        material = resolve_material(config, material_dir)

    # --- 前两步：卡与模式准入 ---------------------------------------------
    report = validate_run(config, material)

    warnings: list[str] = list(report.warnings)
    threshold_only = config.run_mode == "threshold_only"

    closure_info = _closure_metadata(efficiency_closure)
    metadata = {
        "schema_version": config.schema_version,
        "run_id": None,  # 由 io.new_run_dir 填
        "run_mode": config.run_mode,
        "unit_mode": config.unit.mode,
        "unit": config.unit.to_dict(),
        "axis_order": list(config.grid.AXIS_ORDER),
        "enabled_features": {
            "threshold_only": threshold_only,
            "removal_available": not threshold_only,
            "geometry_feedback": config.solver.geometry_feedback,
            "history_enabled": config.solver.history_enabled,
            "oblique_incidence": False,
            "structured_interface": False,
            "acceleration": config.solver.acceleration,
            "snapshot_policy": config.output.snapshot_policy,
            "efficiency_closure": efficiency_closure is not None,
        },
        "evidence_status": material.evidence_status,
        "source_type": material.source_type,
        "card_version": material.card_version,
        "card_sha256": material.card_sha256,
        "watermark": _watermark(material, config),
        "seed": config.seed,
        "laser_metadata": {
            "direction_unit_input": list(config.laser.direction_unit_input) if config.laser.direction_unit_input else None,
            "direction_unit_normalized": list(config.laser.direction_unit),
            "direction_was_renormalized": config.laser.direction_renormalized,
            "peak_fluence_internal": config.laser.peak_fluence_internal,
            "power_measurement_location": config.laser.power_measurement_location,
        },
        "validation": report.to_dict(),
        "efficiency_closure": closure_info,
        "warnings": warnings,
        "approximations": [],
        "status": "running",
    }

    metadata["watermark"]["efficiency_closure"] = closure_info
    material_snapshot = {
        "schema_version": material.schema_version,
        "card": material.to_dict(),
        "card_sha256": material.card_sha256,
        "watermark": _watermark(material, config),
        "capabilities": {k: v.to_dict() for k, v in material.capabilities.items()},
    }
    material_snapshot["watermark"]["efficiency_closure"] = closure_info

    config_snapshot = config.to_dict()
    # 单一生效配置回显：调用方/导出不必从日志和多个模块拼接实际设置。
    # 运行期间会继续补充 effective_history_enabled、窗口策略与加速回退原因。
    metadata["effective_configuration"] = {
        "config": config_snapshot,
        "derived": {
            "history_enabled_effective": None,
            "window_radius_policy_effective": None,
            "acceleration_effective_mode": None,
            "response_closure": closure_info,
        },
    }

    result = RunResult(
        status="running",
        run_id="",
        config_snapshot=config_snapshot,
        material_snapshot=material_snapshot,
        metadata=metadata,
        statistics={},
        warnings=warnings,
        events_total=report.estimated_events or 0,
        unit=config.unit,
    )

    if not report.ok:
        result.status = "failed"
        result.errors = list(report.errors)
        result.diagnostics = {"stage": "validation"}
        result.removal_available = not threshold_only
        result.elapsed_s = time.perf_counter() - t_start
        return result

    # --- 受限阈值协议（批次 H / T14）--------------------------------------
    # 无论开启与否都先做配置层校验；未开启时据实报不可用（不返回全 0 假数组）。
    threshold_protocol = build_threshold_protocol(
        material,
        enabled=config.threshold.enabled,
        candidate_index=config.threshold.candidate_index,
        observable_name=config.threshold.observable_name,
        fluence_basis=config.threshold.fluence_basis,
    )
    threshold_active = bool(threshold_protocol.available)
    metadata["enabled_features"]["threshold_protocol"] = threshold_active
    metadata["threshold_protocol"] = threshold_protocol.to_dict()

    # --- 初始化表面与相结构（批次 G）--------------------------------------
    structure = load_structure(config, material)
    structured = bool(structure is not None and not getattr(structure, "is_uniform", True))
    result.metadata["enabled_features"]["structured_interface"] = structured
    structure_summary: dict[str, Any] | None = None
    if structured:
        from .structure import sample_volume_fraction

        structure_summary = structure.summary()
        # 目标体积分数与**实际**体积分数分开报告（有限样本不强制等于目标）
        structure_summary["volume_fraction"] = sample_volume_fraction(structure)
        result.metadata["structure"] = structure_summary

    surface = SurfaceState.initialize(
        config.grid,
        config.laser,
        history_enabled=False,
        threshold_protocol=threshold_active,
    )
    if structured:
        surface.initialize_phases(structure)
    result.surface = surface

    # --- 响应核 ------------------------------------------------------------
    law: FixedThresholdLogLaw | None = None
    phase_laws: dict[int, Any] = {}
    if not threshold_only:
        if structured:
            # 每个暴露相各自的响应核；逐事件只调用当前相那一个
            phase_laws = structure.laws(unit_mode=config.unit.mode)
        else:
            # --- U07：真实数据驱动求解 ---------------------------------------
            # `config.solver.response_curve` 非空时，逐事件核改由**实测曲线**
            # 驱动（`TabulatedEventLaw`），而不是材料卡的解析对数律。
            # 这是「真实数据 → 真实求解」的唯一入口。
            response_curve_obj = None
            curve_name = getattr(config.solver, "response_curve", None)
            response_model_id = getattr(config.solver, "response_model", None)
            if curve_name and response_model_id:
                raise UFDemoError(
                    CONFIG_INVALID,
                    "response_curve 与 response_model 不能同时驱动逐事件响应核",
                    field_path="solver.response_model",
                    actual={"response_curve": curve_name, "response_model": response_model_id},
                    requirement="二选一；曲线卡与脉宽条件化材料模型是不同响应来源",
                )
            if curve_name:
                if not curves_dir:
                    raise UFDemoError(
                        CONFIG_INVALID,
                        "config.solver.response_curve 指定了曲线卡，但未提供 curves_dir",
                        field_path="solver.response_curve",
                        actual=curve_name,
                        requirement="同时提供 curves_dir='data/curves'",
                        suggestion="调用方（CLI/界面）需把曲线目录传进来。",
                    )
                from . import tables as _T  # noqa: PLC0415

                curve_path = Path(curves_dir) / str(curve_name)
                if not curve_path.exists():
                    raise UFDemoError(
                        CONFIG_INVALID,
                        f"找不到响应曲线卡：{curve_name}",
                        field_path="solver.response_curve",
                        actual=str(curve_path),
                        requirement="文件存在于 curves_dir 下",
                    )
                response_curve_obj = _T.load_curve(curve_path)
            law = build_pulse_law(
                material, unit=config.unit, curve=response_curve_obj,
                # 用**运行时**激光条件做固定条件比对（不是材料卡的），
                # 否则「曲线是否适用于本次运行」这件事根本没被检查。
                laser={
                    "wavelength_m": config.laser.wavelength_m,
                    "pulse_duration_s": config.laser.pulse_duration_s,
                    "repetition_rate_Hz": config.laser.repetition_rate_Hz,
                },
                response_model_id=getattr(config.solver, "response_model", None),
            )
            if getattr(law, "response_model", None):
                result.metadata["response_model"] = dict(law.response_model)
                result.metadata["effective_configuration"]["derived"]["response_model"] = dict(law.response_model)
                result.metadata.setdefault("watermark", {})["response_model"] = dict(law.response_model)
                result.material_snapshot.setdefault("watermark", {})["response_model"] = dict(law.response_model)

    # A material card can request incubation even when the legacy config flag
    # is false; the reference loop then enables the required local history and
    # emits a warning. Record the effective state so exported metadata cannot
    # claim that history was disabled when it was actually used.
    _incubation_laws = ([law] if law is not None else []) + list(phase_laws.values())
    effective_history_enabled = bool(config.solver.history_enabled) or any(
        getattr(_law_obj, "incubation", None) is not None for _law_obj in _incubation_laws
    )
    result.metadata["enabled_features"]["history_enabled_effective"] = effective_history_enabled
    result.metadata["effective_configuration"]["derived"]["history_enabled_effective"] = effective_history_enabled
    result.metadata["history_definition"] = dict(getattr(material, "history_definition", {}) or {})

    rois = [RoiSpec.from_dict(r, i, config.unit) for i, r in enumerate(config.output.roi or ())]
    cs_cfg = dict(config.output.cross_section or {}) if config.output.cross_section else None

    # --- 窗口半径策略（性能开关，默认关闭）---------------------------------
    # 阈值型响应核在 ``F <= 阈值`` 处返回**恰好 0**，所以"不可能超阈值"的格子
    # 对去除量没有贡献，不必计算。深孔算例（深度 ≫ zR）下这类格子占窗口的 99%。
    # 阈值取**当前激活的核**声明的 ``threshold_internal``；分相时取最小者
    # （最保守 = 最大窗口，绝不会因此漏掉可烧蚀的格子）。
    # 取不到阈值时**明确回退**并写警告 —— 绝不假装收缩已启用。
    window_policy = str(getattr(config.solver, "window_radius_policy", "tail_epsilon"))
    window_threshold: float | None = None
    if window_policy == "above_threshold":
        _thr_candidates: list[float] = []
        _window_laws = ([law] if law is not None else []) + list(phase_laws.values())
        # 对幂律孵化，历史增长会继续降低阈值；在没有有限历史上界时，
        # 用初始阈值开窗会漏掉后来变成可烧蚀的环带。安全做法是回退到
        # tail_epsilon 全尾窗，而不是把“未计算”误报成零去除。
        if any(getattr(_law_obj, "incubation", None) is not None for _law_obj in _window_laws) \
                or getattr(config.solver, "response_model", None):
            window_policy = "tail_epsilon"
            warnings.append(
                "请求了 above_threshold 窗口，但响应核含动态孵化阈值或脉宽条件化参数；"
                "为避免历史/模型参数变化后漏算可烧蚀区域，已回退到 tail_epsilon 全尾窗。"
            )
        for _law_obj in _window_laws:
            _v = getattr(_law_obj, "threshold_internal", None)
            if isinstance(_v, (int, float)) and math.isfinite(float(_v)) and float(_v) > 0.0:
                _thr_candidates.append(float(_v))
        if window_policy == "above_threshold" and _thr_candidates:
            window_threshold = min(_thr_candidates)
        elif window_policy == "above_threshold":
            window_policy = "tail_epsilon"
            warnings.append(
                "请求了 window_radius_policy=above_threshold，但当前响应核**没有声明**可用阈值"
                "（threshold_internal）—— 无法计算超阈值半径，已回退到既有 tail_epsilon 窗口，"
                "**未启用**任何收缩。"
            )

    if progress_callback:
        progress_callback(
            {
                "stage": "initialized",
                "events_total": report.estimated_events,
                "estimated_memory_bytes": report.estimated_memory_bytes,
            }
        )

    # --- 事件循环 ----------------------------------------------------------
    snapshot_targets = _snapshot_event_set(config)
    result.diagnostics["events"] = {"n_events": 0, "n_ablating_events": 0}
    result.diagnostics["removal"] = {
        "candidate_volume_internal": 0.0,
        "applied_volume_internal": 0.0,
        "unapplied_candidate_removal_volume_internal": 0.0,
        "clipped_events": 0,
        "n_clipped_cells": 0,
        "phase_switch_cells": 0,
        "phase_switch_events": 0,
    }
    result.diagnostics["phase"] = {
        "structured_interface": structured,
        "initial_phase_cell_counts": dict(surface.phase_counts()),
        "final_phase_cell_counts": {},
        "phase_names": ({p.phase_id: p.name for p in structure.phases} if structured else {}),
    }
    # 焦平面策略写进元数据：让**每一次运行**的光学假设可审计，
    # 而不是只靠背景文件里的一句口头约定。见 docs/decisions/ADR-0018。
    _gf = str(getattr(config.solver, "geometry_feedback", "fixed_geometry"))
    result.metadata["focus"] = {
        "strategy": str(getattr(config.solver, "focus_strategy", "fixed_original_surface")),
        "z_m": float(getattr(config.grid, "initial_height_m", 0.0) or 0.0),
        "layer_refocus": False,
        "geometry_feedback": _gf,
        "passive_defocus": _gf == "axial_defocus",
        "note": (
            "焦平面恒为**加工前原始上表面**，不随槽底下降做 Z 调整（任务书 §6）；"
            "表面下降造成的**被动离焦**由 geometry_feedback 单独决定。"
        ),
    }
    result.diagnostics["fluence_ledger"] = {
        "emitted_energy_internal": 0.0,
        "estimated_intercepted_energy_internal": 0.0,
        "max_domain_truncated_fraction": 0.0,
        "window_radius_policy": window_policy,
        # ``above_threshold`` 策略的**口径变化**必须可追踪：窗口内超阈值半径之外
        # 的格子不再计算（它们对去除量贡献恒为 0，但会被计入剂量观测量）。
        "threshold_window": {
            "enabled": window_policy == "above_threshold",
            "threshold_internal": window_threshold,
            "margin": float(config.solver.window_threshold_margin),
            "events": 0,
            "window_cells_total": 0,
            "tail_window_cells_total": 0,
            "cells_skipped_fraction": None,
            "collapsed_events": 0,
        },
        "note": (
            "发射能量与估计截获能量之差只反映数值裁剪与计算域边界，"
            "不得解释为热损失或被吸收能量；这只检查光学输入账本。"
        ),
    }
    result.metadata["effective_configuration"]["derived"]["window_radius_policy_effective"] = window_policy
    result.diagnostics["threshold"] = {        "n_above_threshold_cells": 0,
        "n_exceeded_cell_events": 0,
        "exceeded_cells_final": 0,
        "exceeded_area_internal": 0.0,
        "protocol_available": threshold_protocol.available,
        "protocol_enabled": bool(config.threshold.enabled),
        "protocol_reason": threshold_protocol.reason,
        "observable": threshold_protocol.observable_name,
        "fluence_basis": threshold_protocol.fluence_basis,
        "threshold_internal": threshold_protocol.threshold_internal,
        "threshold_label": threshold_protocol.threshold_label,
        "threshold_kind": threshold_protocol.threshold_kind,
        "source_kind": threshold_protocol.source_kind,
        "source_index": threshold_protocol.source_index,
        "n_candidates": threshold_protocol.n_candidates,
        "evidence_status": threshold_protocol.evidence_status,
        "used_for_depth": False,
        "unit_note": "exceeded_area_internal 以内部长度单位平方计；换算见水印。",
        "note": (
            "受限协议：只对**本事件入射能流**判超阈，绝不使用累计剂量；"
            "该观测为分类标记（超阈值/改性），不产生去除量，也不代表任何热学量。"
        ),
    }

    last_event: PulseEvent | None = None
    previous_event_time_s: float | None = None
    n_events = 0
    events_limit = int(config.output.events_csv_max_rows or 0)

    # 批次 J（T18）：几何修正诊断（正入射时各项保持 0/None，不制造假数据）
    _axial_dir = (
        abs(config.laser.direction_unit[0]) < 1e-15
        and abs(config.laser.direction_unit[1]) < 1e-15
        and abs(config.laser.direction_unit[2] - 1.0) < 1e-15
    )
    # 「几何修正是否启用」不能只看「非轴向 or 动态角度」：
    # **轴向光束 + 初始斜面 + 关闭动态角度**同样需要投影与法向厚度换算
    # ——轴向 ≠ 垂直于倾斜工件。旧判据在这格上把几何修正报成「未启用」，
    # 与 beam_patch 的快捷路径同源（F11）。判据与 beam.is_flat_initial_surface 对齐。
    _tilted_initial = str(getattr(config.grid, "initial_surface", "flat")) == "tilted_plane" and (
        max(abs(float(v)) for v in getattr(config.grid, "initial_slope", (0.0, 0.0))) > 0.0
    )
    geom_diag: dict[str, Any] = {
        "enabled": (not _axial_dir) or bool(config.solver.dynamic_angle) or _tilted_initial,
        "oblique_incidence": not _axial_dir,
        "dynamic_angle": bool(config.solver.dynamic_angle),
        "tilted_initial_surface": _tilted_initial,
        "direction_unit": list(config.laser.direction_unit),
        "incidence_deg_axial": (
            math.degrees(math.acos(min(1.0, max(-1.0, float(config.laser.direction_unit[2])))))
            if not _axial_dir else 0.0
        ),
        "initial_surface": getattr(config.grid, "initial_surface", "flat"),
        "initial_slope": list(getattr(config.grid, "initial_slope", (0.0, 0.0))),
        "normal_thickness_conversions": 0,
        "n_events_with_shadowing": 0,
        "shadowed_cells_total": 0,
        "backfacing_cells_total": 0,
        "min_mu": None,
        "max_incidence_deg": None,
        "supported_range": {
            "min_nz": MIN_SUPPORTED_NZ,
            "max_incidence_deg": MAX_SUPPORTED_INCIDENCE_DEG,
            "note": "软件数值/展示范围，不是七类材料的物理边界。",
        },
        "note": (
            "几何修正只做投影（F_s=μ·F_⊥）与首次交点可见性；"
            "未提供材料偏振吸收依据时**不预测吸收差异**。"
        ),
    }
    result.diagnostics["geometry"] = geom_diag

    # --- 批次 I（T15/T16/T17）：冻结几何分组 vs 逐脉冲参考 -------------------
    # 分组只在工况允许时启用；否则**回退**到逐脉冲参考实现并保存原因（不静默降级）。
    grouped_stats: dict[str, Any] | None = None
    batch_policy: Any = None
    if config.solver.mode == "grouped" and efficiency_closure is None:
        from .accelerators import (
            BatchPolicy,
            LocalKernel,
            check_fallback_conditions,
            drift_reference_for,
        )

        decision = check_fallback_conditions(
            structured=structured,
                history_enabled=bool(config.solver.history_enabled) or effective_history_enabled,
            geometry_feedback=config.solver.geometry_feedback,
            dynamic_angle=bool(config.solver.dynamic_angle),
            oblique_incidence=not _axial_dir,
            response_kind=str(getattr(law, "KIND", "log_fixed")),
            response_gain=float(getattr(config.solver, "response_gain", 1.0)),
            subcell_order=int(getattr(config.solver, "subcell_order", 1)),
        )
        accel_requested = config.solver.acceleration
        numba_ok = False
        if accel_requested == "numba":
            from .accelerators import numba_available

            numba_ok = numba_available()
            if not numba_ok:
                warnings.append(
                    "请求了 solver.acceleration=numba，但当前环境缺少 numba；"
                    "已回退 NumPy 局部核（结果同式、逐位一致，只是没有 JIT 加速）。"
                    "安装：pip install -e \".[accel]\"。"
                )
        if not decision.use_batch:
            warnings.append(f"批量模式未启用，已回退逐脉冲参考实现：{decision.reason}")
            result.metadata["enabled_features"]["acceleration"] = "off"
            result.metadata["effective_configuration"]["derived"]["acceleration_effective_mode"] = "reference"
            result.metadata["acceleration"] = {
                "requested_mode": "grouped",
                "effective_mode": "reference",
                "requested_backend": config.solver.acceleration,
                "effective_backend": "off",
                "fallback_reason": decision.reason,
                "local_kernel": "off",
            }
        elif law is None:
            # 分相路径不会走到这里（上面已回退），但 threshold_only 时 law 为 None
            warnings.append("批量模式需要响应核；当前无响应核，已回退逐脉冲参考实现。")
            result.metadata["enabled_features"]["acceleration"] = "off"
            result.metadata["effective_configuration"]["derived"]["acceleration_effective_mode"] = "reference"
            result.metadata["acceleration"] = {
                "requested_mode": "grouped",
                "effective_mode": "reference",
                "requested_backend": config.solver.acceleration,
                "effective_backend": "off",
                "fallback_reason": "当前运行没有响应核（threshold_only 或未开放模式）。",
                "local_kernel": "off",
            }
        else:
            kernel = LocalKernel.build(prefer_numba=(accel_requested == "numba"))
            batch_policy = BatchPolicy(
                batch_size=config.solver.batch_size,
                min_batch_size=config.solver.min_batch_size,
                local_rel_tol=config.solver.local_rel_tol,
                local_abs_tol_internal=config.solver.local_abs_tol_internal,
                geometry_drift_limit=config.solver.geometry_drift_limit,
                max_cell_block=config.solver.max_cell_block,
            )
            batch_policy.validate()
            # 实际生效的局部核后端（off=NumPy 参考核；numba=JIT）——与界面/水印同源
            result.metadata["enabled_features"]["acceleration"] = (
                "numba" if kernel.kind == "numba" else "off"
            )
            result.metadata["effective_configuration"]["derived"]["acceleration_effective_mode"] = "grouped"
            result.metadata["acceleration"] = {
                "requested_mode": "grouped",
                "effective_mode": "grouped",
                "requested_backend": accel_requested,
                "effective_backend": kernel.kind,
                "fallback_reason": None,
                "local_kernel": kernel.kind,
                "local_kernel_jit_time_s": kernel.jit_time_s,
                "batch_size": batch_policy.batch_size,
                "min_batch_size": batch_policy.min_batch_size,
                "local_rel_tol": batch_policy.local_rel_tol,
                "local_abs_tol_internal": batch_policy.local_abs_tol_internal,
                "geometry_drift_limit": batch_policy.geometry_drift_limit,
                "max_cell_block": batch_policy.max_cell_block,
                "drift_reference_internal": drift_reference_for(config),
                "note": (
                    "冻结几何分组：块内逐事件各自计算非线性响应后求和，绝不先累加能流；"
                    "局部误差估计仅用于控制 batch_size，**全局正确性以完整逐脉冲对照为准**（G08）。"
                ),
            }
            grouped_stats = _grouped_event_loop(
                config=config,
                surface=surface,
                law=law,
                policy=batch_policy,
                kernel=kernel,
                drift_reference_internal=drift_reference_for(config),
                threshold_protocol=threshold_protocol,
                threshold_active=threshold_active,
                result=result,
                warnings=warnings,
                events_limit=events_limit,
                snapshot_targets=snapshot_targets,
                cancel_token=cancel_token,
                progress_callback=progress_callback,
            )
            n_events = int(grouped_stats["n_events"])
            last_event = grouped_stats["last_event"]
            if window_policy == "above_threshold":
                warnings.append(
                    "分组批量路径（mode=grouped）不支持窗口半径策略 above_threshold："
                    "该路径在块级冻结几何下批量计算，本次仍按既有 tail_epsilon 窗口。"
                    "如需超阈值收缩请用 mode=reference。"
                )
            if grouped_stats["cancelled"]:
                result.status = "cancelled"
                result.diagnostics["stage"] = "event_loop"
    elif config.solver.mode == "grouped" and efficiency_closure is not None:
        warnings.append(
            "已提供逐事件效率闭合，分组加速被禁用并回退 reference："
            "闭合依赖事件开始状态，冻结几何/块级提交会破坏递推语义。"
        )
        result.metadata["enabled_features"]["acceleration"] = "off"
        result.metadata["effective_configuration"]["derived"]["acceleration_effective_mode"] = "reference"
        result.metadata["acceleration"] = {
            "requested_mode": "grouped",
            "effective_mode": "reference",
            "requested_backend": config.solver.acceleration,
            "effective_backend": "off",
            "fallback_reason": "efficiency_closure_requires_event_start_state",
            "local_kernel": "off",
        }

    # 参考（逐脉冲）模式：同样记录 acceleration 元数据，避免界面/导出读到空字段。
    if config.solver.mode != "grouped":
        result.metadata["enabled_features"]["acceleration"] = "off"
        result.metadata["effective_configuration"]["derived"]["acceleration_effective_mode"] = "reference"
        result.metadata["acceleration"] = {
            "requested_mode": "reference",
            "effective_mode": "reference",
            "requested_backend": config.solver.acceleration,
            "effective_backend": "off",
            "fallback_reason": None,
            "local_kernel": "off",
            "note": "逐脉冲参考实现；未启用任何加速后端。",
        }

    # 逐脉冲参考循环。分组路径已在上面跑完时，迭代表达式求值为空序列，
    # 本循环整体跳过——用表达式而非缩进分支，避免两条路径的循环体重复。
    for event in (() if grouped_stats is not None else iter_events(config.path, config.laser)):
        if _is_cancelled(cancel_token):
            result.status = "cancelled"
            result.diagnostics["stage"] = "event_loop"
            warnings.append(f"运行被取消：已完成 {n_events} 个事件，只保留部分结果。")
            break

        # 第 1 步：真实物理事件与事件索引
        n_events += 1
        last_event = event
        if events_limit and len(result.events_rows) < events_limit:
            result.events_rows.append(event.to_dict())

        # 第 2 步：局部光斑窗口（无交集时仍记录发射能量）
        opt = BeamOptions(
            geometry_feedback=config.solver.geometry_feedback,
            tail_epsilon=config.solver.tail_epsilon,
            dynamic_angle=bool(config.solver.dynamic_angle),
            window_radius_policy=window_policy,
            fluence_threshold=window_threshold,
            window_threshold_margin=float(config.solver.window_threshold_margin),
            subcell_order=int(getattr(config.solver, "subcell_order", 1)),
        )
        patch = beam_patch(event, surface, opt)

        ledger = result.diagnostics["fluence_ledger"]
        ledger["emitted_energy_internal"] += patch.emitted_energy_J
        ledger["estimated_intercepted_energy_internal"] += patch.estimated_intercepted_energy_J
        ledger["max_domain_truncated_fraction"] = max(ledger["max_domain_truncated_fraction"], patch.domain_truncated_fraction)

        # 窗口半径策略的累计诊断（如实上报被裁掉的格子比例）
        _tw = ledger.get("threshold_window")
        if _tw is not None and _tw["enabled"]:
            _tw["events"] += 1
            _tw["window_cells_total"] += int(getattr(patch, "window_cells", 0))
            _tail_cells = getattr(patch, "tail_window_cells", None)
            if _tail_cells is not None:
                _tw["tail_window_cells_total"] += int(_tail_cells)
            if patch.empty:
                _tw["collapsed_events"] += 1

        # 批次 J：几何修正统计（正入射时 patch.visibility/mu 为 None，本段不执行）
        if patch.visibility is not None:
            _n_sh = int(patch.visibility.get("n_shadowed", 0))
            geom_diag["n_events_with_shadowing"] += int(_n_sh > 0)
            geom_diag["shadowed_cells_total"] += _n_sh
        if patch.mu is not None and getattr(patch.mu, "size", 0):
            _lit = np.asarray(patch.mu) > 0.0
            if np.any(_lit):
                _mu_min = float(np.min(np.asarray(patch.mu)[_lit]))
                geom_diag["min_mu"] = (
                    _mu_min if geom_diag["min_mu"] is None else min(float(geom_diag["min_mu"]), _mu_min)
                )
            geom_diag["backfacing_cells_total"] += int(np.count_nonzero(~_lit))
        # 正入射非空窗口：与原行为一致（不登记 notes）；空窗口或启用几何修正时登记。
        if patch.empty or geom_diag["enabled"]:
            for note in patch.notes:
                if note not in warnings:
                    warnings.append(note)

        section = (patch.iy0, patch.iy1, patch.ix0, patch.ix1)
        illumination_history_window = None
        if not patch.empty:
            # Snapshot the event-start history before recording this pulse's
            # illumination.  The response law consumes the pre-event count;
            # the cumulative dose ledger is updated immediately afterwards.
            illumination_history_window = np.asarray(
                surface.illumination_count[patch.iy0:patch.iy1, patch.ix0:patch.ix1],
                dtype=np.uint32,
            ).copy()
            # 第 4 步：累计入射剂量与照射诊断
            surface.accumulate_illumination(section, patch.fluence, patch.mask)

        # 受限阈值协议：只喂**本事件入射能流**（patch.fluence），绝不喂累计剂量；
        # 协议未开启时 threshold_active=False，本段整体不执行（不产生假数组）。
        if threshold_active and not patch.empty:
            exceed = classify_exceedance(threshold_protocol, patch.fluence, patch.mask)
            if exceed is not None and exceed.size:
                n_exceed = surface.accumulate_threshold(section, exceed)
                thr_diag = result.diagnostics["threshold"]
                thr_diag["n_above_threshold_cells"] += n_exceed
                thr_diag["n_exceeded_cell_events"] += n_exceed

        if threshold_only:
            # 阈值展示与逐事件去除分开分派，不通过伪造零 delta 共用深度核。
            # 协议已开启时由上面的受限协议路径统一计数，避免同一掩膜被重复累计。
            from .response import ThresholdEvaluator

            thr = ThresholdEvaluator.evaluate(
                patch.fluence if not patch.empty else np.zeros((0, 0)),
                {"threshold_internal": material.response.get("threshold_internal"), "observable_name": "fluence_above_threshold"},
            )
            if (
                not threshold_active
                and thr.available
                and thr.exceed_mask is not None
                and thr.exceed_mask.size
            ):
                result.diagnostics["threshold"]["n_above_threshold_cells"] += int(np.count_nonzero(thr.exceed_mask & patch.mask))
            result.events_processed = n_events
            previous_event_time_s = float(event.time_s)
            continue

        # 非 threshold_only 必须有响应核：单相走 law，分相走 phase_laws。
        # （分相时 law 为 None，逐事件只调用当前暴露相那一个核。）
        assert law is not None or phase_laws, "非 threshold_only 模式必须存在响应核"
        if patch.empty:
            result.events_processed = n_events
            previous_event_time_s = float(event.time_s)
            continue

        # 第 3、5 步：读取本事件开始时的状态 → 能流 → 候选去除量
        # 逐点曝光历史：**本事件响应前**的局部有效曝光计数；核内以 count+1
        # 转成材料模型的 1-based N。未达到去除阈值但确实被光斑照射的单元，
        # 仍可按材料卡的历史定义参与孵化，因此历史使用 illumination_count，
        # 与 exposure_count（实际去除次数）明确分离。
        # 卡声明了孵化模型就必须有它 —— 缺了核会拒绝执行（不静默退回固定阈值）。
        # 这里**自动开启**并记 warning：开历史是更完整的物理（不是近似），
        # 且全程可追溯，故不属于"静默降级"。
        # In a structured run ``law`` is None and each exposed phase owns its
        # response law.  Inspect all active laws so a phase-specific incubation
        # declaration cannot silently run with a missing local history array.
        _history_laws = ([law] if law is not None else []) + list(phase_laws.values())
        _needs_history = any(getattr(_law_obj, "incubation", None) is not None for _law_obj in _history_laws)
        if _needs_history and not config.solver.history_enabled:
            _history_note = (
                "材料卡声明了累积孵化模型（Fth(N)=Fth1·N^(S-1)）："
                "已自动开启逐点曝光历史（solver.history_enabled false→true），"
                "阈值将随各点累积照射次数变化。"
            )
            if _history_note not in warnings:
                warnings.append(_history_note)
        history = HistoryState(
            exposure_count=(
                # ⚠️ 必须**切成当前窗口**：exposure_count 是全网格 (ny,nx)，
                # 而 patch.fluence 只是窗口 (ny_win,nx_win)；
                # 不切就会与能流形状不匹配（核里会直接报错，不静默错算）。
                illumination_history_window
                if (config.solver.history_enabled or _needs_history) else None),
            definition=dict(getattr(material, "history_definition", {}) or {}),
        )
        if phase_laws:
            # 只调用**当前暴露相**的响应；一个物理脉冲绝不拆给两个相各算一次
            cand_arr = _per_phase_candidate(surface, patch, section, phase_laws, history, material)
            incr_direction = "vertical_height"
        else:
            assert law is not None
            # G-02: integrate the nonlinear response at explicit sub-cell
            # fluence samples, then average the increments.  Averaging F first
            # would bias the logarithmic threshold law near the ablation edge.
            sample_fluence = getattr(patch, "fluence_samples", None)
            if sample_fluence is not None:
                sample_hist = history
                if history is not None and history.exposure_count is not None:
                    q = int(np.asarray(sample_fluence).shape[-1])
                    sample_hist = HistoryState(
                        exposure_count=np.repeat(
                            np.asarray(history.exposure_count, dtype=np.uint32)[..., None],
                            q,
                            axis=-1,
                        ),
                        definition=history.definition,
                    )
                incr = law.increment(np.asarray(sample_fluence), sample_hist, material)
                cand_arr = np.mean(np.asarray(incr.values, dtype=np.float64), axis=-1)
                cand_arr = np.where(patch.mask, cand_arr, 0.0)
            else:
                incr = law.increment(patch.fluence, history, material)
                cand_arr = np.where(patch.mask, incr.values, 0.0)
            incr_direction = incr.depth_direction

        # 批次 J（T18）：法向厚度 → 高度（细则 6.6 / 9.2）
        # 只在核**明确声明** depth_direction=surface_normal 且表面法向确实倾斜时转换；
        # 正入射水平面 n_z≡1，该转换是恒等操作（因此批次 A–I 结果逐位不变）。
        # 源文深度方向不明时**不自动转换**（任务书 6.6）。
        normal_converted = False
        if incr_direction == "surface_normal" and patch.nz is not None:
            nz_win = np.asarray(patch.nz, dtype=np.float64)
            if float(np.max(np.abs(nz_win - 1.0))) > 0.0:
                from .geometry import normal_thickness_to_vertical_depth

                # cand_arr 是**去除量**（正值），故用法向厚度→垂直深度的正关系 d=a_n/n_z；
                # 对应的 Δh=-a_n/n_z（负高度变化）见 geometry.normal_thickness_to_height_drop。
                cand_arr = normal_thickness_to_vertical_depth(cand_arr, nz_win)
                normal_converted = True
                geom_diag["normal_thickness_conversions"] += 1

        # Optional causal learned closure.  It is applied after the response
        # semantics gate and geometry conversion, immediately before the one
        # surface commit.  Therefore later beam geometry and history see the
        # corrected surface rather than a post-hoc scaled final depth.
        if efficiency_closure is not None:
            import numpy as np

            local_shape = tuple(np.asarray(cand_arr).shape)
            if phase_laws:
                pid_window = surface.phase_id[patch.iy0:patch.iy1, patch.ix0:patch.ix1]
                threshold_arr = np.ones(local_shape, dtype=np.float64)
                for _pid_value, _law_obj in phase_laws.items():
                    _sel = pid_window == int(_pid_value)
                    if np.any(_sel):
                        threshold_arr = np.where(
                            _sel, _closure_threshold_array(_law_obj, history, local_shape), threshold_arr
                        )
            else:
                assert law is not None
                threshold_arr = _closure_threshold_array(law, history, local_shape)
            rayleigh_m = getattr(config.laser, "rayleigh_range_m", None)
            features = build_features(
                fluence=patch.fluence,
                threshold=threshold_arr,
                depth_m=np.asarray(
                    surface.initial_height[patch.iy0:patch.iy1, patch.ix0:patch.ix1]
                    - surface.height[patch.iy0:patch.iy1, patch.ix0:patch.ix1],
                    dtype=np.float64,
                ),
                exposure_count=np.asarray(surface.exposure_count[patch.iy0:patch.iy1, patch.ix0:patch.ix1], dtype=np.float64),
                illumination_count=np.asarray(illumination_history_window, dtype=np.float64),
                r2_m2=patch.r2,
                spot_radius_m=float(patch.spot_radius),
                rayleigh_range_m=(None if rayleigh_m is None else float(rayleigh_m)),
                pulse_duration_s=config.laser.pulse_duration_s,
                pass_index=int(event.pass_id),
                event_dt_s=(
                    None
                    if previous_event_time_s is None
                    else max(float(event.time_s) - float(previous_event_time_s), 0.0)
                ),
            )
            cand_arr, eta = apply_closure(cand_arr, features, efficiency_closure)
            closure_diag = result.diagnostics.setdefault("efficiency_closure", {})
            closure_diag["events"] = int(closure_diag.get("events", 0)) + 1
            closure_diag["eta_min"] = min(float(np.min(eta)), float(closure_diag.get("eta_min", np.inf)))
            closure_diag["eta_max"] = max(float(np.max(eta)), float(closure_diag.get("eta_max", -np.inf)))
            closure_diag["eta_mean_sum"] = float(closure_diag.get("eta_mean_sum", 0.0)) + float(np.mean(eta))
            closure_diag["feature_count"] = int(features.shape[1])
            closure_diag["rayleigh_feature_available"] = rayleigh_m is not None

        # --- C4：标定增益 a 作用于**几何更新之前** ---------------------------
        # Δd_cal = a·Δd_base。位置很关键：
        #   · 放在这里（候选增量 → 提交表面之前），后续脉冲会按**新表面**重算被动离焦，
        #     所以 D(a) ≠ a·D(1) —— 标定**真的改变了求解过程**；
        #   · 若改到结果页去乘，就退化成"给深度乘个系数"，与任务书 §4 的
        #     明确要求相违（那条要求正是为了防这种假标定）。
        # a=1.0 时不走分支，既有算例逐位不变。
        _gain = float(getattr(config.solver, "response_gain", 1.0))
        if _gain != 1.0:
            cand_arr = cand_arr * _gain
            geom_diag.setdefault("gain_applied", 0)
            geom_diag["gain_applied"] += 1
            geom_diag["gain_value"] = _gain

        # 第 6 步：语义/方向/非负/有限校验已在 IncrementResult.validate() 内完成
        cand_vol = float(np.sum(cand_arr) * surface.grid.dx_m * surface.grid.dy_m)

        # 第 7、8 步：相界面截断（批次 G）+ 一次提交
        diag = surface.apply_increment(
            cand_arr,
            event_index=event.index,
            section=section,
            structure=structure,
            candidate_volume_internal=cand_vol,
        )

        if diag.n_ablating_cells > 0:
            result.diagnostics["events"]["n_ablating_events"] += 1
        rem = result.diagnostics["removal"]
        rem["candidate_volume_internal"] += diag.candidate_volume_internal
        rem["applied_volume_internal"] += diag.applied_volume_internal
        rem["unapplied_candidate_removal_volume_internal"] += diag.clipped_volume_internal
        rem["clipped_events"] += int(diag.clipped_events)
        rem["n_clipped_cells"] += int(diag.n_clipped_cells)
        rem["phase_switch_cells"] += int(diag.phase_switch_cells)
        for note in diag.notes:
            if note.startswith("本事件后有"):
                # 逐事件相切换说明合并为收尾的一条汇总（细则 5.4：诊断不刷屏）
                if diag.phase_switch_cells:
                    rem["phase_switch_events"] += 1
                continue
            if note not in warnings:
                warnings.append(note)

        result.events_processed = n_events
        previous_event_time_s = float(event.time_s)

        # 第 9 步：快照节点与进度
        if _should_snapshot(config, event, snapshot_targets, result):
            _record_snapshot(result, surface, config, event)

        if progress_callback and (n_events % max(1, config.solver.cancel_check_interval) == 0):
            progress_callback({"stage": "solving", "events_done": n_events, "events_total": report.estimated_events})

    # --- 统计与收尾 --------------------------------------------------------
    result.diagnostics["events"]["n_events"] = n_events
    result.events_total = n_events

    # 受限阈值协议终态：累积超阈单元数与相应面积（未开启时保持 0，
    # 并保留 protocol_reason 说明为何不可用，而不是让调用方误以为「预测无超阈」）。
    if threshold_active and surface.threshold_exceeded_mask is not None:
        n_final = int(np.count_nonzero(surface.threshold_exceeded_mask))
        thr_diag = result.diagnostics["threshold"]
        thr_diag["exceeded_cells_final"] = n_final
        thr_diag["exceeded_area_internal"] = float(n_final * surface.grid.dx_m * surface.grid.dy_m)

    if result.status == "running":
        result.status = "completed"

    # 域截断警告聚合成一条（细则 5.4：记录数值截断说明，但不刷屏）
    trunc = result.diagnostics["fluence_ledger"]["max_domain_truncated_fraction"]
    _tw_final = result.diagnostics["fluence_ledger"].get("threshold_window") or {}
    if _tw_final.get("tail_window_cells_total"):
        _tw_final["cells_skipped_fraction"] = 1.0 - (
            _tw_final["window_cells_total"] / _tw_final["tail_window_cells_total"]
        )
    if trunc > 1e-6:
        if window_policy == "above_threshold":
            warnings.append(
                f"窗口半径策略 above_threshold：最坏情况有 {trunc:.3%} 的发射能量未计入窗口。"
                "这部分能流**低于响应阈值**，对去除量的贡献恰好为 0，"
                "不是「能流尾部超出计算域」，也不得解释为热损失；"
                "累计能流/照射计数等**剂量观测量**只在该半径内统计，"
                f"被裁掉的窗口格数占比见 fluence_ledger.threshold_window"
                f"（{_tw_final.get('cells_skipped_fraction')}）。"
            )
        else:
            warnings.append(
                f"有脉冲的能流尾部超出计算域，最坏情况未截获约 {trunc:.3%} 的发射能量；"
                "该能量不重新归一化给剩余网格，也不解释为热损失。"
            )
    geom_note = "固定几何解析基准：本事件使用初始表面高度计算离焦，忽略当前高度变化。"
    if config.solver.geometry_feedback == "fixed_geometry" and geom_note in warnings:
        pass  # 已由 beam 提示一次即可

    # 结构化结果：相标签终态、截断统计与频繁跨界警告（细则 3.3、8 节）
    if structured:
        result.diagnostics["phase"]["final_phase_cell_counts"] = dict(surface.phase_counts())
        rem = result.diagnostics["removal"]
        clip_vol = rem["unapplied_candidate_removal_volume_internal"]
        cand_vol = rem["candidate_volume_internal"]
        result.diagnostics["removal"]["unapplied_fraction_of_candidate"] = (
            (clip_vol / cand_vol) if cand_vol > 0 else 0.0
        )
        if n_events > 0 and rem["clipped_events"] / n_events > 0.5:
            warnings.append(
                f"跨相截断频繁：{rem['clipped_events']}/{n_events} 个事件被相界面截断。"
                "此时逐事件形貌对相界面的位置更敏感，属于有损近似，请谨慎解读。"
            )
        if rem["phase_switch_events"]:
            warnings.append(
                f"有 {rem['phase_switch_events']} 个事件出现相标签切换，累计 {rem['phase_switch_cells']} 个单元暴露到新相；"
                "新相响应从下一个真实脉冲开始，本脉冲不对新相重复施加完整能量。"
            )
        # 复用初始化时算好的 summary（含目标/实际体积分数），只补终态与截断统计
        assert structure_summary is not None
        structure_summary["final_phase_cell_counts"] = dict(surface.phase_counts())
        structure_summary["clipped_events"] = rem["clipped_events"]
        structure_summary["phase_switch_events"] = rem["phase_switch_events"]
        structure_summary["unapplied_candidate_removal_volume_internal"] = clip_vol

    # --- 批次 I：分组模式的诊断与如实标注（细则 9.1、任务书 11.1）-----------
    if grouped_stats is not None:
        result.diagnostics["acceleration"] = {
            "effective_mode": "grouped",
            "n_blocks": int(grouped_stats["n_blocks"]),
            "n_rejected_blocks": int(grouped_stats["n_rejected"]),
            "n_trials": int(grouped_stats["n_trials"]),
            "batch_size_configured": int(config.solver.batch_size),
            "n_beam_patches": int(grouped_stats["n_patches"]),
            "patch_cache_hits": int(grouped_stats["patch_cache_hits"]),
            "patch_reuse_ratio": (
                float(grouped_stats["patch_cache_hits"])
                / float(grouped_stats["n_patches"] + grouped_stats["patch_cache_hits"])
                if (grouped_stats["n_patches"] + grouped_stats["patch_cache_hits"]) > 0
                else 0.0
            ),
                "max_local_error_internal": float(grouped_stats["max_local_error_internal"]),
                "max_rel_l2_local": float(grouped_stats["max_rel_l2"]),
                "local_error_estimated": bool(grouped_stats["local_error_estimated"]),
                "snapshot_on_block_boundary": bool(grouped_stats["snapshots_on_block_boundary"]),
                "note": (
                    "以上为**局部**误差估计（B 与两个 B/2 试算比较），只用于控制 batch_size；"
                    "它不是全局误差证明，全局正确性以完整逐脉冲对照（G08）为准。"
                    "fixed_geometry 下能流只依赖初始面、一步与两个半步恒等，"
                    "已跳过半步试算（local_error_estimated=false），这是严格等价而非降低校验。"
                ),
        }
        if grouped_stats["snapshots_on_block_boundary"]:
            result.metadata["approximations"].append(
                "分组模式为保证快照时刻真实，主动在快照事件处切分块边界；"
                "快照表面对应所标事件提交后的状态，代价是块数可能增加。"
            )
        if grouped_stats["n_rejected"]:
            warnings.append(
                f"分组模式共拒绝/缩小 {grouped_stats['n_rejected']} 次块（局部误差或几何漂移超限），"
                "这些块按更小的 batch_size 或参考更新处理，未提交被拒绝的状态。"
            )
        result.metadata["acceleration"]["n_rejected_blocks"] = int(grouped_stats["n_rejected"])

    # --- 批次 J：几何修正收尾统计 ------------------------------------------
    geom_diag["max_incidence_deg"] = (
        None if geom_diag["min_mu"] is None
        else float(math.degrees(math.acos(min(1.0, max(0.0, float(geom_diag["min_mu"]))))))
    )
    if geom_diag["enabled"]:
        if geom_diag["normal_thickness_conversions"]:
            result.metadata["approximations"].append(
                "几何修正：核输出为**法向**厚度时按一阶关系 Δh=-a_n/n_z 换算为高度变化"
                "（细则 6.6；不是 -a_n·n_z）。该换算是几何近似，不改变材料响应本身。"
            )
        if geom_diag["n_events_with_shadowing"]:
            result.metadata["approximations"].append(
                f"几何修正：共有 {geom_diag['n_events_with_shadowing']} 个事件的照射窗口内存在"
                f"遮挡/背光单元（累计 {geom_diag['shadowed_cells_total']} 个单元次），"
                "这些单元的直接照射记为 0；可见性按首次交点射线检查，不做域外假设。"
            )

    # 最后一个事件后始终生成最终状态
    _finalize_statistics(result, surface, config, rois, cs_cfg, threshold_only)
    _record_final_snapshot(result, surface, config, last_event, n_events)

    if threshold_only:
        result.removal_available = False
        thr_diag = result.diagnostics["threshold"]
        thr_diag["mode_note"] = (
            "threshold_only：removal_available=false；体积与深度统计写 null，"
            "界面显示“不提供”，不以 0 暗示已预测无去除。"
        )
        result.metadata["approximations"].append("threshold_only：不产生去除量。")

    if "efficiency_closure" in result.diagnostics:
        _closure_diag = result.diagnostics["efficiency_closure"]
        _n_closure_events = int(_closure_diag.get("events", 0))
        _closure_diag["eta_mean"] = (
            float(_closure_diag.get("eta_mean_sum", 0.0)) / _n_closure_events
            if _n_closure_events
            else None
        )
        _closure_diag.pop("eta_mean_sum", None)
    result.metadata["status"] = result.status
    result.metadata["events_processed"] = n_events
    result.metadata["warnings"] = warnings
    # 水印里的 warnings 必须与最终结果一致（导出与界面逐字段同源）
    for _wm in (result.metadata.get("watermark"), (result.material_snapshot or {}).get("watermark")):
        if isinstance(_wm, dict):
            _wm["warnings"] = list(warnings)
    result.elapsed_s = time.perf_counter() - t_start
    result.diagnostics["stage"] = "finished"

    if progress_callback:
        progress_callback({"stage": "finished", "events_done": n_events, "status": result.status})

    return result


# ---------------------------------------------------------------------------
# 快照与统计辅助
# ---------------------------------------------------------------------------


def _event_batches(iterator: Any, batch_size: int) -> Any:
    """把事件生成器按**有限批**切分（不物化全部事件，满足细则 9.1 的事件块限额）。"""
    pending: list[PulseEvent] = []
    for event in iterator:
        pending.append(event)
        if len(pending) >= batch_size:
            yield pending
            pending = []
    if pending:
        yield pending


def _grouped_event_loop(
    *,
    config: RunConfig,
    surface: SurfaceState,
    law: FixedThresholdLogLaw,
    policy: Any,
    kernel: Any,
    drift_reference_internal: float | None,
    threshold_protocol: Any,
    threshold_active: bool,
    result: RunResult,
    warnings: list[str],
    events_limit: int,
    snapshot_targets: set[int],
    cancel_token: Any,
    progress_callback: Callable[[Mapping[str, Any]], None] | None,
) -> dict[str, Any]:
    """冻结几何分组的事件主循环（批次 I / T17）。

    与逐脉冲路径的对应关系：

    * 光束与响应：``accelerators.accumulate_block`` 在**块起点几何**下逐事件
      计算各自响应后求和（绝不先累加能流）；
    * 提交：``SurfaceState.apply_block_increment`` 一次性写回高度与四类计数，
      计数语义（``touch_counts``）与逐脉冲逐位对齐；
    * 快照：在**块边界**触发，索引标注为该块内最后一个命中事件——这是分组模式
      的既有近似，收尾以 ``grouped_snapshot_on_block_boundary`` 记入诊断；
    * 取消：块级检查，粒度比逐脉冲粗；取消时保留已完成块的结果。
    """
    from .accelerators import GeometryView, solve_block
    import numpy as np

    batch_size = int(config.solver.batch_size)
    iterator = iter_events(config.path, config.laser)
    n_events = 0
    last_event: PulseEvent | None = None
    n_blocks = 0
    n_rejected = 0
    n_trials = 0
    patch_cache_hits = 0
    n_patches = 0
    max_local_error = 0.0
    max_rel_l2 = 0.0
    local_error_estimated = False
    cancelled = False

    events_diag = result.diagnostics["events"]
    rem = result.diagnostics["removal"]
    ledger = result.diagnostics["fluence_ledger"]
    thr_diag = result.diagnostics["threshold"]
    snapshots_on_block_boundary = False
    # Fixed-geometry patches are immutable across exact snapshot splits; keep
    # one run-level cache so observation boundaries do not reduce reuse.
    patch_cache_store: dict[tuple[Any, ...], Any] = {}

    for batch in _event_batches(iterator, batch_size):
        # ``solve_block`` may accept only a prefix after local-error rejection.
        # Consume that prefix and feed the remaining events back through the same
        # loop; never advance counters for events that were not committed.
        pending = list(batch)
        while pending:
            if _is_cancelled(cancel_token):
                cancelled = True
                warnings.append(f"运行被取消：已完成 {n_events} 个事件，只保留部分结果。")
                pending = []
                break

            # Snapshots are exact state observations. Split before the first
            # requested event so a block never crosses a snapshot boundary.
            if snapshot_targets and len(result.snapshots) < config.output.max_snapshots:
                for idx, event in enumerate(pending):
                    if event.index in snapshot_targets:
                        pending_for_block = pending[: idx + 1]
                        break
                else:
                    pending_for_block = pending
            else:
                pending_for_block = pending

            view = GeometryView.of(surface)
            plan = solve_block(
                view, pending_for_block, law,
                policy=policy, kernel=kernel,
                drift_reference_internal=drift_reference_internal,
                threshold_protocol=threshold_protocol if threshold_active else None,
                geometry_feedback=config.solver.geometry_feedback,
                patch_cache_store=patch_cache_store,
            )
            accepted = list(plan.events)
            if not accepted:
                raise UFDemoError(
                    CONFIG_INVALID,
                    "分组块核没有消费任何事件",
                    field_path="solver.batch_size",
                    actual=len(pending_for_block),
                    requirement="每次 solve_block 至少接受 1 个事件",
                )
            acc = plan.accumulation
            n_events += len(accepted)
            last_event = accepted[-1]
            for event in accepted:
                if events_limit and len(result.events_rows) < events_limit:
                    result.events_rows.append(event.to_dict())
            n_blocks += 1
            n_rejected += plan.n_rejected
            n_trials += plan.total_trials
            patch_cache_hits += acc.patch_cache_hits
            n_patches += acc.n_patches
            max_local_error = max(max_local_error, float(plan.estimate.max_abs_internal))
            max_rel_l2 = max(max_rel_l2, float(plan.estimate.rel_l2))
            if "跳过半步试算" not in (plan.estimate.reason or ""):
                local_error_estimated = True

            # 提交窗口 = 本块被照射的包围盒；不能只按去除域截取。
            section = None
            if acc.illum_counts is not None and acc.illum_counts.size and int(np.max(acc.illum_counts)) > 0:
                ys = np.nonzero(np.any(acc.illum_counts > 0, axis=1))[0]
                xs = np.nonzero(np.any(acc.illum_counts > 0, axis=0))[0]
                section = (int(ys[0]), int(ys[-1]) + 1, int(xs[0]), int(xs[-1]) + 1)
            elif acc.delta_h.size and float(np.max(acc.delta_h)) > 0.0:
                ys = np.nonzero(np.any(acc.delta_h > 0.0, axis=1))[0]
                xs = np.nonzero(np.any(acc.delta_h > 0.0, axis=0))[0]
                section = (int(ys[0]), int(ys[-1]) + 1, int(xs[0]), int(xs[-1]) + 1)

            commit = surface.apply_block_increment(
                acc.delta_h,
                touch_counts=acc.touch_counts,
                fluence_sum=acc.fluence_sum,
                illum_counts=acc.illum_counts,
                exceed_counts=acc.exceed_counts if threshold_active else None,
                exceed_or=acc.exceed_or if threshold_active else None,
                section=section,
            )

            ledger["emitted_energy_internal"] += acc.emitted_energy_J
            ledger["estimated_intercepted_energy_internal"] += acc.estimated_intercepted_energy_J
            ledger["max_domain_truncated_fraction"] = max(
                ledger["max_domain_truncated_fraction"], acc.max_domain_truncated_fraction
            )
            for note in acc.notes:
                if note not in warnings:
                    warnings.append(note)
            if acc.n_ablating_events > 0:
                events_diag["n_ablating_events"] += acc.n_ablating_events
            cand_vol = acc.candidate_volume_internal(surface.grid.dx_m, surface.grid.dy_m)
            rem["candidate_volume_internal"] += cand_vol
            rem["applied_volume_internal"] += commit["applied_volume_internal"]
            rem["unapplied_candidate_removal_volume_internal"] += max(
                0.0, cand_vol - commit["applied_volume_internal"]
            )
            if threshold_active and acc.exceed_counts is not None:
                n_exceed = int(np.sum(acc.exceed_counts))
                thr_diag["n_above_threshold_cells"] += n_exceed
                thr_diag["n_exceeded_cell_events"] += n_exceed

            for event in accepted:
                if _should_snapshot(config, event, snapshot_targets, result):
                    _record_snapshot(result, surface, config, event)
                    snapshots_on_block_boundary = True

            result.events_processed = n_events
            pending = pending[len(accepted):]
            if progress_callback and (n_blocks % max(1, config.solver.cancel_check_interval // max(1, batch_size)) == 0):
                progress_callback({"stage": "solving_grouped", "events_done": n_events, "events_total": result.events_total})

    return {
        "n_events": n_events,
        "last_event": last_event,
        "n_blocks": n_blocks,
        "n_rejected": n_rejected,
        "n_trials": n_trials,
        "patch_cache_hits": patch_cache_hits,
        "n_patches": n_patches,
        "max_local_error_internal": max_local_error,
        "max_rel_l2": max_rel_l2,
        "local_error_estimated": local_error_estimated,
        "cancelled": cancelled,
        "snapshots_on_block_boundary": snapshots_on_block_boundary,
    }


def _pass_end_event_indices(path: PathConfig, laser: LaserConfig, every_n: int) -> set[int]:
    """``snapshot_policy="passes"`` 的触发点：**每 N 遍最后一个事件**的 index。

    细则语义是「逐遍策略在每一遍结束时记录形貌」，触发点是该遍**最后一个事件
    之后**，而不是下一遍第一个事件之前——后者会把下一遍的首脉冲一起算进去，
    使第 1 遍的快照落到第 3 个事件而不是第 2 个。

    「每 N 遍」按普通的逐遍计数理解：第 N、2N、3N……遍各记一次，**含末遍**。
    末遍结束处会与收尾快照（``final=True``）落在同一个事件上，两条记录内容相同
    但语义不同（一条是「逐遍策略的第 k 条」，一条是「始终存在的最终状态」），
    与批次 C 已交付的栅格验收行「3 遍 → 3 条逐遍 + 最终 = 4」一致。
    ``max_snapshots`` 在此仅约束逐遍快照，收尾快照不计入该额度。
    """
    if every_n < 1:
        return set()
    order: list[int] = []
    last_index: dict[int, int] = {}
    for ev in iter_events(path, laser):
        if ev.pass_id not in last_index:
            order.append(ev.pass_id)
        last_index[ev.pass_id] = ev.index
    ends: set[int] = set()
    for ordinal, pass_id in enumerate(order):
        if (ordinal + 1) % every_n == 0:
            ends.add(last_index[pass_id])
    return ends


def _snapshot_event_set(config: RunConfig) -> set[int]:
    """事件级快照触发点：``events`` 直接取给定 index，``passes`` 由逐遍边界算出。"""
    policy = config.output.snapshot_policy
    if policy == "events":
        return {int(v) for v in (config.output.snapshot_events or ())}
    if policy == "passes":
        return _pass_end_event_indices(
            config.path, config.laser, int(config.output.snapshot_every_n_passes or 1)
        )
    return set()


def _should_snapshot(config: RunConfig, event: PulseEvent, targets: set[int], result: RunResult) -> bool:
    if config.output.snapshot_policy not in ("events", "passes"):
        return False
    if len(result.snapshots) >= config.output.max_snapshots:
        return False
    return event.index in targets


def _snapshot_payload(surface: SurfaceState, config: RunConfig, event: PulseEvent | None, final: bool) -> dict[str, Any]:
    import numpy as np

    payload: dict[str, Any] = {
        "event_index": -1 if event is None else event.index,
        "time_s": 0.0 if event is None else event.time_s,
        "pass_id": -1 if event is None else event.pass_id,
        "final": final,
        "height": surface.height.astype(np.float64),
        "cumulative_fluence": surface.cumulative_fluence.astype(np.float64),
        "illumination_count": surface.illumination_count.astype(np.uint32),
        "exposure_count": surface.exposure_count.astype(np.uint32),
        "phase_id": surface.phase_id.astype(np.uint16),
    }
    if config.run_mode != "threshold_only":
        payload["depth"] = surface.depth.astype(np.float64)
    # 受限阈值协议量：仅在协议开启（数组已分配）时进入快照，否则不写假数组。
    thr_count = getattr(surface, "threshold_exceedance_count", None)
    if thr_count is not None:
        payload["threshold_exceedance_count"] = thr_count.astype(np.uint32)
    thr_mask = getattr(surface, "threshold_exceeded_mask", None)
    if thr_mask is not None:
        payload["threshold_exceeded_mask"] = thr_mask.astype(np.uint8)
    return payload


def _record_snapshot(result: RunResult, surface: SurfaceState, config: RunConfig, event: PulseEvent) -> None:
    payload = _snapshot_payload(surface, config, event, final=False)
    approx_bytes = sum(v.nbytes for v in payload.values() if hasattr(v, "nbytes"))
    used = sum(s.get("bytes", 0) for s in result.snapshots)
    if used + approx_bytes > config.output.max_snapshot_bytes:
        note = "快照字节上限已到，后续快照被跳过（统计仍基于求解场）。"
        if note not in result.warnings:
            result.warnings.append(note)
        return
    payload["bytes"] = approx_bytes
    result.snapshots.append(payload)


def _record_final_snapshot(result: RunResult, surface: SurfaceState, config: RunConfig, last_event: PulseEvent | None, n_events: int) -> None:
    payload = _snapshot_payload(surface, config, last_event, final=True)
    payload["bytes"] = sum(v.nbytes for v in payload.values() if hasattr(v, "nbytes"))
    payload["event_count"] = n_events
    result.snapshots.append(payload)


def _finalize_statistics(result: RunResult, surface: SurfaceState, config: RunConfig, rois: list[RoiSpec], cs_cfg: Mapping[str, Any] | None, threshold_only: bool) -> None:
    stats = domain_statistics(surface)
    stats["unit_system"] = config.unit.mode
    stats["length_unit"] = config.unit.length_label
    stats["depth_unit"] = config.unit.depth_label
    stats["fluence_unit"] = config.unit.fluence_label

    if threshold_only:
        # 体积和深度统计写 null（细则 2.2）
        stats["removal_volume_internal"] = None
        stats["max_depth_internal"] = None
        stats["center_depth_internal"] = None
        stats["mean_depth_internal"] = None
        stats["n_abating_cells"] = None
        stats["removal_available"] = False
        stats["note"] = "threshold_only：体积与深度不提供（null），界面显示“不提供”。"
        result.statistics = stats
        result.rois = []
        result.profiles = []
        return

    stats["removal_available"] = True
    if surface.structure is not None and not getattr(surface.structure, "is_uniform", True):
        # 相单元计数按「名称」展开为独立行，便于 statistics.csv 直接读取
        for name, cnt in surface.phase_counts().items():
            stats[f"phase_cell_count[{name}]"] = cnt
        stats["structured_interface"] = True
    result.statistics = stats
    result.rois = [r.to_dict() for r in roi_statistics(surface, rois)]
    if cs_cfg:
        axis = str(cs_cfg.get("axis", "x"))
        # 截面偏移同样属于「参与几何的长度」，必须换到内部尺度后再与网格比较
        offsets = [config.unit.length_to_internal(float(v)) for v in cs_cfg.get("offsets_m", [0.0])]
        profiles = cross_section(surface, axis=axis, offsets_m=offsets)
        if not config.unit.allows_physical_depth_export:
            for p in profiles:
                p["depth_units_label"] = (
                    f"{config.unit.depth_label}（与几何同一无量纲尺度，不是物理深度；"
                    "需要 d/delta_ref 时按 reference_scales 换算）"
                )
        result.profiles = profiles
