"""界面逻辑层（批次 E / T09）—— **不导入 Streamlit，也不通过全局 UI 状态取输入**。

执行细则第 3 节要求「科学核心不得导入 Streamlit 或 Plotly，不得通过全局 UI 状态
取得输入」；第 11.3 节要求「参数编辑状态与已完成结果分开保存」「提交时冻结配置
快照并计算输入哈希」「只旋转视图、切换图层/截面/快照时读取已有数据」。

本模块把上述要求落成可单元测试的纯逻辑：

* :class:`SessionState` —— 把**可编辑参数**与**已冻结的结果**放在两个字段里；
* :func:`submit` —— 只在这里调用求解器；每成功提交一次 ``solve_count += 1``；
* :func:`read_existing_run` —— 读取历史运行目录，只增加 ``read_count``，不求解；
* :func:`snapshot_arrays` / :func:`layer_view` —— 纯读取（含视图旋转/翻转），
  **不增加任何计数器**，这就是 G09「相机和快照切换不增加求解次数」的实现依据；
* :func:`is_stale` —— 提交后参数再被改动时，把旧结果标记为「上一次运行」。

界面层（``webapp.py`` 与唯一界面 ``webui/demo.html``）只负责渲染与调用本模块，不直接碰求解器。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .config import RunConfig, UnitContext, validate_run
from .errors import UFDemoError
from .io import config_hash, load_run, make_run_id, new_run_dir, save_run, stable_json
from .materials import CAP_EVENT_INCREMENT, CAP_THRESHOLD, MaterialSpec
from .solver import RunResult, solve

# 界面允许渲染的图层（执行细则 11.3 / 任务书 13 节）。
# 措辞采用**严格字面**口径：执行细则 11.3「阈值图不写『热影响区』；照射剂量不写
# 『温度』」按字面执行——标签本身不得出现这些词，**否定式（“不是 X”）同样不允许**，
# 否则最朴素的子串守卫就会失效，无法作为安全网复审。因此约定：标签只描述**该图
# 实际是什么量**，不得引入被禁止的对照概念。澄清信息放在 LAYER_NOTES / 不可用原因里，
# 且同样避开被禁词。
LAYER_LABELS: dict[str, str] = {
    "height": "当前表面高度",
    "depth": "去除深度",
    "cumulative_fluence": "累计入射剂量",
    "illumination_count": "有效照射计数",
    # 批次 H：受限阈值协议量。只描述**该图实际是什么量**（本事件入射能流的超阈分类），
    # 不含任何热学措辞。
    "threshold_mask": "超阈值/改性标记（仅按本事件入射能流判定）",
    "warning_mask": "域外/截断等警告标记",
    # 批次 G：分相结构的"相编号"图。只显示**当前暴露相**的编号，
    # 是几何/身份标签，不是任何损伤或热学量。
    "phase_id": "材料相标签（当前暴露相的编号）",
}

# 禁止出现在界面与导出里的措辞（执行细则 11.3，严格子串口径）。
FORBIDDEN_TERMS: tuple[str, ...] = (
    "热影响区",
    "HAZ",
    "温度场",
    "温度分布",
    "热输入",
    "温度",
)

# 本批次**明确不提供**的图层及原因（不装作可用，也不拿无关量凑数）。
# 关键：不得用「累计剂量 vs 单脉冲阈值」比较来伪造超阈值标记——两者量纲虽同，
# 物理含义不同（累计入射剂量 ≠ 单脉冲峰值能流）。
UNAVAILABLE_LAYERS: dict[str, str] = {
    "threshold_mask": (
        "该运行没有逐事件超阈值掩膜（受限阈值协议未开启，或该材料卡未提供可用阈值）。"
        "协议只对**本事件入射能流**判超阈，不得用累计剂量与单脉冲阈值比较来伪造该标记，"
        "也不得把它读作热学损伤标记。"
    ),
}


# ---------------------------------------------------------------------------
# 批次 G：相结构诊断（只读展示，不参与任何计算）
# ---------------------------------------------------------------------------


def build_structure_diagnostics(
    diagnostics_inner: Mapping[str, Any] | None,
    structure_summary: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """把求解器写出的相结构诊断整理成界面可展示的字典。

    入参对应 ``result.diagnostics`` 与 ``result.metadata["structure"]``；两者都能从
    运行目录（``diagnostics.json`` / ``metadata.json``）读回，因此**实时运行与历史
    读取走同一条路径**，不会出现"读回的历史运行看不到分相统计"。

    均质单相运行返回空字典（界面不显示分相面板，也不提供相标签图层）。
    """
    sm = dict(structure_summary or {})
    if not sm or sm.get("structure_type") in (None, "homogeneous") or "phases" not in sm:
        return {}
    diag = dict(diagnostics_inner or {})
    phase = dict(diag.get("phase") or {})
    removal = dict(diag.get("removal") or {})
    vf = dict(sm.get("volume_fraction") or {})
    return {
        "structured_interface": True,
        "structure_type": sm.get("structure_type"),
        "algorithm_version": sm.get("algorithm_version"),
        "seed": sm.get("seed"),
        "phases": list(sm.get("phases") or []),
        "phase_names": {str(k): v for k, v in (phase.get("phase_names") or {}).items()},
        "initial_phase_cell_counts": dict(phase.get("initial_phase_cell_counts") or {}),
        "final_phase_cell_counts": dict(phase.get("final_phase_cell_counts") or {}),
        "volume_fraction": vf,
        "target_volume_fraction": sm.get("target_volume_fraction"),
        "same_phase_merge": sm.get("same_phase_merge"),
        "clipped_events": removal.get("clipped_events"),
        "n_clipped_cells": removal.get("n_clipped_cells"),
        "phase_switch_cells": removal.get("phase_switch_cells"),
        "phase_switch_events": removal.get("phase_switch_events"),
        "unapplied_candidate_removal_volume_internal": removal.get(
            "unapplied_candidate_removal_volume_internal"
        ),
        "unapplied_fraction_of_candidate": removal.get("unapplied_fraction_of_candidate"),
        "truncation_note": sm.get("truncation_note"),
    }


def build_threshold_diagnostics(diagnostics_inner: Mapping[str, Any] | None) -> dict[str, Any]:
    """批次 H：把求解器写出的受限阈值协议诊断整理成界面可展示的字典。

    入参对应 ``result.diagnostics``（也能从运行目录 ``diagnostics.json`` 读回），
    因此**实时运行与历史读取走同一条路径**，不会出现"读回的历史运行看不到阈值信息"。

    协议未开启/不可用时返回空字典——界面不显示阈值面板，也不提供 ``threshold_mask``
    图层，而不是拿全 0 数组冒充可用。``used_for_depth`` 恒为 False：超阈值是分类标记，
    不是去除量。
    """
    diag = dict(diagnostics_inner or {})
    thr = dict(diag.get("threshold") or {})
    if not thr or not thr.get("protocol_available"):
        return {}
    return {
        "available": True,
        "enabled": bool(thr.get("protocol_enabled")),
        "observable": thr.get("observable"),
        "fluence_basis": thr.get("fluence_basis"),
        "threshold_internal": thr.get("threshold_internal"),
        "threshold_label": thr.get("threshold_label"),
        "threshold_kind": thr.get("threshold_kind"),
        "source_kind": thr.get("source_kind"),
        "source_index": thr.get("source_index"),
        "n_candidates": thr.get("n_candidates"),
        "evidence_status": thr.get("evidence_status"),
        "n_above_threshold_cells": thr.get("n_above_threshold_cells"),
        "n_exceeded_cell_events": thr.get("n_exceeded_cell_events"),
        "exceeded_cells_final": thr.get("exceeded_cells_final"),
        "exceeded_area_internal": thr.get("exceeded_area_internal"),
        "used_for_depth": False,
        "restricted": True,
        "note": thr.get("note"),
    }

def build_acceleration_diagnostics(
    diagnostics_inner: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """批次 I：把冻结几何分组的诊断整理成界面可展示的字典。

    入参对应 ``result.diagnostics`` 与 ``result.metadata``（也能从运行目录
    ``diagnostics.json`` / ``metadata.json`` 读回），因此**实时运行与历史读取同源**。

    关键口径（细则 12 节 / 任务书 11.1）：

    * 界面必须显示**实际批大小、拒绝/回退次数、与参考模式的差值和运行时间**；
    * 局部误差估计**不是**全局误差证明——文案里必须写明，避免读者误读；
    * 参考模式或回退时，如实说明"未启用批量"及原因，不假装加速过。
    """
    diag = dict(diagnostics_inner or {})
    acc = dict(diag.get("acceleration") or {})
    meta = dict(metadata or {})
    meta_acc = dict(meta.get("acceleration") or {})

    # 优先用 diagnostics 里的运行期数据；必要时用 metadata 补模式信息
    effective_mode = acc.get("effective_mode") or meta_acc.get("effective_mode")
    if not acc and not meta_acc:
        return {}

    return {
        "available": True,
        "requested_mode": meta_acc.get("requested_mode"),
        "effective_mode": effective_mode,
        "requested_backend": meta_acc.get("requested_backend"),
        "effective_backend": meta_acc.get("effective_backend") or acc.get("local_kernel"),
        "grouped": effective_mode == "grouped",
        "fallback_reason": meta_acc.get("fallback_reason"),
        "batch_size_configured": acc.get("batch_size_configured"),
        "n_blocks": acc.get("n_blocks"),
        "n_rejected_blocks": acc.get("n_rejected_blocks"),
        "n_trials": acc.get("n_trials"),
        "n_beam_patches": acc.get("n_beam_patches"),
        "patch_cache_hits": acc.get("patch_cache_hits"),
        "patch_reuse_ratio": acc.get("patch_reuse_ratio"),
        "max_local_error_internal": acc.get("max_local_error_internal"),
        "max_rel_l2_local": acc.get("max_rel_l2_local"),
        "local_error_estimated": acc.get("local_error_estimated"),
        "snapshot_on_block_boundary": bool(acc.get("snapshot_on_block_boundary")),
        "lossy_approximation": bool(acc.get("snapshot_on_block_boundary")),
        "note": acc.get("note") or meta_acc.get("note"),
        "boundary_note": (
            "局部误差估计只用于控制 batch_size，**不是全局误差证明**；"
            "全局正确性以完整逐脉冲对照为准（见 G08 与性能报告）。"
        ),
    }


def build_geometry_diagnostics(
    diagnostics_inner: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """批次 J：把斜入射/动态角度的几何诊断整理成界面可展示的字典。

    入参对应 ``result.diagnostics`` 与 ``result.metadata``（也能从运行目录读回），
    因此**实时运行与历史读取同源**（与批次 G/H/I 的诊断面板一致）。

    关键口径（细则 9.2 / 任务书 6.6）：

    * ``n_z>=0.5``、入射角≤60° 是**软件数值/展示范围**，不是材料物理边界；
    * **只做几何修正**，未提供吸收依据时不预测吸收差异；
    * 遮挡/背向的直接照射记为 0，但**不代表材料内部无响应**。
    """
    diag = dict(diagnostics_inner or {})
    geo = dict(diag.get("geometry") or {})
    # 正入射（未启用几何修正）时返回空字典：界面不显示该面板，
    # 与批次 G/H/I 的「未启用即不显示、不返回假数据」同口径。
    if not geo or not geo.get("enabled"):
        return {}
    return {
        "available": True,
        "enabled": bool(geo.get("enabled")),
        "oblique_incidence": bool(geo.get("oblique_incidence")),
        "dynamic_angle": bool(geo.get("dynamic_angle")),
        "direction_unit": geo.get("direction_unit"),
        "incidence_deg_axial": geo.get("incidence_deg_axial"),
        "min_mu": geo.get("min_mu"),
        "max_incidence_deg": geo.get("max_incidence_deg"),
        "initial_surface": geo.get("initial_surface"),
        "initial_slope": geo.get("initial_slope"),
        "normal_thickness_conversions": geo.get("normal_thickness_conversions"),
        "n_events_with_shadowing": geo.get("n_events_with_shadowing"),
        "shadowed_cells_total": geo.get("shadowed_cells_total"),
        "backfacing_cells_total": geo.get("backfacing_cells_total"),
        "supported_range": geo.get("supported_range"),
        "lossy_approximation": bool(
            (geo.get("normal_thickness_conversions") or 0) or (geo.get("shadowed_cells_total") or 0)
        ),
        "boundary_note": (
            "n_z≥0.5、入射角≤60° 是**软件数值/展示范围**，不是七类材料的物理边界；"
            "超范围会停止该模式并给出位置，不裁剪角度继续运行。"
        ),
        "note": geo.get("note"),
    }


def _fmt_cell(v: Any) -> str:
    """统一把诊断值格式化为字符串。

    Streamlit 的 ``st.dataframe`` 经 pyarrow 转换时要求**同列同类型**；
    混合 bool/str/float/None 会抛 ``ArrowInvalid``。诊断面板天然是异构的
    "键-值"表，故在纯逻辑层就统一成字符串，界面层无需再猜类型。
    """
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt_cell(x) for x in v) + "]"
    if isinstance(v, Mapping):
        return "{" + ", ".join(f"{k}={_fmt_cell(val)}" for k, val in v.items()) + "}"
    return str(v)


def acceleration_diagnostics_rows(d: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把加速诊断整理成界面可直接显示的「键-值」行（纯逻辑，便于断言）。

    所有值统一为字符串：``st.dataframe`` 的列必须同类型（见 `_fmt_cell`）。
    """
    if not d:
        return []
    grouped = bool(d.get("grouped"))
    rows = [
        {"项": "请求模式", "值": _fmt_cell(d.get("requested_mode"))},
        {"项": "实际模式", "值": _fmt_cell(d.get("effective_mode"))},
        {"项": "局部核后端", "值": _fmt_cell(d.get("effective_backend"))},
    ]
    if grouped:
        rows += [
            {"项": "批大小", "值": _fmt_cell(d.get("batch_size_configured"))},
            {"项": "块数", "值": _fmt_cell(d.get("n_blocks"))},
            {"项": "拒绝/回退块数", "值": _fmt_cell(d.get("n_rejected_blocks"))},
            {"项": "光束补丁数", "值": _fmt_cell(d.get("n_beam_patches"))},
            {"项": "补丁复用命中", "值": _fmt_cell(d.get("patch_cache_hits"))},
            {"项": "补丁复用率", "值": _fmt_cell(d.get("patch_reuse_ratio"))},
            {"项": "局部误差（内部单位）", "值": _fmt_cell(d.get("max_local_error_internal"))},
            {"项": "执行半步试算", "值": _fmt_cell(d.get("local_error_estimated"))},
            {"项": "快照在块边界", "值": _fmt_cell(d.get("snapshot_on_block_boundary"))},
        ]
    if d.get("fallback_reason"):
        rows.append({"项": "回退原因", "值": _fmt_cell(d["fallback_reason"])})
    return rows


def geometry_diagnostics_rows(d: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把几何诊断整理成界面可直接显示的「键-值」行（纯逻辑，便于断言）。

    所有值统一为字符串（见 `_fmt_cell`），避免 pyarrow 的混合类型转换失败。
    """
    if not d or not d.get("enabled"):
        return []
    rng = d.get("supported_range") or {}
    rows = [
        {"项": "斜入射", "值": _fmt_cell(d.get("oblique_incidence"))},
        {"项": "动态角度（逐点法向）", "值": _fmt_cell(d.get("dynamic_angle"))},
        {"项": "初始面", "值": _fmt_cell(d.get("initial_surface"))},
        {"项": "初始斜率 (h_x, h_y)", "值": _fmt_cell(d.get("initial_slope"))},
        {"项": "光轴夹角 (°)", "值": _fmt_cell(d.get("incidence_deg_axial"))},
        {"项": "窗口内最大入射角 (°)", "值": _fmt_cell(d.get("max_incidence_deg"))},
        {"项": "法向厚度转换次数", "值": _fmt_cell(d.get("normal_thickness_conversions"))},
        {"项": "含遮挡的事件数", "值": _fmt_cell(d.get("n_events_with_shadowing"))},
        {"项": "遮挡单元次", "值": _fmt_cell(d.get("shadowed_cells_total"))},
        {"项": "背向单元次", "值": _fmt_cell(d.get("backfacing_cells_total"))},
        {"项": "支持范围 n_z ≥", "值": _fmt_cell(rng.get("min_nz"))},
        {"项": "支持范围入射角 ≤", "值": _fmt_cell(rng.get("max_incidence_deg"))},
    ]
    return rows


STATUS_ZH = {
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "running": "进行中",
}


# ---------------------------------------------------------------------------
# 参数与快照的稳定哈希
# ---------------------------------------------------------------------------


def params_hash(params: Mapping[str, Any]) -> str:
    """参数冻结哈希。同一参数+种子必须得到同一哈希（G09 可复现性）。"""
    return config_hash(params)


def unit_context_of(params: Mapping[str, Any]) -> UnitContext:
    return UnitContext.from_config(params)


def watermark_of(material: MaterialSpec, unit: UnitContext, *, run_mode: str) -> dict[str, Any]:
    """每个结果必须持续显示的标签（细则 11.3、任务书 13 节）。

    与 ``solver``/``io`` 共用同一构造器，保证「界面显示的标签」与「导出文件里的标签」
    逐字段一致（不再各写一份）。
    """
    from .materials import build_watermark

    return build_watermark(material, unit, run_mode=run_mode)


def format_watermark(wm: Mapping[str, Any]) -> str:
    """一行式标签，供图表标题与导出复用。"""
    phys = "物理预测" if wm.get("physical_prediction_allowed") else "非物理预测"
    return (
        f"{wm.get('material_id')}｜{wm.get('family')}／{wm.get('grade')}｜"
        f"模式 {wm.get('run_mode')}｜单位 {wm.get('unit_system')}｜"
        f"证据 {wm.get('evidence_status')}｜{phys}"
    )


# ---------------------------------------------------------------------------
# 结果容器
# ---------------------------------------------------------------------------


def _roi_statistics_from_stats(stats: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Rebuild saved ROI rows from the long-form statistics CSV."""
    grouped: dict[str, dict[str, Any]] = {}
    for key, value in dict(stats or {}).items():
        if not str(key).startswith("roi[") or "]." not in str(key):
            continue
        head, field = str(key).split("].", 1)
        name = head[4:]
        grouped.setdefault(name, {"name": name})[field] = value
    return list(grouped.values())


@dataclass
class FrozenRun:
    """一次**已提交**运行的结果快照。界面图表只读这里，不读当前表单值。"""

    run_id: str
    run_dir: str
    run_mode: str
    unit_system: str
    input_hash: str
    submitted_at: str
    status: str
    material_watermark: dict[str, Any]
    statistics: dict[str, Any] = field(default_factory=dict)
    # 加工观测 ROI（与全域 statistics 分开，避免把边带平均当槽平均）。
    roi_statistics: list[dict[str, Any]] = field(default_factory=list)
    profiles: list[dict[str, Any]] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    events_processed: int = 0
    events_total: int = 0
    removal_available: bool = True
    # 求解耗时（秒）。实时运行取自 result，历史读取取自 diagnostics.json 顶层。
    elapsed_s: float | None = None
    # 批次 G：相结构诊断（实时运行与历史读取同源；均质运行为空）
    structure_diagnostics: dict[str, Any] = field(default_factory=dict)
    # 批次 H：受限阈值协议诊断（未开启时为空；开启时含可观测量/基准/阈值与原因）
    threshold_diagnostics: dict[str, Any] = field(default_factory=dict)
    # 批次 I：分组批量诊断（参考模式/回退时也给出模式与原因；含批大小、拒绝次数、局部误差）
    acceleration_diagnostics: dict[str, Any] = field(default_factory=dict)
    # 批次 J：几何修正诊断（斜入射/动态角度；正入射时为空字典）
    geometry_diagnostics: dict[str, Any] = field(default_factory=dict)
    # 内存中的结果与表面（用于渲染；不写回表单）
    result: RunResult | None = None
    # 从磁盘读取的最终表面数组（历史运行；同样只读，不求解）
    disk_surface: dict[str, Any] = field(default_factory=dict)
    # 磁盘快照：目录 + 文件名列表（按 index 顺序），按需读取
    snapshot_dir: str | None = None
    snapshot_files: list[str] = field(default_factory=list)

    # -- 只读渲染接口（不求解、不计数）-----------------------------------
    def surface(self):
        return None if self.result is None else self.result.surface

    def _snapshot_arrays(self, snapshot_index: int) -> dict[str, Any]:
        """取第 ``snapshot_index`` 个快照的数组：先内存，再磁盘。纯读取。"""
        if 0 <= snapshot_index < len(self.snapshots):
            snap = self.snapshots[snapshot_index]
            arrs = {k: v for k, v in snap.items() if hasattr(v, "shape")}
            if arrs:
                return arrs
        if self.snapshot_dir and 0 <= snapshot_index < len(self.snapshot_files):
            import numpy as np

            fp = Path(self.snapshot_dir) / self.snapshot_files[snapshot_index]
            if fp.exists():
                with np.load(fp, allow_pickle=False) as npz:
                    return {k: npz[k] for k in npz.files}
        return {}

    def arrays_from(self, snapshot_index: int | None) -> dict[str, Any]:
        """取当前表面或某个快照的数组。纯读取（历史运行从磁盘按需读取）。"""
        if snapshot_index is not None:
            return self._snapshot_arrays(int(snapshot_index))
        s = self.surface()
        if s is None:
            return dict(self.disk_surface)
        out = {
            "height": s.height,
            "x": s.x,
            "y": s.y,
            "initial_height": s.initial_height,
            "cumulative_fluence": s.cumulative_fluence,
            "illumination_count": s.illumination_count,
            "exposure_count": s.exposure_count,
            "phase_id": s.phase_id,
            "warning_mask": s.warning_mask,
        }
        if self.removal_available:
            out["depth"] = s.depth
        # 受限阈值协议量：仅在协议开启（数组已分配）时暴露，不返回全 0 假数组
        thr_count = getattr(s, "threshold_exceedance_count", None)
        if thr_count is not None:
            out["threshold_exceedance_count"] = thr_count
        thr_mask = getattr(s, "threshold_exceeded_mask", None)
        if thr_mask is not None:
            out["threshold_exceeded_mask"] = thr_mask
        return out

    def _threshold_layer_available(self, snapshot_index: int | None = None) -> bool:
        """该运行是否真的记录了逐事件超阈掩膜（协议开启且数组存在）。"""
        return "threshold_exceeded_mask" in self.arrays_from(snapshot_index)

    def available_layers(self, snapshot_index: int | None = None) -> dict[str, str | None]:
        """返回 ``{layer: 不可用原因或 None}``，供界面如实展示可用性。"""
        arrs = self.arrays_from(snapshot_index)
        out: dict[str, str | None] = {}
        for name in LAYER_LABELS:
            if name == "depth" and not self.removal_available:
                out[name] = "该运行不提供去除深度"
            elif name == "threshold_mask" and "threshold_exceeded_mask" not in arrs:
                # 协议开启但本结果仍无掩膜：据实说明；协议未开启则用统一口径。
                if self.threshold_diagnostics:
                    out[name] = "该运行已开启阈值协议，但本次结果未保存逐事件超阈掩膜"
                else:
                    out[name] = UNAVAILABLE_LAYERS["threshold_mask"]
            elif name == "phase_id" and not self.structure_diagnostics:
                out[name] = "该运行未启用分相结构（均质单相，没有相编号可显示）"
            elif name == "threshold_mask":
                out[name] = None
            elif name in UNAVAILABLE_LAYERS:
                out[name] = UNAVAILABLE_LAYERS[name]
            elif name in arrs:
                out[name] = None
            elif name == "depth" and "height" in arrs:
                out[name] = None  # 可由 h0 - h 得到
            else:
                out[name] = "该结果为 threshold_only，或此来源未保存该数组"
        return out

    def depth_of(self, snapshot_index: int | None = None):
        """``depth = h0 - h``；只读，不写入任何状态。"""
        if not self.removal_available:
            return None
        if snapshot_index is None:
            s = self.surface()
            if s is not None:
                return getattr(s, "depth", None)
            arrs = dict(self.disk_surface)
        else:
            arrs = self._snapshot_arrays(int(snapshot_index))
        if "depth" in arrs:
            return arrs["depth"]
        h = arrs.get("height")
        if h is None:
            return None
        h0 = arrs.get("initial_height")
        return (0.0 if h0 is None else h0) - h

    def layer(self, name: str, snapshot_index: int | None = None):
        """取某个图层数组。未知图层名报错；已知但不可用返回 ``None``。"""
        if name not in LAYER_LABELS:
            raise UFDemoError(
                "CONFIG_INVALID",
                f"未知图层 {name!r}",
                field_path="layer",
                actual=name,
                requirement=f"取值属于 {sorted(LAYER_LABELS)}",
            )
        if name in UNAVAILABLE_LAYERS:
            # 受限阈值协议（批次 H）开启后该图层可用；其余口径不变。
            if name == "threshold_mask" and self._threshold_layer_available(snapshot_index):
                return self.arrays_from(snapshot_index)["threshold_exceeded_mask"]
            return None
        if name == "phase_id" and not self.structure_diagnostics:
            # 均质运行没有相结构：不拿全零数组冒充"相标签图"
            return None
        if name == "depth":
            return self.depth_of(snapshot_index)
        arrs = self.arrays_from(snapshot_index)
        if name in arrs:
            return arrs[name]
        # 允许用 h0 - h 现场得到深度；其余图层必须真实存在
        return None

    def renderable_layers(self, snapshot_index: int | None = None) -> list[str]:
        avail = self.available_layers(snapshot_index)
        return [k for k, v in avail.items() if v is None]

    def summary_rows(self) -> list[dict[str, Any]]:
        """给界面表格用的统计行（不含单位换算；单位见水印）。"""
        st = dict(self.statistics or {})
        keys = (
            "removal_volume_internal",
            "center_depth_internal",
            "max_depth_internal",
            "mean_depth_internal",
            "domain_area_internal",
            "unit_system",
            "run_mode",
        )
        return [{"metric": k, "value": st.get(k)} for k in keys]

    def structure_summary_rows(self) -> list[dict[str, Any]]:
        """批次 G：每个相一行的分相统计（供界面表格；无分相结构时返回空）。"""
        d = self.structure_diagnostics
        if not d:
            return []
        init = d.get("initial_phase_cell_counts") or {}
        fin = d.get("final_phase_cell_counts") or {}
        rows: list[dict[str, Any]] = []
        for ph in d.get("phases") or []:
            name = ph.get("name")
            rows.append(
                {
                    "phase_id": ph.get("phase_id"),
                    "name": name,
                    "role": ph.get("role"),
                    "threshold_internal": ph.get("threshold_internal"),
                    "delta_internal": ph.get("delta_internal"),
                    "initial_cells": init.get(name),
                    "final_cells": fin.get(name),
                }
            )
        return rows

    def threshold_rows(self) -> list[dict[str, Any]]:
        """批次 H：受限阈值协议的可展示字段（协议未开启/不可用时返回空）。

        一律**如实**：``used_for_depth`` 恒为 False（分类标记不是去除量），
        ``fluence_basis`` 必须显示出来，避免读者把阈值图读成别的量。
        """
        d = self.threshold_diagnostics
        if not d:
            return []
        return [
            {"metric": "观测量", "value": d.get("observable")},
            {"metric": "能流基准", "value": d.get("fluence_basis")},
            {"metric": "阈值（内部单位）", "value": d.get("threshold_internal")},
            {"metric": "阈值标签", "value": d.get("threshold_label")},
            {"metric": "阈值口径", "value": d.get("threshold_kind")},
            {"metric": "来源类别", "value": d.get("source_kind")},
            {"metric": "候选索引", "value": d.get("source_index")},
            {"metric": "候选数", "value": d.get("n_candidates")},
            {"metric": "证据状态", "value": d.get("evidence_status")},
            {"metric": "累计超阈单元·事件数", "value": d.get("n_exceeded_cell_events")},
            {"metric": "最终超阈单元数", "value": d.get("exceeded_cells_final")},
            {"metric": "最终超阈面积（内部单位²）", "value": d.get("exceeded_area_internal")},
            {"metric": "是否参与深度更新", "value": d.get("used_for_depth")},
        ]

    def truncation_rows(self) -> list[dict[str, Any]]:
        """批次 G：跨相截断诊断（名称固定为「未应用候选去除体积」）。"""
        d = self.structure_diagnostics
        if not d:
            return []
        return [
            {"metric": "被截断的事件数", "value": d.get("clipped_events")},
            {"metric": "被截断的单元次数", "value": d.get("n_clipped_cells")},
            {"metric": "相标签切换的单元次数", "value": d.get("phase_switch_cells")},
            {"metric": "出现相标签切换的事件数", "value": d.get("phase_switch_events")},
            {
                "metric": "未应用候选去除体积（内部单位）",
                "value": d.get("unapplied_candidate_removal_volume_internal"),
            },
            {
                "metric": "未应用候选占比",
                "value": d.get("unapplied_fraction_of_candidate"),
            },
        ]


# ---------------------------------------------------------------------------
# 会话状态：参数编辑态 与 结果态 分离
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    """界面会话状态。

    关键不变量：

    * ``params`` 是**可编辑**的当前表单值；
    * ``frozen`` 是**已提交**的结果（图表只读它）；
    * 提交后参数再被改动 → :meth:`is_stale` 为真，界面必须把旧结果标为
      「上一次运行」，不得把它当成当前参数的结果。
    """

    params: dict[str, Any]
    frozen: FrozenRun | None = None
    submitted_params_hash: str | None = None
    submitted_at: str | None = None
    solve_count: int = 0
    read_count: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    last_error: dict[str, Any] | None = None
    preview_ok: bool | None = None
    preview_errors: list[dict[str, Any]] = field(default_factory=list)

    # -- 状态查询 -----------------------------------------------------------
    @property
    def has_result(self) -> bool:
        return self.frozen is not None

    def is_stale(self) -> bool:
        """当前参数是否已经偏离产生结果的参数（→ 旧结果标记为上一次运行）。"""
        if self.frozen is None or self.submitted_params_hash is None:
            return False
        return params_hash(self.params) != self.submitted_params_hash

    def result_label(self) -> str:
        if self.frozen is None:
            return "（尚无结果）"
        if self.is_stale():
            return f"上一次运行（{self.submitted_at}）"
        return f"本次运行（{self.submitted_at}）"

    def counters(self) -> dict[str, int]:
        """透明度计数器：求解只发生在提交；读取与回放另有计数。"""
        return {
            "solve_count": self.solve_count,
            "read_existing_count": self.read_count,
            "frozen_results": 1 if self.frozen else 0,
        }


def new_session(params: Mapping[str, Any]) -> SessionState:
    return SessionState(params=copy.deepcopy(dict(params)))


def set_pending_params(state: SessionState, params: Mapping[str, Any]) -> dict[str, Any]:
    """把**表单当前值**写入编辑态（不求解、不冻结）。

    执行细则 11.3 与 G09 要求「显示新参数但尚未提交时，旧结果必须标为
    上一次运行」。因此界面每次重跑都把表单值写进 ``state.params``，
    :meth:`SessionState.is_stale` 据此比较已提交哈希。
    """
    state.params = copy.deepcopy(dict(params))
    return state.params


# ---------------------------------------------------------------------------
# 提交：唯一允许调用求解器的地方
# ---------------------------------------------------------------------------


def preview_validate(state: SessionState, material: MaterialSpec) -> list[dict[str, Any]]:
    """只做准入校验，**不求解、不计入求解次数**。"""
    try:
        cfg = RunConfig.from_dict(copy.deepcopy(state.params))
        report = validate_run(cfg, material)
        state.preview_ok = report.ok
        state.preview_errors = list(report.errors)
        return list(report.errors)
    except UFDemoError as err:
        state.preview_ok = False
        state.preview_errors = [err.to_dict()]
        return state.preview_errors


def submit(
    state: SessionState,
    material: MaterialSpec,
    *,
    out_base: str | Path,
    project_root: str | Path,
    label: str = "ui_run",
    code_info: Mapping[str, Any] | None = None,
    curves_dir: str | Path | None = None,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> FrozenRun:
    """冻结当前参数、求解一次并把结果写入新目录。

    这是**唯一**会调用 :func:`ufdemo.solver.solve` 的入口，因此
    ``solve_count`` 只在成功提交时 +1。
    """
    from datetime import datetime, timezone

    frozen_params = copy.deepcopy(dict(state.params))
    in_hash = params_hash(frozen_params)
    now = datetime.now(timezone.utc).isoformat()

    # 配置构造本身也可能失败（例如 nx 越界）。这类失败同样必须：
    # (1) 记入 last_error 供界面显示；(2) 不计入求解次数；(3) 不污染已有冻结结果。
    try:
        cfg = RunConfig.from_dict(frozen_params)
        report = validate_run(cfg, material)
    except UFDemoError as err:
        state.preview_ok = False
        state.preview_errors = [err.to_dict()]
        state.last_error = {"stage": "config", "errors": [err.to_dict()]}
        raise
    state.preview_ok = report.ok
    state.preview_errors = list(report.errors)
    if not report.ok:
        state.last_error = {
            "stage": "validate",
            "errors": list(report.errors),
        }
        raise UFDemoError(
            report.errors[0]["code"] if report.errors else "CONFIG_INVALID",
            "准入校验未通过，未执行求解",
            field_path=report.errors[0]["field_path"] if report.errors else None,
            actual=None,
            suggestion="修正参数后重新提交；界面不会自动切换模式或变粗网格。",
        )

    run_id = make_run_id(label)
    run_dir = new_run_dir(out_base, run_id)

    # U07：把曲线目录传下去 —— 否则 `solver.response_curve` 指定的实测曲线
    # 加载不到，界面上「用真实数据求解」这条路永远走不通。
    result = solve(
        cfg,
        material,
        progress_callback=progress_callback,
        curves_dir=str(curves_dir) if curves_dir else None,
    )
    result.run_id = run_id
    saved = save_run(result, run_dir, project_root=project_root, code_info=code_info)

    unit = cfg.unit
    # 与导出同源：优先取求解结果里那份水印；缺失时才按卡重建
    wm = dict((result.metadata or {}).get("watermark") or {})
    if not wm:
        wm = watermark_of(material, unit, run_mode=cfg.run_mode)
        wm["geometry_feedback"] = cfg.solver.geometry_feedback
        wm["acceleration"] = cfg.solver.acceleration
    wm["warnings"] = list(result.warnings)

    frozen = FrozenRun(
        run_id=run_id,
        run_dir=str(run_dir),
        run_mode=cfg.run_mode,
        unit_system=unit.mode,
        input_hash=in_hash,
        submitted_at=now,
        status=result.status,
        material_watermark=wm,
        statistics=dict(result.statistics or {}),
        roi_statistics=list(result.rois or []),
        profiles=list(result.profiles or []),
        snapshots=list(result.snapshots or []),
        warnings=list(result.warnings),
        errors=list(result.errors),
        events_processed=result.events_processed,
        events_total=result.events_total,
        removal_available=result.removal_available,
        result=result,
        structure_diagnostics=build_structure_diagnostics(
            getattr(result, "diagnostics", None), (result.metadata or {}).get("structure")
        ),
        threshold_diagnostics=build_threshold_diagnostics(getattr(result, "diagnostics", None)),
        acceleration_diagnostics=build_acceleration_diagnostics(
            getattr(result, "diagnostics", None), getattr(result, "metadata", None)
        ),
        geometry_diagnostics=build_geometry_diagnostics(
            getattr(result, "diagnostics", None), getattr(result, "metadata", None)
        ),
        elapsed_s=getattr(result, "elapsed_s", None),
    )

    state.frozen = frozen
    state.submitted_params_hash = in_hash
    state.submitted_at = now
    state.solve_count += 1
    state.last_error = None
    state.history.append(
        {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "input_hash": in_hash,
            "submitted_at": now,
            "solve_index": state.solve_count,
            "status": result.status,
        }
    )
    return frozen


# ---------------------------------------------------------------------------
# 读取已有运行：不求解，只增加 read_count
# ---------------------------------------------------------------------------


def _loaded_elapsed_s(loaded: Any) -> float | None:
    """从运行目录读回求解耗时。``io`` 把它写在 ``diagnostics.json`` 顶层。"""
    diag = getattr(loaded, "diagnostics", None) or {}
    if not isinstance(diag, Mapping):
        return None
    val = diag.get("elapsed_s")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _grid_meta_from_disk(cfg_grid: Any, disk_surface: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """**读历史**时重建网格元信息。

    为什么需要：``run_to_payload`` 的 ``grid`` 来自 ``frozen.surface()``，
    而读历史路径下 ``result is None`` → ``surface()`` 返回 ``None`` → ``grid`` 变空字典。
    后果是**只有「读历史」这条路径**才会暴露的连锁故障：

    * 前端时间轴 ``drawHeatmap(..., f.grid.nx, f.grid.ny)`` 传入 ``undefined``；
    * ``createImageData(undefined, undefined)`` 抛
      ``Value is not of type 'long'``，回放画布永远空白（实测停在默认 300×150）；
    * 截面路径 ``Array.from(f.grid.xs)`` 同样拿不到坐标。

    修复数据来源（二者都在盘上，**不重算、不猜测**）：

    * ``nx`` / ``ny`` / ``dx`` / ``dy`` ← 落盘的 ``config.json`` 的 ``grid`` 段；
      缺 ``config`` 时退而用 ``final_surface.npz`` 里 ``height`` 的形状（``(ny, nx)``）；
    * ``xs`` / ``ys`` ← 落盘 surface 的 ``x`` / ``y`` 数组（**原始坐标，不重建**，
      避免浮点重建与原值出现末位差异）。

    返回 ``None`` 表示确实取不到（老运行缺 config 且缺 surface）——
    此时前端应显示「网格信息不可用」，而不是画一张空白图当成功。
    """
    g = dict(cfg_grid or {})
    out: dict[str, Any] = {}
    h = (disk_surface or {}).get("height")
    shape = getattr(h, "shape", None)
    if g.get("nx") and g.get("ny"):
        out["nx"] = int(g["nx"])
        out["ny"] = int(g["ny"])
        out["dx"] = float(g.get("dx_m") or 0.0)
        out["dy"] = float(g.get("dy_m") or 0.0)
    elif shape is not None and len(shape) == 2:
        out["ny"], out["nx"] = int(shape[0]), int(shape[1])
        out["dx"] = out["dy"] = 0.0
    else:
        return None
    ds = disk_surface or {}
    if ds.get("x") is not None:
        out["xs"] = ds["x"]
    if ds.get("y") is not None:
        out["ys"] = ds["y"]
    return out


def read_existing_run(state: SessionState, run_dir: str | Path) -> FrozenRun:
    """读取一个历史运行目录。**不调用求解器。**"""
    loaded = load_run(run_dir)
    cfg = dict(loaded.config or {})
    mat_snap = dict(loaded.material_snapshot or {})
    mat_snap = dict(mat_snap.get("card", mat_snap))
    # 回放同源：优先用导出时写下的 watermark（watermark.json / metadata），
    # 仅对旧运行（无该文件）才按卡与元数据重建，保证"导出=回放"。
    wm = dict(getattr(loaded, "watermark", None) or {})
    reconstructed = {
        "material_id": mat_snap.get("id") or cfg.get("material_id"),
        "family": (mat_snap.get("identity") or {}).get("family"),
        "grade": (mat_snap.get("identity") or {}).get("grade"),
        "evidence_status": mat_snap.get("evidence_status"),
        "source_type": mat_snap.get("source_type"),
        "card_version": mat_snap.get("card_version"),
        "card_sha256": loaded.metadata.get("material_card_sha256"),
        "physical_prediction_allowed": bool(
            (mat_snap.get("applicability") or {}).get("physical_material_prediction_allowed", False)
        ),
        "run_mode": cfg.get("run_mode") or loaded.metadata.get("run_mode"),
        "unit_system": (loaded.metadata.get("unit") or {}).get("unit_system")
        or loaded.metadata.get("unit_mode"),
        "length_label": ((loaded.metadata.get("unit") or {}).get("labels") or {}).get("length"),
        "fluence_label": ((loaded.metadata.get("unit") or {}).get("labels") or {}).get("fluence"),
        "depth_label": ((loaded.metadata.get("unit") or {}).get("labels") or {}).get("depth"),
        "physical_depth_export_allowed": (loaded.metadata.get("unit") or {}).get(
            "physical_depth_export_allowed"
        ),
        "geometry_feedback": (loaded.metadata.get("enabled_features") or {}).get("geometry_feedback"),
        "acceleration": (loaded.metadata.get("enabled_features") or {}).get("acceleration"),
        "warnings": list(loaded.warnings),
    }
    for k, v in reconstructed.items():
        wm.setdefault(k, v)

    frozen = FrozenRun(
        run_id=loaded.run_id,
        run_dir=str(run_dir),
        run_mode=str(cfg.get("run_mode") or ""),
        unit_system=str(wm.get("unit_system") or ""),
        input_hash=loaded.metadata.get("config_sha256", ""),
        submitted_at=loaded.metadata.get("written_at_utc", ""),
        status=loaded.status,
        material_watermark=wm,
        statistics=dict(loaded.statistics or {}),
        roi_statistics=_roi_statistics_from_stats(loaded.statistics),
        profiles=list(loaded.profiles or []),
        snapshots=list(loaded.snapshots or []),
        warnings=list(loaded.warnings),
        errors=list(loaded.diagnostics.get("errors", [])),
        events_processed=loaded.diagnostics.get("events_processed", 0),
        events_total=loaded.diagnostics.get("events_total", 0),
        removal_available=(
            "depth" in (loaded.surface or {})
            or ("height" in (loaded.surface or {}) and cfg.get("run_mode") != "threshold_only")
        ),
        result=None,
        disk_surface={
            k: v for k, v in (loaded.surface or {}).items() if hasattr(v, "shape")
        },
        snapshot_dir=(
            str(Path(run_dir) / "snapshots") if (loaded.snapshots or []) else None
        ),
        snapshot_files=[
            e.get("file") for e in (loaded.snapshots or []) if e.get("file")
        ],
        structure_diagnostics=build_structure_diagnostics(
            (loaded.diagnostics or {}).get("diagnostics"),
            (loaded.metadata or {}).get("structure"),
        ),
        threshold_diagnostics=build_threshold_diagnostics(
            (loaded.diagnostics or {}).get("diagnostics")
        ),
        acceleration_diagnostics=build_acceleration_diagnostics(
            (loaded.diagnostics or {}).get("diagnostics"),
            loaded.metadata,
        ),
        geometry_diagnostics=build_geometry_diagnostics(
            (loaded.diagnostics or {}).get("diagnostics"),
            loaded.metadata,
        ),
        elapsed_s=_loaded_elapsed_s(loaded),
    )
    # 读历史时 result 为 None → surface() 为 None → 契约层拿不到 grid。
    # 这里用**盘上已有**的 config 与坐标补回，使回放/截面可用（不重算）。
    frozen.grid_meta = _grid_meta_from_disk(cfg.get("grid"), loaded.surface)
    state.frozen = frozen
    state.read_count += 1
    # 读取历史结果不等于用当前参数求解：把提交哈希同步为该结果的输入哈希，
    # 避免刚读完就被误标为「上一次运行」。
    state.submitted_params_hash = frozen.input_hash
    state.submitted_at = frozen.submitted_at
    return frozen


# ---------------------------------------------------------------------------
# 视图与回放：纯读取，绝不增加求解次数
# ---------------------------------------------------------------------------


def snapshot_arrays(state: SessionState, snapshot_index: int | None) -> dict[str, Any]:
    """取快照/当前表面的数组。断言：不改变 ``solve_count``。"""
    before = state.solve_count
    out = {} if state.frozen is None else state.frozen.arrays_from(snapshot_index)
    assert state.solve_count == before, "回放/视图切换不得触发求解"
    return out


def layer_view(state: SessionState, layer: str, snapshot_index: int | None = None):
    """取某图层数组。断言：不改变 ``solve_count``。"""
    before = state.solve_count
    out = None if state.frozen is None else state.frozen.layer(layer, snapshot_index)
    assert state.solve_count == before, "切换图层不得触发求解"
    return out


def orient_view(array: Any, *, transpose: bool = False, flip_x: bool = False, flip_y: bool = False):
    """视图旋转/翻转（相机操作）。纯数组变换，**不求解**。

    细则 11.3：只旋转视图时读取已有数据。
    """
    if array is None:
        return None
    out = array.T if transpose else array
    if flip_y:
        out = out[::-1, :]
    if flip_x:
        out = out[:, ::-1]
    return out


def cross_section(array: Any, *, axis: str, index: int, coords: Any = None) -> dict[str, Any]:
    """从**已有数组**取截面，不重新求解。"""
    if array is None:
        return {"axis": axis, "index": index, "coord": [], "value": []}
    if axis == "x":
        line = array[index, :]
    elif axis == "y":
        line = array[:, index]
    else:
        raise UFDemoError(
            "CONFIG_INVALID", "截面轴必须是 'x' 或 'y'", field_path="axis", actual=axis
        )
    coord = [] if coords is None else list(coords)
    return {"axis": axis, "index": index, "coord": coord, "value": list(line)}


# ---------------------------------------------------------------------------
# 措辞守卫（细则 11.3：阈值图不写“热影响区”；剂量不写“温度”）
# ---------------------------------------------------------------------------


def assert_safe_wording(text: str, *, where: str = "ui") -> str:
    """检查界面/导出文案没有把剂量说成热学量、把阈值标记说成热损伤。

    采用**严格子串**口径：被禁词一旦出现即拒绝，包含否定式（“不是 X”）。
    这是刻意的——严格守卫才能作为可复审的安全网；澄清请改用 LAYER_LABELS 的
    措辞或 UNAVAILABLE_LAYERS 的原因文本。
    """
    hits = [t for t in FORBIDDEN_TERMS if t in text]
    if hits:
        raise UFDemoError(
            "CONFIG_INVALID",
            f"文案包含禁用措辞：{hits}",
            field_path=where,
            actual=text,
            requirement="剂量图不得读作热学量；阈值标记不得读作热学损伤区；纤维结构不是已预测的分层损伤",
            suggestion="改用 LAYER_LABELS 中给出的措辞（只描述该图实际是什么量）。",
        )
    return text


def depth_export_label(unit: UnitContext) -> str:
    """合成模式禁止导出物理深度（细则 4.1）。"""
    return "depth_um" if unit.allows_physical_depth_export else f"depth/{unit.depth_label}"


def guard_depth_export(unit: UnitContext, filename: str) -> str:
    if ("depth_um" in filename) and not unit.allows_physical_depth_export:
        raise UFDemoError(
            "CONFIG_INVALID",
            "合成模式禁止导出以 depth_um 命名的物理深度",
            field_path="export.filename",
            actual=filename,
            requirement=f"改用 depth/{unit.depth_label}",
            suggestion="合成深度与微米平面坐标不可混算。",
        )
    return filename


# ---------------------------------------------------------------------------
# 能力门槛与合成模式（显式选择，不自动切换）
# ---------------------------------------------------------------------------


def capability_gate(material: MaterialSpec, run_mode: str) -> tuple[bool, str]:
    """该材料能否用该模式做定量运行，并给出**准确**的不可用原因。"""
    if run_mode not in material.allowed_run_modes:
        return False, (
            f"材料卡未开放 {run_mode} 模式；允许：{list(material.allowed_run_modes)}"
        )
    if run_mode == "reference_case":
        cap = material.capability(CAP_EVENT_INCREMENT)
        if not cap.available:
            return False, f"参考定量不可用：{cap.reason}"
        # 人工解析 fixture 的条件由解析定义给出（细则 2.2：check_reference_conditions
        # 对 fixture_only 跳过条件匹配），因此**允许**运行，但结果必须标为非物理预测。
        if material.fixture_only:
            return True, (
                "允许：人工解析 fixture 基准（条件由解析定义）——结果标注为**非物理预测**，"
                "不得当作材料响应。"
            )
        if not material.is_physical():
            return True, (
                "允许参考运行，但材料卡未声明允许物理材料预测"
                "（applicability.physical_material_prediction_allowed=false）——"
                "结果标注为**非物理预测**。"
            )
        return True, cap.reason
    if run_mode == "synthetic_demo":
        return True, "允许：合成演示（参数与结构均标为合成，不解释为真实材料响应）"
    if run_mode == "threshold_only":
        cap = material.capability(CAP_THRESHOLD)
        return cap.available, (cap.reason if cap.available else f"阈值展示不可用：{cap.reason}")
    if run_mode == "calibrated_case":
        return False, "calibrated_case 需独立验证报告与批准版本，本批次未开放。"
    return False, f"未支持的模式 {run_mode!r}"


def synthetic_choices() -> list[dict[str, str]]:
    """可显式选择的合成示例（细则 2.2：用户必须明确选择，不得自动降级）。"""
    return [
        {
            "id": "synthetic_demo_point",
            "title": "合成定点脉冲（无量纲）",
            "note": "L_ref/F_ref/delta_ref 均为合成定义；禁止导出物理 μm 深度。",
        }
    ]


def suggest_synthetic(material: MaterialSpec, run_mode: str) -> dict[str, str] | None:
    """当某模式不可用时，**提示**可显式选择的合成示例（不自动启用）。"""
    ok, _reason = capability_gate(material, run_mode)
    if ok:
        return None
    return {
        "offer": "可显式切换到合成演示",
        "warning": "合成演示不是该材料的物理预测；不要把它当作材料响应结果。",
    }


# ---------------------------------------------------------------------------
# 模板与参数装配（界面表单 → 配置 JSON）
# ---------------------------------------------------------------------------


def list_examples(examples_dir: str | Path) -> list[str]:
    d = Path(examples_dir)
    if not d.exists():
        return []
    return sorted(p.name for p in d.glob("*.json"))


def load_template(examples_dir: str | Path, name: str) -> dict[str, Any]:
    p = Path(examples_dir) / name
    return json.loads(p.read_text(encoding="utf-8"))


def get_path(raw: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    """按 ``grid.nx`` / ``path.segments.0.speed_m_s`` 读取嵌套值。"""
    cur: Any = raw
    for part in dotted.split("."):
        if isinstance(cur, Mapping):
            if part not in cur:
                return default
            cur = cur[part]
        elif isinstance(cur, (list, tuple)):
            if not part.lstrip("-").isdigit():
                return default
            idx = int(part)
            if idx >= len(cur) or idx < -len(cur):
                return default
            cur = cur[idx]
        else:
            return default
    return cur


def apply_overrides(template: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """按 ``grid.nx`` / ``path.segments.0.speed_m_s`` 形式的点分路径写入覆写值。

    列表段支持整数下标（例如 ``path.segments.0``）。返回新 dict，不改原模板。
    可选字段（如 ``output.roi``）在模板里不存在时只在**字典**层级按需创建；
    若中途遇到列表而路径还没走完，则视为模板结构不匹配并报配置错误。
    """
    raw = copy.deepcopy(dict(template))
    for dotted, value in overrides.items():
        if value is None:
            continue
        parts = dotted.split(".")
        cur: Any = raw
        for i, part in enumerate(parts[:-1]):
            nxt = parts[i + 1]
            want_list = nxt.lstrip("-").isdigit()
            if isinstance(cur, dict):
                if want_list:
                    cur.setdefault(part, [])
                else:
                    cur.setdefault(part, {})
                cur = cur[part]
            elif isinstance(cur, list):
                if not part.lstrip("-").isdigit():
                    raise UFDemoError(
                        "CONFIG_INVALID",
                        f"覆写路径 {dotted!r} 试图在列表上使用非下标键 {part!r}",
                        field_path=dotted,
                        actual=part,
                        requirement="列表段必须是整数下标",
                    )
                idx = int(part)
                while len(cur) <= idx:
                    cur.append({} if not want_list else [])
                cur = cur[idx]
            else:
                raise UFDemoError(
                    "CONFIG_INVALID",
                    f"覆写路径 {dotted!r} 与模板结构不匹配",
                    field_path=dotted,
                    actual=type(cur).__name__,
                    requirement="路径上的中间节点必须是 dict 或 list",
                )
        last = parts[-1]
        if isinstance(cur, dict):
            cur[last] = value
        elif isinstance(cur, list) and last.lstrip("-").isdigit():
            idx = int(last)
            while len(cur) <= idx:
                cur.append(None)
            cur[idx] = value
        else:
            raise UFDemoError(
                "CONFIG_INVALID",
                f"覆写路径 {dotted!r} 的父节点类型不受支持",
                field_path=dotted,
                actual=type(cur).__name__,
            )
    return raw


def params_hash_is_reproducible(params: Mapping[str, Any]) -> bool:
    return params_hash(params) == params_hash(copy.deepcopy(dict(params)))


def stable_params_json(params: Mapping[str, Any]) -> str:
    return stable_json(params)


def iter_layer_names() -> Iterable[str]:
    return LAYER_LABELS.keys()


# ---------------------------------------------------------------------------
# 查表（批次 F）：纯读取，**绝不触发求解**
# ---------------------------------------------------------------------------


def list_curve_cards(curves_dir: str | Path) -> list[dict[str, Any]]:
    """列出曲线卡（``*.curve.json``）的摘要，供界面下拉与报告使用。

    只做文件枚举 + **读一个字段**；不加载 CSV、不插值、不求解。

    ``entry_class``（入口分层，U04）取值：

    * ``"measured"`` —— 真实实验数据，进**默认入口**；
    * ``"fixture"``  —— 人工解析 / 合成 / 公式重算，只在**「人工解析测试」入口**出现；
    * **缺该字段时按 ``"fixture"`` 处理** —— 未经分类的数据**不占默认入口**，
      这是保守方向：宁可少显示，也不把测试数据当实测展示给用户。

    历史上三张曲线卡都是 fixture（公式重算 / 人工解析 / 合成），
    却在默认下拉里以「YSZ」等名字出现，容易被读成材料实测数据 —— 这个字段就是为消除该歧义。
    """
    from . import tables

    out: list[dict[str, Any]] = []
    for p in tables.iter_curve_cards(curves_dir):
        entry_class = "fixture"
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            ec = raw.get("entry_class")
            if isinstance(ec, str) and ec.strip():
                entry_class = ec.strip()
        except Exception:  # noqa: BLE001 - 读不出就按 fixture（保守），不阻断列表
            pass
        out.append({
            "name": p.name,
            "path": str(p),
            "curve_id": p.name[: -len(".curve.json")],
            "entry_class": entry_class,
        })
    return out


def load_curve_card(curves_dir: str | Path, name: str):
    """加载并校验一张曲线卡。不求解。"""
    from . import tables
    root = Path(curves_dir).resolve()
    candidate_name = Path(name)
    if candidate_name.name != name or candidate_name.suffixes[-2:] != [".curve", ".json"]:
        raise ValueError("曲线卡名称不合法")
    candidate = (root / name).resolve()
    if candidate.parent != root:
        raise ValueError("曲线路径越界")
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return tables.load_curve(candidate)


def table_lookup(
    state: SessionState,
    curve,
    x: Any,
    *,
    method: str = "linear",
    allow_out_of_range: bool = False,
):
    """在曲线上查值。**断言不改变 ``solve_count``**——查表不是求解。

    越界时：``allow_out_of_range=False`` 会抛 ``TABLE_OUT_OF_RANGE``（界面据此提示），
    ``True`` 时返回 ``values`` 含 ``None`` 的报告，**绝不返回 0**。
    """
    from . import tables

    before = state.solve_count
    res = tables.lookup(curve, x, method=method, allow_out_of_range=allow_out_of_range)
    assert state.solve_count == before, "查表不得触发求解"
    return res


def table_grid(state: SessionState, curve, *, n: int = 200, method: str = "linear") -> dict[str, Any]:
    """在有效区间上采样原始点与插值线，供「原始点与插值图」。断言不求解。"""
    from . import tables

    before = state.solve_count
    grid = tables.interpolate_grid(curve, n=n, method=method)
    assert state.solve_count == before, "生成插值图不得触发求解"
    return grid


def curve_capability_rows(curve) -> list[dict[str, Any]]:
    """曲线去向与可派生的量（如实展示可做什么、不可做什么）。"""
    from . import tables

    rows = [
        {"项": "输出语义", "值": curve.output_semantics},
        {"项": "去向", "值": curve.route_zh},
        {"项": "可进事件核（语义）", "值": "是" if curve.can_enter_event_kernel else "否"},
        {"项": "可派生的量", "值": "、".join(tables.derivable_quantities(curve))},
        {"项": "有效区间", "值": f"[{curve.valid_range[0]!r}, {curve.valid_range[1]!r}] {curve.x_unit}"},
        {"项": "证据状态", "值": curve.evidence_status},
    ]
    return rows


def material_entry_rows(material_dir: str | Path) -> list[dict[str, Any]]:
    """七材料能力入口表（纯逻辑，不导入 Streamlit；供界面与 G09 报告共用）。"""
    from .materials import load_material_catalog, material_entry_rows as _entry_rows

    catalog = load_material_catalog(material_dir)
    return _entry_rows(catalog, material_dir)


def material_entry_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """入口表汇总：开放 / 红线 / 缺口 计数，并列出缺口条目。"""
    rows = list(rows)
    opened = [r for r in rows if r.get("kind") == "opened"]
    blocked = [r for r in rows if r.get("kind") == "blocked"]
    deferred = [r for r in rows if r.get("kind") == "deferred"]
    filled = [r for r in rows if r.get("verified") is not None]
    return {
        "n_opened": len(opened),
        "n_blocked": len(blocked),
        "n_deferred": len(deferred),
        "n_unverified": len(rows) - len(filled),
        "all_verified": len(filled) == len(rows),
        "all_probes_hold": all(r.get("verified") is True for r in rows),
        "deferred_items": [
            {"family": r.get("family"), "item": r.get("item"), "reason": r.get("binding")}
            for r in deferred
        ],
    }


def list_measured_datasets(measured_dir: str | Path) -> dict[str, Any]:
    """列出实测数据集及其**权限判定**（U05）。

    **权限判定复用 `datasets.evaluate`** —— 界面与接口层不得各写一套判定，
    否则两处会漂移（一处放宽、一处收紧，用户看到的行为无法解释）。

    只读注册表 + 逐条判定；**不求解、不插值**。
    注册表缺失时返回 `available=False` 与原因，**不抛异常、不伪造数据**。
    """
    from . import datasets as DS

    measured = Path(measured_dir)
    reg = DS.load_registry(measured)
    datasets = reg.get("datasets") or []
    if not datasets:
        return {
            "available": False,
            "reason": (
                "未找到数据集注册表（data/measured/registry.json）。"
                "运行 tools/measured_data_report.py --write-registry 生成。"
            ),
            "datasets": [],
            "summary": None,
            "observation_semantics": list(DS.OBSERVATION_SEMANTICS),
        }
    return {
        "available": True,
        "path": str(measured),
        "schema": reg.get("schema"),
        "counting_note": reg.get("counting_note"),
        "observation_semantics": list(DS.OBSERVATION_SEMANTICS),
        "semantics_zh": dict(DS.OBSERVATION_SEMANTICS_ZH),
        "summary": reg.get("summary"),
        "datasets": datasets,
    }


def assert_dataset_increment_access(record: Mapping[str, Any]) -> None:
    """请求把某条实测记录用作**逐事件增量**时的闸门（U05）。

    直接委派 `datasets.assert_increment_access`，后者又委派既有闸门
    `response.assert_increment_semantics` —— **抛既定错误码
    ``RESPONSE_SEMANTICS_INVALID``**，不新造码。
    """
    from . import datasets as DS

    DS.assert_increment_access(record)


def build_diamond_evaluator(measured_dir: str | Path):
    """从实测数据建构金刚石过程响应评估器（U06）。返回 ``None`` 表示数据缺失。

    **每次调用重新拟合**：17 行数据、10 个系数，代价可忽略；
    缓存反而会引入「数据变了但模型没更新」的隐患。
    """
    import csv as _csv

    from .evaluators import ProcessEvaluator

    path = Path(measured_dir) / "diamond_rsm_measured.csv"
    if not path.exists():
        return None
    rows = list(_csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    if not rows:
        return None
    return ProcessEvaluator.from_rows(rows)


def diamond_evaluator_summary(measured_dir: str | Path) -> dict[str, Any]:
    """过程响应评估器的**摘要**：支持范围、指标、门槛判定、留出对照。

    界面用它渲染「数据支持范围 + 预测」面板。
    **不产生形貌、不求解网格**。
    """
    from . import evaluators as EV

    ev = build_diamond_evaluator(measured_dir)
    if ev is None:
        return {
            "available": False,
            "reason": (
                "未找到 data/measured/diamond_rsm_measured.csv（U04 未导入）。"
                "过程响应评估器需要该实测数据才能拟合。"
            ),
        }

    # 指标：训练 / 分组五折 / 留出。**三者必须并报** ——
    # 只报留出会把 3.26% 当成模型水平，而分组五折的深度是 52.42%。
    import numpy as _np

    from .evaluators import (
        DEFAULT_ALPHA,
        GROUPED_CV_SEED,
        INPUTS,
        OUTPUTS,
        QuadraticResponseSurface,
        design,
    )

    import csv as _csv

    path = Path(measured_dir) / "diamond_rsm_measured.csv"
    rows = list(_csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    train = [r for r in rows if r["split_role"] == "calibration_candidate"]
    hold = [r for r in rows if r["split_role"] == "held_out_published_confirmation"]

    def _metric(pred, actual):
        out = {}
        for j, name in enumerate(OUTPUTS):
            d = pred[:, j] - actual[:, j]
            out[name] = {
                "MAE": float(_np.mean(_np.abs(d))),
                "RMSE": float(_np.sqrt(_np.mean(d**2))),
                "MAPE_percent": float(100.0 * _np.mean(_np.abs(d / actual[:, j]))),
            }
        return out

    surf = ev.surface
    assert surf.coef_ is not None
    x = _np.array([[float(r[k]) for k in INPUTS] for r in train])
    y = _np.array([[float(r[k]) for k in OUTPUTS] for r in train])
    train_metrics = _metric(design(x) @ surf.coef_, y)

    # 分组五折（与报告同一 seed、同一划分）
    groups = _np.array([r["condition_group"] for r in train])
    unique = _np.unique(groups)
    rng = _np.random.default_rng(GROUPED_CV_SEED)
    shuffled = unique.copy()
    rng.shuffle(shuffled)
    oof = _np.full_like(y, _np.nan)
    folds = []
    for k, vg in enumerate(_np.array_split(shuffled, 5)):
        va = _np.isin(groups, vg)
        tr = ~va
        s = QuadraticResponseSurface(alpha=DEFAULT_ALPHA).fit(x[tr], y[tr])
        assert s.coef_ is not None
        oof[va] = design(x[va]) @ s.coef_
        folds.append({
            "fold": k,
            "train_records": int(tr.sum()),
            "validation_records": int(va.sum()),
            "train_design_rank": int(_np.linalg.matrix_rank(design(x[tr]))),
            "validation_groups": [str(g) for g in vg],
        })
    grouped_metrics = _metric(oof, y)

    hold_block = None
    if hold:
        hx = _np.array([[float(r[k]) for k in INPUTS] for r in hold])
        hy = _np.array([[float(r[k]) for k in OUTPUTS] for r in hold])
        hp = design(hx) @ surf.coef_
        hold_block = {
            "case_ids": [str(r["case_id"]) for r in hold],
            "inputs": {k: float(hx[0, j]) for j, k in enumerate(INPUTS)},
            "measured": {k: float(hy[0, j]) for j, k in enumerate(OUTPUTS)},
            "predicted": {k: float(hp[0, j]) for j, k in enumerate(OUTPUTS)},
            "metrics": _metric(hp, hy),
        }

    depth_mape = grouped_metrics["depth_um"]["MAPE_percent"]
    return {
        "available": True,
        "material_family": ev.material_family,
        "source_doi": ev.source_doi,
        "n_training_rows": ev.n_training_rows,
        "n_distinct_conditions": ev.n_distinct_conditions,
        "inputs": list(INPUTS),
        "outputs": list(OUTPUTS),
        "regularization_alpha": DEFAULT_ALPHA,
        "grouped_cv_seed": GROUPED_CV_SEED,
        "support": surf.check_support(x).to_dict() if hasattr(surf, "check_support") else None,
        "support_bounds": {k: list(v) for k, v in surf.bounds_.items()},
        "training_metrics": train_metrics,
        "grouped_cv_metrics": grouped_metrics,
        "holdout": hold_block,
        "folds": folds,
        "gate": ev.validation_verdict(depth_mape),
        "warnings": list(ev.warnings),
        "disclaimer": (
            "**辅助手段，不替代逐事件物理引擎的验证。** "
            "本面板是工艺级过程响应（宽/深/Ra），不是单脉冲去除律，"
            "不进主循环、不改变形貌。"
        ),
    }


def list_runs(base_dir: str | Path, *, max_depth: int = 2) -> list[dict[str, Any]]:
    """列出可读取的运行目录（``runs/<id>`` 与 ``runs/<group>/<id>``）。

    只做目录扫描，不加载 NPZ、不求解。
    """
    base = Path(base_dir)
    out: list[dict[str, Any]] = []
    if not base.exists():
        return out
    candidates: list[Path] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        if (child / "metadata.json").exists():
            candidates.append(child)
        elif max_depth > 1:
            for gc in sorted(child.iterdir()):
                if gc.is_dir() and (gc / "metadata.json").exists():
                    candidates.append(gc)
    for d in candidates:
        try:
            md = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # pragma: no cover - 目录损坏
            continue
        out.append(
            {
                "run_id": md.get("run_id", d.name),
                "run_dir": str(d),
                "status": md.get("status"),
                "status_zh": STATUS_ZH.get(md.get("status"), md.get("status")),
                "written_at_utc": md.get("written_at_utc"),
                "run_mode": (md.get("run_mode") or (md.get("reference_result") or {}).get("reference_kind")),
                "mtime": d.stat().st_mtime,
            }
        )
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out
