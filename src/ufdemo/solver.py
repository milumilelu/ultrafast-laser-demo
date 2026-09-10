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
from typing import Any, Callable, Mapping

from .beam import BeamOptions, beam_patch
from .config import LaserConfig, PathConfig, RunConfig, SolverConfig, validate_run
from .errors import NUMERIC_NONFINITE, RESPONSE_SEMANTICS_INVALID, RESOURCE_BUDGET_EXCEEDED, UFDemoError
from .metrics import RoiSpec, cross_section, domain_statistics, removal_volume, roi_statistics
from .materials import CAP_EVENT_INCREMENT, MaterialSpec, resolve_material
from .paths import PulseEvent, iter_events
from .response import FixedThresholdLogLaw, HistoryState, build_pulse_law
from .structure import load_structure
from .surface import SurfaceState


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


def solve(
    config: RunConfig,
    material: MaterialSpec | None = None,
    cancel_token: Any = None,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    *,
    material_dir: str | None = None,
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
    """
    import numpy as np

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

    metadata = {
        "schema_version": config.schema_version,
        "run_id": None,  # 由 io.new_run_dir 填
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
        },
        "evidence_status": material.evidence_status,
        "source_type": material.source_type,
        "card_version": material.card_version,
        "card_sha256": material.card_sha256,
        "seed": config.seed,
        "laser_metadata": {
            "direction_unit_input": list(config.laser.direction_unit_input) if config.laser.direction_unit_input else None,
            "direction_unit_normalized": list(config.laser.direction_unit),
            "direction_was_renormalized": config.laser.direction_renormalized,
            "peak_fluence_internal": config.laser.peak_fluence_internal,
            "power_measurement_location": config.laser.power_measurement_location,
        },
        "validation": report.to_dict(),
        "warnings": warnings,
        "approximations": [],
        "status": "running",
    }

    material_snapshot = {
        "schema_version": material.schema_version,
        "card": material.to_dict(),
        "card_sha256": material.card_sha256,
        "watermark": material.watermark(),
        "capabilities": {k: v.to_dict() for k, v in material.capabilities.items()},
    }

    config_snapshot = config.to_dict()

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

    surface = SurfaceState.initialize(config.grid, config.laser, history_enabled=False)
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
            law = build_pulse_law(material, unit=config.unit)

    rois = [RoiSpec.from_dict(r, i, config.unit) for i, r in enumerate(config.output.roi or ())]
    cs_cfg = dict(config.output.cross_section or {}) if config.output.cross_section else None

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
    result.diagnostics["fluence_ledger"] = {
        "emitted_energy_internal": 0.0,
        "estimated_intercepted_energy_internal": 0.0,
        "max_domain_truncated_fraction": 0.0,
        "note": (
            "发射能量与估计截获能量之差只反映数值裁剪与计算域边界，"
            "不得解释为热损失或被吸收能量；这只检查光学输入账本。"
        ),
    }
    result.diagnostics["threshold"] = {"n_above_threshold_cells": 0}

    last_event: PulseEvent | None = None
    n_events = 0
    events_limit = int(config.output.events_csv_max_rows or 0)
    for event in iter_events(config.path, config.laser):
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
        )
        patch = beam_patch(event, surface, opt)

        ledger = result.diagnostics["fluence_ledger"]
        ledger["emitted_energy_internal"] += patch.emitted_energy_J
        ledger["estimated_intercepted_energy_internal"] += patch.estimated_intercepted_energy_J
        ledger["max_domain_truncated_fraction"] = max(ledger["max_domain_truncated_fraction"], patch.domain_truncated_fraction)
        if patch.empty:
            for note in patch.notes:
                if note not in warnings:
                    warnings.append(note)

        section = (patch.iy0, patch.iy1, patch.ix0, patch.ix1)
        if not patch.empty:
            # 第 4 步：累计入射剂量与照射诊断
            surface.accumulate_illumination(section, patch.fluence, patch.mask)

        if threshold_only:
            # 阈值展示与逐事件去除分开分派，不通过伪造零 delta 共用深度核
            from .response import ThresholdEvaluator

            thr = ThresholdEvaluator.evaluate(
                patch.fluence if not patch.empty else np.zeros((0, 0)),
                {"threshold_internal": material.response.get("threshold_internal"), "observable_name": "fluence_above_threshold"},
            )
            if thr.available and thr.exceed_mask is not None and thr.exceed_mask.size:
                result.diagnostics["threshold"]["n_above_threshold_cells"] += int(np.count_nonzero(thr.exceed_mask & patch.mask))
            result.events_processed = n_events
            continue

        # 非 threshold_only 必须有响应核：单相走 law，分相走 phase_laws。
        # （分相时 law 为 None，逐事件只调用当前暴露相那一个核。）
        assert law is not None or phase_laws, "非 threshold_only 模式必须存在响应核"
        if patch.empty:
            result.events_processed = n_events
            continue

        # 第 3、5 步：读取本事件开始时的状态 → 能流 → 候选去除量
        history = HistoryState(exposure_count=None)
        if phase_laws:
            # 只调用**当前暴露相**的响应；一个物理脉冲绝不拆给两个相各算一次
            cand_arr = _per_phase_candidate(surface, patch, section, phase_laws, history, material)
        else:
            assert law is not None
            incr = law.increment(patch.fluence, history, material)
            cand_arr = np.where(patch.mask, incr.values, 0.0)

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

        # 第 9 步：快照节点与进度
        if _should_snapshot(config, event, snapshot_targets, result):
            _record_snapshot(result, surface, config, event)

        if progress_callback and (n_events % max(1, config.solver.cancel_check_interval) == 0):
            progress_callback({"stage": "solving", "events_done": n_events, "events_total": report.estimated_events})

    # --- 统计与收尾 --------------------------------------------------------
    result.diagnostics["events"]["n_events"] = n_events
    result.events_total = n_events

    if result.status == "running":
        result.status = "completed"

    # 域截断警告聚合成一条（细则 5.4：记录数值截断说明，但不刷屏）
    trunc = result.diagnostics["fluence_ledger"]["max_domain_truncated_fraction"]
    if trunc > 1e-6:
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

    # 最后一个事件后始终生成最终状态
    _finalize_statistics(result, surface, config, rois, cs_cfg, threshold_only)
    _record_final_snapshot(result, surface, config, last_event, n_events)

    if threshold_only:
        result.removal_available = False
        result.diagnostics["threshold"]["note"] = (
            "threshold_only：removal_available=false；体积与深度统计写 null，"
            "界面显示“不提供”，不以 0 暗示已预测无去除。"
        )
        result.metadata["approximations"].append("threshold_only：不产生去除量。")

    result.metadata["status"] = result.status
    result.metadata["events_processed"] = n_events
    result.metadata["warnings"] = warnings
    result.elapsed_s = time.perf_counter() - t_start
    result.diagnostics["stage"] = "finished"

    if progress_callback:
        progress_callback({"stage": "finished", "events_done": n_events, "status": result.status})

    return result


# ---------------------------------------------------------------------------
# 快照与统计辅助
# ---------------------------------------------------------------------------


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
