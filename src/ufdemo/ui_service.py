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

界面层（``app.py``）只负责渲染与调用本模块，不直接碰求解器。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

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
    "cumulative_fluence": "累计入射剂量（能量沉积观测量）",
    "illumination_count": "有效照射计数",
    "threshold_mask": "超阈值/改性标记（受限阈值协议量）",
    "warning_mask": "域外/截断等警告标记",
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
        "本批次未记录逐事件超阈值掩膜；阈值观测量属批次 H 的受限阈值协议。"
        "不得用累计剂量与单脉冲阈值比较来伪造该标记，也不得把它读作热学损伤标记。"
    ),
}

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
    """每个结果必须持续显示的标签（细则 11.3、任务书 13 节）。"""
    wm = material.watermark()
    wm.update(
        {
            "run_mode": run_mode,
            "unit_system": unit.mode,
            "length_label": unit.length_label,
            "fluence_label": unit.fluence_label,
            "depth_label": unit.depth_label,
            "physical_depth_export_allowed": unit.allows_physical_depth_export,
            "geometry_feedback": None,  # 由调用方补
            "acceleration": None,
            "warnings": [],
        }
    )
    return wm


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
    profiles: list[dict[str, Any]] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    events_processed: int = 0
    events_total: int = 0
    removal_available: bool = True
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
        return out

    def available_layers(self, snapshot_index: int | None = None) -> dict[str, str | None]:
        """返回 ``{layer: 不可用原因或 None}``，供界面如实展示可用性。"""
        arrs = self.arrays_from(snapshot_index)
        out: dict[str, str | None] = {}
        for name in LAYER_LABELS:
            if name in UNAVAILABLE_LAYERS:
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

    result = solve(cfg, material)
    result.run_id = run_id
    saved = save_run(result, run_dir, project_root=project_root, code_info=code_info)

    unit = cfg.unit
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
        profiles=list(result.profiles or []),
        snapshots=list(result.snapshots or []),
        warnings=list(result.warnings),
        errors=list(result.errors),
        events_processed=result.events_processed,
        events_total=result.events_total,
        removal_available=result.removal_available,
        result=result,
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


def read_existing_run(state: SessionState, run_dir: str | Path) -> FrozenRun:
    """读取一个历史运行目录。**不调用求解器。**"""
    loaded = load_run(run_dir)
    cfg = dict(loaded.config or {})
    mat_snap = dict(loaded.material_snapshot or {})
    wm = {
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
        "run_mode": cfg.get("run_mode"),
        "unit_system": (loaded.metadata.get("unit") or {}).get("unit_system"),
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
        profiles=list(loaded.profiles or []),
        snapshots=list(loaded.snapshots or []),
        warnings=list(loaded.warnings),
        errors=[],
        events_processed=len(loaded.events or []),
        events_total=len(loaded.events or []),
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
    )
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

    只做文件枚举；**不加载 CSV、不插值、不求解**。
    """
    from . import tables

    out: list[dict[str, Any]] = []
    for p in tables.iter_curve_cards(curves_dir):
        out.append({"name": p.name, "path": str(p), "curve_id": p.name[: -len(".curve.json")]})
    return out


def load_curve_card(curves_dir: str | Path, name: str):
    """加载并校验一张曲线卡。不求解。"""
    from . import tables

    return tables.load_curve(Path(curves_dir) / name)


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
