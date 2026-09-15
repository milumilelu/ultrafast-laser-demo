"""实验 CSV 标定（C4）。

把实验测得的平均深度，标定到**逐脉冲求解器**上。任务书 §4/§5 的硬要求：

* 只开放**一个正增益** ``a``：``Δd_cal = a·Δd_base``；
* ``a`` 作用在**每个脉冲的几何更新之前** —— 由 ``solver`` 的
  ``response_gain`` 承担，因此后续脉冲会按新表面重算被动离焦，
  ``D(a) ≠ a·D(1)``。**不得**退化成"结果页乘系数"；
* ``a`` 与材料卡的 ``δ`` **不同时自由拟合**（两者都控幅度，会互相抵消）；
* 训练/留出按**工况组**划分（重复工况同组，不得跨侧）；
* 实验端**只收 CSV**，不读共聚焦高度图；已有标量（均值/Sa 等）直接用；
* 负值、重复点**保留原样**，不自动取绝对值或删除。

依赖 SciPy 的 ``least_squares``（有边界 + 可选 soft_l1），**不自造优化器**。
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import (
    CONFIG_INVALID,
    RunConfig,
    load_shared_background,
    shared_background_patch,
)
from .errors import UFDemoError
from .planning import serpentine_plan

#: 中文表头 → 内部字段。**支持现有文件，无需先重写**（任务书 §5.1）。
HEADER_MAP_ZH: Mapping[str, str] = {
    "序号": "sample_id",
    "脉宽fs": "pulse_duration_fs",
    "频率kHz": "repetition_rate_kHz",
    "间距mm": "hatch_spacing_mm",
    "重复加工次数": "pass_count",
    "速度mm/s": "scan_speed_mm_s",
    "mean_depth_um": "mean_depth_um",
    "Sa_um": "Sa_um",
    "Sq_um": "Sq_um",
    "Sz_um": "Sz_um",
    "min_depth_um": "min_depth_um",
    "max_depth_um": "max_depth_um",
    "use_for_fit": "use_for_fit",
    "session_id": "session_id",
    "measurement_id": "measurement_id",
}

#: 编码探测顺序。实验表常见 GB18030（中文表头），不能假定 UTF-8。
_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "gb18030", "utf-16", "latin-1")

#: 参与标定的必需字段。缺任何一个都无法建立"工况 → 深度"的对应。
REQUIRED_FIELDS: tuple[str, ...] = (
    "pulse_duration_fs", "repetition_rate_kHz", "scan_speed_mm_s",
    "hatch_spacing_um", "pass_count", "mean_depth_um",
)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if s == "" or s.lower() in ("null", "none", "na", "n/a", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _read_text_any_encoding(path: Path) -> tuple[str, str]:
    """按常见编码依次尝试；返回 ``(文本, 实际编码)``。

    为什么要探测：实验表是**中文表头 + GB18030**，直接 UTF-8 读会抛
    ``UnicodeDecodeError``。探测而非硬编码，才能同时容纳以后导出的 UTF-8 表。
    """
    raw = path.read_bytes()
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    raise UFDemoError(
        CONFIG_INVALID,
        "实验 CSV 编码无法识别",
        field_path="calibration.csv",
        actual=str(path),
        requirement=f"可解码为 {list(_ENCODINGS)} 之一",
        suggestion="另存为 UTF-8 或 GB18030。",
    )


@dataclass
class ExperimentRow:
    """一条实验记录。**负值与重复保留原样**，不做静默清洗。"""

    sample_id: str
    pulse_duration_fs: float
    repetition_rate_kHz: float
    scan_speed_mm_s: float
    hatch_spacing_um: float
    pass_count: int
    mean_depth_um: float
    Sa_um: float | None = None
    Sq_um: float | None = None
    Sz_um: float | None = None
    min_depth_um: float | None = None
    max_depth_um: float | None = None
    use_for_fit: bool | None = None
    source_row: int = 0
    extras: dict[str, Any] = field(default_factory=dict)

    # -- 派生 ----------------------------------------------------------------
    @property
    def condition_key(self) -> tuple:
        """工况指纹 —— 用于**把重复工况归到同一组**，避免跨训练/留出泄漏。"""
        return (
            round(self.pulse_duration_fs, 6),
            round(self.repetition_rate_kHz, 6),
            round(self.scan_speed_mm_s, 6),
            round(self.hatch_spacing_um, 6),
            int(self.pass_count),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sampleId": self.sample_id,
            "pulseDurationFs": self.pulse_duration_fs,
            "repetitionRateKHz": self.repetition_rate_kHz,
            "scanSpeedMmS": self.scan_speed_mm_s,
            "hatchSpacingUm": self.hatch_spacing_um,
            "passCount": self.pass_count,
            "meanDepthUm": self.mean_depth_um,
            "SaUm": self.Sa_um,
            "SqUm": self.Sq_um,
            "SzUm": self.Sz_um,
            "sourceRow": self.source_row,
        }


@dataclass
class ExperimentTable:
    """一份实验表的规范化结果。**原始 CSV 不被修改。**"""

    rows: list[ExperimentRow]
    source_path: str
    encoding: str
    n_raw_rows: int
    skipped_rows: list[dict[str, Any]] = field(default_factory=list)
    notes: tuple[str, ...] = ()

    @property
    def n_used(self) -> int:
        return len(self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourcePath": self.source_path,
            "encoding": self.encoding,
            "nRawRows": self.n_raw_rows,
            "nUsedRows": self.n_used,
            "skipped": list(self.skipped_rows),
            "notes": list(self.notes),
            "rows": [r.to_dict() for r in self.rows],
        }


def load_experiment_csv(path: str | Path) -> ExperimentTable:
    """读实验 CSV → 规范化行。

    * 自动探测编码（GB18030 / UTF-8）；
    * 中文表头映射；**间距 mm → μm（×1000）**；
    * 缺必需字段的行**单独列出**（不静默丢弃，也不猜值补齐）。
    """
    p = Path(path)
    if not p.exists():
        raise UFDemoError(
            CONFIG_INVALID, "实验 CSV 不存在",
            field_path="calibration.csv", actual=str(p), requirement="文件存在",
        )
    text, enc = _read_text_any_encoding(p)
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise UFDemoError(
            CONFIG_INVALID, "实验 CSV 没有表头",
            field_path="calibration.csv", actual=str(p), requirement="第一行是表头",
        )

    # 表头归一：中文优先，其次原样（已英文的表直接可用）
    colmap: dict[str, str] = {}
    for raw_name in reader.fieldnames:
        name = (raw_name or "").strip()
        colmap[raw_name] = HEADER_MAP_ZH.get(name, name)

    rows: list[ExperimentRow] = []
    skipped: list[dict[str, Any]] = []
    n_raw = 0
    for i, raw in enumerate(reader, start=2):   # 行号从 2 起（跳过表头）
        n_raw += 1
        rec = {colmap.get(k, k): v for k, v in raw.items() if k is not None}
        depth = _to_float(rec.get("mean_depth_um"))
        dur = _to_float(rec.get("pulse_duration_fs"))
        freq = _to_float(rec.get("repetition_rate_kHz"))
        speed = _to_float(rec.get("scan_speed_mm_s"))
        passes = _to_float(rec.get("pass_count"))
        # 间距列可能叫 hatch_spacing_mm（中文表）或 hatch_spacing_um（英文表）
        if rec.get("hatch_spacing_mm") not in (None, ""):
            sp_mm = _to_float(rec.get("hatch_spacing_mm"))
            spacing_um = None if sp_mm is None else sp_mm * 1000.0   # mm → μm
        else:
            spacing_um = _to_float(rec.get("hatch_spacing_um"))

        missing = [
            k for k, v in (
                ("pulse_duration_fs", dur), ("repetition_rate_kHz", freq),
                ("scan_speed_mm_s", speed), ("hatch_spacing_um", spacing_um),
                ("pass_count", passes), ("mean_depth_um", depth),
            ) if v is None
        ]
        if missing:
            skipped.append({"row": i, "missing": missing})
            continue

        use_flag = rec.get("use_for_fit")
        use_for_fit: bool | None = None
        if use_flag not in (None, ""):
            use_for_fit = str(use_flag).strip().lower() in ("1", "true", "yes", "y", "是")

        rows.append(ExperimentRow(
            sample_id=str(rec.get("sample_id") or i).strip() or str(i),
            pulse_duration_fs=float(dur),
            repetition_rate_kHz=float(freq),
            scan_speed_mm_s=float(speed),
            hatch_spacing_um=float(spacing_um),
            pass_count=int(passes),
            mean_depth_um=float(depth),   # **负值原样保留**
            Sa_um=_to_float(rec.get("Sa_um")),
            Sq_um=_to_float(rec.get("Sq_um")),
            Sz_um=_to_float(rec.get("Sz_um")),
            min_depth_um=_to_float(rec.get("min_depth_um")),
            max_depth_um=_to_float(rec.get("max_depth_um")),
            use_for_fit=use_for_fit,
            source_row=i,
        ))

    notes = [
        f"编码探测结果：{enc}；原始 {n_raw} 行，采用 {len(rows)} 行。",
        "**间距列按 mm 读取并 ×1000 转 μm**（中文表头「间距mm」）。",
        "负值与重复工况**原样保留**；未做取绝对值/截零/去重。",
    ]
    if skipped:
        notes.append(f"{len(skipped)} 行缺必需字段，已单独列出（未猜值补齐）。")
    neg = [r.sample_id for r in rows if r.mean_depth_um < 0]
    if neg:
        notes.append(
            f"{len(neg)} 行均值为**负**（{neg[:5]}…）：纯去除模型无法解释，"
            "在口径查清前单独报告，**不**用负增益去拟合。"
        )
    return ExperimentTable(rows=rows, source_path=str(p), encoding=enc,
                           n_raw_rows=n_raw, skipped_rows=skipped, notes=tuple(notes))


# ---------------------------------------------------------------------------
# 分组（训练 / 留出）
# ---------------------------------------------------------------------------


def group_split(
    rows: Sequence[ExperimentRow], *, holdout_groups: int = 5, seed: int = 20260913,
) -> tuple[list[ExperimentRow], list[ExperimentRow], dict[str, Any]]:
    """按**工况组**划分训练/留出：同一工况（重复测量）必须同侧。

    任务书 §5.2：重复工况同组划分；有 ``use_for_fit`` 时尊重人工选择。
    """
    by_key: dict[tuple, list[ExperimentRow]] = {}
    for r in rows:
        by_key.setdefault(r.condition_key, []).append(r)

    keys = sorted(by_key.keys(), key=lambda k: str(k))
    n_groups = len(keys)
    info: dict[str, Any] = {
        "n_rows": len(rows),
        "n_groups": n_groups,
        "n_groups_with_repeats": sum(1 for k in keys if len(by_key[k]) > 1),
    }
    if n_groups <= 1:
        return list(rows), [], {**info, "note": "只有一个工况组，无法划分留出；全部用于训练。"}

    # 人工标记优先：标了 use_for_fit 的行按标记走，未标的按组划分
    forced_fit = [r for r in rows if r.use_for_fit is True]
    forced_hold = [r for r in rows if r.use_for_fit is False]

    n_hold = max(1, min(int(holdout_groups), n_groups - 1))
    import random

    rng = random.Random(seed)
    shuffled = keys[:]
    rng.shuffle(shuffled)
    hold_keys = set(shuffled[:n_hold])

    train: list[ExperimentRow] = []
    hold: list[ExperimentRow] = []
    for k in keys:
        target = hold if k in hold_keys else train
        for r in by_key[k]:
            if r.use_for_fit is True:
                train.append(r)
            elif r.use_for_fit is False:
                hold.append(r)
            else:
                target.append(r)

    info.update({
        "holdout_groups": sorted(str(k) for k in hold_keys),
        "n_train": len(train),
        "n_holdout": len(hold),
        "forced_rows": {"fit": len(forced_fit), "holdout": len(forced_hold)},
        "seed": seed,
    })
    # **断言不跨侧**：同一工况的行不得同时出现在训练与留出
    train_keys = {r.condition_key for r in train}
    hold_keys_set = {r.condition_key for r in hold}
    overlap = train_keys & hold_keys_set
    info["leakage_groups"] = sorted(str(k) for k in overlap)
    return train, hold, info


# ---------------------------------------------------------------------------
# 预测：CSV 工况 → 同一套逐脉冲求解器
# ---------------------------------------------------------------------------


@dataclass
class ObservationSpec:
    """观测口径（任务书 §5.1）。**这只是一个统计口径，不是导入高度图。**

    实验 CSV 给的均值是对**整个加工区**还是**中央测量窗口**统计的，
    模型必须用**同一口径**去算，否则两边在比不同的东西。
    """

    kind: str = "full_region_mean"      # full_region_mean | center_window_mean
    center_window_um: tuple[float, float] | None = None
    statistic: str = "mean"             # mean | median

    def describe(self) -> str:
        if self.kind == "center_window_mean" and self.center_window_um:
            return (f"中央 {self.center_window_um[0]:g}×{self.center_window_um[1]:g} μm "
                    f"窗口的{self.statistic}")
        return f"全加工区的{self.statistic}"


@dataclass
class PredictionSpec:
    """怎么把一条 CSV 行变成一次求解。"""

    material_card_file: str
    window_um: float = 40.0        # **仿真域**边长（μm）—— 网格范围
    dx_um: float = 0.2             # 网格步长
    #: **实际加工区**边长（μm）。默认与 ``window_um`` 相同（旧行为）。
    #: 单独设成比域小，是为了在加工区外围留出**未被加工的余量**，
    #: 这样边界效应不主导统计，也能看出"路径有没有跑出加工区"。
    #: 两者**同心**（网格以 ``center_x_m=0`` 为中心）。
    machining_region_um: float | None = None
    #: **加工前的原始上表面高度**（m）。焦点策略是 ``fixed_original_surface``：
    #: 焦平面恒为这个值，**不随槽底下降调整**。这里显式传给网格与路径，
    #: 使「焦平面 = 原始表面」**按构造成立**，而不是依赖它是 0。
    #: 非零值用于模拟工件表面高于/低于基准面的情形。
    initial_height_m: float = 0.0
    observation: ObservationSpec = field(default_factory=ObservationSpec)
    #: 覆盖材料卡 ``response`` 的部分字段（C3 反推基线用）。
    #: **不写盘**：在内存构造 MaterialSpec，原始材料卡文件保持不变。
    response_override: Mapping[str, Any] | None = None

    def describe_approximation(self) -> str:
        return (
            f"用 {self.window_um:g}×{self.window_um:g} μm 的**代表窗口**（dx={self.dx_um:g} μm）"
            "近似全区域：包含多个间距周期，但**不含**区域边界的端部效应。"
            "这是模型预测，不是已通过实验高度图验证的二维形貌。"
        )


def build_row_config(
    row: ExperimentRow,
    *,
    spec: PredictionSpec,
    gain: float = 1.0,
    bg: Any | None = None,
) -> RunConfig:
    """把一条实验行 + 共用背景 + 标定增益 → 一个完整 ``RunConfig``。

    要点：
    * 光学固定项**继承共用背景**（功率/波长/M²/w0/焦点策略），
      逐行变量（脉宽/频率/速度/间距/遍数）用**该行原值**；
    * ``E_p = P_物镜后 / f``（该行的 f）；
    * 焦点 Z 恒为初始表面（``fixed_original_surface`` + ``axial_defocus``）。
    """
    from . import resource_root

    bg = bg or load_shared_background()
    # 有阈值时按**声明的实测单线宽度**反推等效光斑半径 ——
    # 否则拿名义 w0（0.874 μm）去算，烧蚀宽度只有 3.96 μm，与实测 5 μm 不符，
    # 覆盖/搭接判断会整体偏窄（这正是之前 h/N「覆盖 5–24%」的成因之一）。
    _thr = (spec.response_override or {}).get("threshold_J_m2")
    patch = shared_background_patch(
        bg, repetition_rate_Hz=row.repetition_rate_kHz * 1e3,
        threshold_J_m2=float(_thr) if _thr else None,
    )

    domain = float(spec.window_um)                       # 仿真域 → 网格范围
    region = float(spec.machining_region_um or domain)   # 加工区 → 蛇形路径范围
    if region > domain + 1e-9:
        raise UFDemoError(
            CONFIG_INVALID,
            "加工区不能大于仿真域",
            field_path="calibration.machining_region_um",
            actual=f"region={region} domain={domain}",
            requirement="machining_region_um ≤ window_um",
            suggestion="把仿真域放大，或缩小加工区；外围需要留余量。",
        )
    nx = max(8, int(round(domain / spec.dx_um)))
    ny = nx

    # 材料卡要先解析出 material_id —— RunConfig 需要它做准入与元数据
    card_path0 = Path(spec.material_card_file)
    if not card_path0.is_absolute():
        from . import project_root as _pr0

        card_path0 = _pr0() / card_path0
    _card_raw = json.loads(card_path0.read_text(encoding="utf-8"))
    material_id = str(_card_raw.get("material_id") or _card_raw.get("id") or "").strip()
    if not material_id:
        raise UFDemoError(
            CONFIG_INVALID,
            "材料卡缺少 material_id",
            field_path="material_card_file",
            actual=str(card_path0),
            requirement="卡内含 material_id（或 id）",
        )

    plan = serpentine_plan(
        region_um=(region, region),      # 只在**加工区**内走刀，外围留白
        spacing_um=row.hatch_spacing_um,
        pass_count=row.pass_count,
        scan_speed_mm_s=row.scan_speed_mm_s,
        # **焦平面 = 加工前的原始上表面**（不是 0、也不随层数变化）。
        # 由 config.validate_run 复核「每一段的 z 都等于 grid.initial_height_m」。
        focus_z_m=float(spec.initial_height_m),
    )

    raw: dict[str, Any] = {
        "schema_version": "1.0",
        "run_mode": "reference_case",
        "unit": {"mode": "SI"},
        "material_id": material_id,
        "material_card_file": spec.material_card_file,
        "grid": {
            "nx": nx, "ny": ny,
            "dx_m": spec.dx_um * 1e-6, "dy_m": spec.dx_um * 1e-6,
            # 原始表面高度：焦平面就钉在它上面（focus_strategy=fixed_original_surface）
            "initial_height_m": float(spec.initial_height_m),
        },
        "laser": {
            **patch["laser"],
            "pulse_duration_s": row.pulse_duration_fs * 1e-15,
        },
        # 路径由 planning 的 PathPlan 生成（**正向与导出同一份**），
        # 再用 to_segments_config 转成求解器吃的格式。
        "path": {"t0_s": 0.0, "segments": plan.to_segments_config()},
        "solver": {
            "mode": "reference",
            **patch["solver"],
            "response_gain": float(gain),      # ← 标定增益进入每次几何更新
            "history_enabled": False,
        },
        "output": {},
    }
    raw["_path_plan_n_lines"] = plan.n_scan_lines
    raw["_machining_region_um"] = region
    raw["_domain_um"] = domain
    raw["_spot_radius_basis"] = dict(patch["laser"].get("_spot_radius_basis") or {})
    raw["_path_plan_notes"] = list(plan.notes)

    from . import project_root as _pr

    card = Path(spec.material_card_file)
    if not card.is_absolute():
        raw["material_card_file"] = str((_pr() / card).resolve())
    cfg = RunConfig.from_dict(raw, base_dir=str(_pr()))
    return cfg


def predict_mean_depth(
    row: ExperimentRow,
    *,
    spec: PredictionSpec,
    gain: float = 1.0,
    bg: Any | None = None,
) -> dict[str, Any]:
    """跑一次求解，按观测口径取深度。**每次调用都真的跑求解器。**"""
    from .materials import load_material_card
    from .solver import solve

    cfg = build_row_config(row, spec=spec, gain=gain, bg=bg)
    card_path = Path(cfg.material_card_file)
    if spec.response_override:
        # C3 反推：参数化材料卡在**内存**里生效，磁盘上的原始卡一个字节都不改。
        from .materials import MaterialSpec

        raw = json.loads(card_path.read_text(encoding="utf-8"))
        raw["response"] = {**(raw.get("response") or {}), **dict(spec.response_override)}
        material = MaterialSpec.from_dict(raw)
    else:
        material = load_material_card(card_path)

    res = solve(cfg, material)
    if res.status != "completed" or res.surface is None:
        return {"ok": False, "status": res.status,
                "errors": [e.get("code") for e in (res.errors or [])]}

    drop = res.surface.initial_height - res.surface.height     # 正值 = 去除
    obs = spec.observation
    if obs.kind == "center_window_mean" and obs.center_window_um:
        wy, wx = obs.center_window_um[1], obs.center_window_um[0]
        g = res.surface.grid
        ny, nx = drop.shape
        dy_i = max(1, int(round(wy * 1e-6 / g.dy_m)))
        dx_i = max(1, int(round(wx * 1e-6 / g.dx_m)))
        cy, cx = ny // 2, nx // 2
        y0, y1 = max(0, cy - dy_i // 2), min(ny, cy + dy_i // 2)
        x0, x1 = max(0, cx - dx_i // 2), min(nx, cx + dx_i // 2)
        sub = drop[y0:y1, x0:x1]
    else:
        sub = drop

    import numpy as np

    value = float(np.median(sub)) if obs.statistic == "median" else float(np.mean(sub))
    return {
        "ok": True,
        "status": res.status,
        "predicted_depth_m": value,
        "predicted_depth_um": value * 1e6,
        "max_depth_um": float(np.max(drop)) * 1e6,
        "observation": obs.describe(),
    }


# ---------------------------------------------------------------------------
# 标定：拟合单一正增益 a
# ---------------------------------------------------------------------------


@dataclass
class CalibrationError:
    """一条实验的残差记录。"""

    sample_id: str
    measured_um: float
    predicted_before_um: float | None      # a=1 基线
    predicted_after_um: float | None       # 标定后
    source_row: int = 0

    @property
    def residual_before_um(self) -> float | None:
        if self.predicted_before_um is None:
            return None
        return self.predicted_before_um - self.measured_um

    @property
    def residual_after_um(self) -> float | None:
        if self.predicted_after_um is None:
            return None
        return self.predicted_after_um - self.measured_um


@dataclass
class CalibrationResult:
    """一次标定的结果。**参数真正改变求解过程**，不是事后乘系数。"""

    material_id: str
    gain: float
    baseline_gain: float = 1.0
    train_rows: tuple[str, ...] = ()
    holdout_rows: tuple[str, ...] = ()
    train_errors: tuple[CalibrationError, ...] = ()
    holdout_errors: tuple[CalibrationError, ...] = ()
    n_train: int = 0
    n_holdout: int = 0
    n_skipped: int = 0
    observation: str = ""
    baseline_params: Mapping[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    source_files: tuple[str, ...] = ()
    code_version: str = ""
    hit_bound: bool = False

    # -- 指标 ----------------------------------------------------------------
    @staticmethod
    def _mae(errs: Iterable[CalibrationError], *, after: bool) -> float | None:
        vals = []
        for e in errs:
            r = e.residual_after_um if after else e.residual_before_um
            if r is not None:
                vals.append(abs(r))
        return None if not vals else float(sum(vals) / len(vals))

    @staticmethod
    def _rmse(errs: Iterable[CalibrationError], *, after: bool) -> float | None:
        vals = []
        for e in errs:
            r = e.residual_after_um if after else e.residual_before_um
            if r is not None:
                vals.append(r * r)
        if not vals:
            return None
        return float(math.sqrt(sum(vals) / len(vals)))

    def metrics(self) -> dict[str, Any]:
        return {
            "gain": self.gain,
            "train": {
                "n": self.n_train,
                "mae_before_um": self._mae(self.train_errors, after=False),
                "mae_after_um": self._mae(self.train_errors, after=True),
                "rmse_before_um": self._rmse(self.train_errors, after=False),
                "rmse_after_um": self._rmse(self.train_errors, after=True),
            },
            "holdout": {
                "n": self.n_holdout,
                "mae_before_um": self._mae(self.holdout_errors, after=False),
                "mae_after_um": self._mae(self.holdout_errors, after=True),
                "rmse_before_um": self._rmse(self.holdout_errors, after=False),
                "rmse_after_um": self._rmse(self.holdout_errors, after=True),
            },
        }

    def to_dict(self) -> dict[str, Any]:
        d = {
            "schema": "ufdemo.calibration/1",
            "materialId": self.material_id,
            "gain": self.gain,
            "baselineGain": self.baseline_gain,
            "nTrain": self.n_train,
            "nHoldout": self.n_holdout,
            "nSkipped": self.n_skipped,
            "observation": self.observation,
            "baselineParams": dict(self.baseline_params),
            "metrics": self.metrics(),
            "hitBound": self.hit_bound,
            "notes": list(self.notes),
            "sourceFiles": list(self.source_files),
            "codeVersion": self.code_version,
            "trainRows": list(self.train_rows),
            "holdoutRows": list(self.holdout_rows),
            "trainErrors": [
                {"sampleId": e.sample_id, "measuredUm": e.measured_um,
                 "predictedBeforeUm": e.predicted_before_um,
                 "predictedAfterUm": e.predicted_after_um, "sourceRow": e.source_row}
                for e in self.train_errors
            ],
            "holdoutErrors": [
                {"sampleId": e.sample_id, "measuredUm": e.measured_um,
                 "predictedBeforeUm": e.predicted_before_um,
                 "predictedAfterUm": e.predicted_after_um, "sourceRow": e.source_row}
                for e in self.holdout_errors
            ],
        }
        return d


#: 默认的工程增益边界。**触及边界要报告基线偏差**，不无限放宽。
DEFAULT_GAIN_BOUNDS: tuple[float, float] = (0.05, 20.0)

#: 预设的工程残差尺度（**不冒充已测噪声标准差**）。
DEFAULT_RESIDUAL_SCALE_UM = 5.0


def calibrate_gain(
    train_rows: Sequence[ExperimentRow],
    *,
    spec: PredictionSpec,
    holdout_rows: Sequence[ExperimentRow] = (),
    bounds: tuple[float, float] = DEFAULT_GAIN_BOUNDS,
    residual_scale_um: float = DEFAULT_RESIDUAL_SCALE_UM,
    loss: str = "soft_l1",
    bg: Any | None = None,
    material_id: str = "",
    progress: Callable[[int, float], None] | None = None,
) -> CalibrationResult:
    """拟合单一正增益 ``a``，使预测平均深度贴近实验均值。

    **每次目标函数求值都真的跑求解器** —— 因为 ``a`` 作用在几何更新之前，
    ``D(a)`` 对 ``a`` 是非线性的（有被动离焦时），不能解析地缩放。

    用 SciPy ``least_squares``（有边界 + ``soft_l1``），不自造优化器。
    """
    try:
        import numpy as np
        from scipy.optimize import least_squares
    except ImportError as exc:  # pragma: no cover
        raise UFDemoError(
            CONFIG_INVALID, "标定需要 SciPy",
            field_path="calibration", actual=None, requirement="安装 scipy",
            suggestion="pip install -i https://pypi.tuna.tsinghua.edu.cn/simple scipy",
        ) from exc

    if not train_rows:
        raise UFDemoError(
            CONFIG_INVALID, "没有训练行，无法标定",
            field_path="calibration.train_rows", actual=0, requirement="≥ 1 行",
        )
    bg = bg or load_shared_background()
    notes: list[str] = []

    # ① 基线（a=1）预测 —— 作为"标定前"，也用于诊断
    before: dict[str, float | None] = {}
    for r in list(train_rows) + list(holdout_rows):
        out = predict_mean_depth(r, spec=spec, gain=1.0, bg=bg)
        before[r.sample_id] = out.get("predicted_depth_um") if out.get("ok") else None

    # ② 目标函数：**每次求值都真的重跑求解器**
    calls = {"n": 0}

    def residual(a_vec) -> Any:
        a = float(a_vec[0])
        calls["n"] += 1
        if progress:
            progress(calls["n"], a)
        rs = []
        for r in train_rows:
            out = predict_mean_depth(r, spec=spec, gain=a, bg=bg)
            pred = out.get("predicted_depth_um")
            if pred is None:
                # 该行跑不出结果 → 用一个大残差把它推离，**不静默当 0**
                rs.append(1e6 / max(residual_scale_um, 1e-9))
                continue
            rs.append((pred - r.mean_depth_um) / max(residual_scale_um, 1e-9))
        return np.asarray(rs, dtype=float)

    lo, hi = bounds
    sol = least_squares(
        residual, x0=np.asarray([1.0]), bounds=(lo, hi), loss=loss,
    )
    gain = float(sol.x[0])
    hit_bound = bool(abs(gain - lo) < 1e-9 or abs(gain - hi) < 1e-9)
    if hit_bound:
        notes.append(
            f"增益触及边界（{lo:g}..{hi:g}）：这通常说明**基线本身偏差较大**，"
            "不是靠调增益能修好的。应回对照环节检查阈值/光学，**不**无限放宽边界。"
        )

    # ③ 标定后预测
    after: dict[str, float | None] = {}
    for r in train_rows:
        out = predict_mean_depth(r, spec=spec, gain=gain, bg=bg)
        after[r.sample_id] = out.get("predicted_depth_um") if out.get("ok") else None
    after_hold: dict[str, float | None] = {}
    for r in holdout_rows:
        out = predict_mean_depth(r, spec=spec, gain=gain, bg=bg)
        after_hold[r.sample_id] = out.get("predicted_depth_um") if out.get("ok") else None

    def _errs(rows: Sequence[ExperimentRow], aft: Mapping[str, float | None]) -> tuple[CalibrationError, ...]:
        return tuple(
            CalibrationError(
                sample_id=r.sample_id, measured_um=r.mean_depth_um,
                predicted_before_um=before.get(r.sample_id),
                predicted_after_um=aft.get(r.sample_id),
                source_row=r.source_row,
            ) for r in rows
        )

    n_failed = sum(1 for v in before.values() if v is None)
    if n_failed:
        notes.append(f"{n_failed} 行在基线（a=1）就跑不出结果（已计入残差，未当作 0）。")
    notes.append(
        "增益作用在**每次脉冲的几何更新之前**（solver.response_gain），"
        "后续脉冲按新表面重算被动离焦 → **D(a) ≠ a·D(1)**。"
    )
    notes.append(
        "残差尺度 s_D 是**预设工程尺度的**，不冒充已测噪声标准差。"
    )
    notes.append(
        "二维覆盖与几何均匀性是模型预测；未导入实测高度图时**不声称**完成二维形貌验证。"
    )

    from . import __version__

    return CalibrationResult(
        material_id=material_id or "",
        gain=gain,
        train_rows=tuple(r.sample_id for r in train_rows),
        holdout_rows=tuple(r.sample_id for r in holdout_rows),
        train_errors=_errs(train_rows, after),
        holdout_errors=_errs(holdout_rows, after_hold),
        n_train=len(train_rows),
        n_holdout=len(holdout_rows),
        observation=spec.observation.describe(),
        baseline_params={"note": "基线 = a=1 的求解结果；阈值/尺度来自材料卡"},
        notes=tuple(notes),
        source_files=(spec.material_card_file,),
        code_version=str(__version__),
        hit_bound=hit_bound,
    )


def save_calibration(result: CalibrationResult, path: str | Path) -> Path:
    """保存 ``calibration.json``（任务书 §5.4 的最小落盘）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p


def load_calibration(path: str | Path) -> dict[str, Any]:
    """读回标定对象。**正向计算、规划、回放读同一份**。"""
    p = Path(path)
    if not p.exists():
        raise UFDemoError(
            CONFIG_INVALID, "标定文件不存在",
            field_path="calibration.path", actual=str(p), requirement="文件存在",
        )
    d = json.loads(p.read_text(encoding="utf-8"))
    if d.get("schema") != "ufdemo.calibration/1":
        raise UFDemoError(
            CONFIG_INVALID, "标定文件 schema 不符",
            field_path="calibration.schema", actual=d.get("schema"),
            requirement="ufdemo.calibration/1",
        )
    g = float(d.get("gain", 0.0))
    if not (math.isfinite(g) and g > 0):
        raise UFDemoError(
            CONFIG_INVALID, "标定文件的增益非法",
            field_path="calibration.gain", actual=g, requirement="正有限数",
        )
    return d


# ---------------------------------------------------------------------------
# C3：从实验 CSV 反推基线（Fth、δ）
# ---------------------------------------------------------------------------
#
# 为什么必须**跨频率**才能反推：
# 同一 (脉宽, 频率) 下峰值能流 F 是**固定的**，此时 ln(F/Fth) 只随 Fth 整体平移，
# 与 δ 完全共线 —— 只能定出乘积 δ·ln(F/Fth)，两个参数各自定不出来。
# 只有让 F 跨若干档，Fth 才可辨识。实测验证：单频率组内拟合出的参数
# 在别的频率上系统性偏离；跨 5 档频率则能同时定住两者。
#
# 关键边界（任务书 §4）：
# * 反推出的 Fth/δ 是**有效参数**（engineering-effective），不是独立实测材料常数；
# * **拟合用与检查用的工况必须分开**（按工况组划分），否则是自证；
# * 与 C4 的 a **不同时放开**：这里是先定基线，a 留到标定阶段。

#: 单脉宽反推时的最少工况数（少于这个数不给结论）。
MIN_ROWS_FOR_BASELINE = 6


def select_baseline_window(
    rows: Sequence[ExperimentRow], *, pulse_duration_fs: float,
    drop_negative: bool = True,
) -> tuple[list[ExperimentRow], list[dict[str, Any]]]:
    """挑出**同一脉宽**的工况，作为一次反推的窗口。

    ``drop_negative=True`` 时剔除均值 ≤ 0 的行 —— 纯去除模型无法解释非正去除，
    把它们混进去会**把基线往错误方向拉**（任务书 §5.2：单独报告，不强行拟合）。
    被剔除的行会如实返回，供报告列出。
    """
    kept: list[ExperimentRow] = []
    dropped: list[dict[str, Any]] = []
    for r in rows:
        if abs(r.pulse_duration_fs - float(pulse_duration_fs)) > 1e-9:
            continue
        if drop_negative and r.mean_depth_um <= 0:
            dropped.append({
                "sampleId": r.sample_id, "meanDepthUm": r.mean_depth_um,
                "reason": "均值非正：纯去除模型无法解释，未参与基线反推",
            })
            continue
        kept.append(r)
    return kept, dropped


def _group_split_rows(
    rows: Sequence[ExperimentRow], *, holdout_groups: int, seed: int,
) -> tuple[list[ExperimentRow], list[ExperimentRow], dict[str, Any]]:
    """按**全工况指纹**（含频率）分组划分。

    ⚠️ 与 ``group_split`` 的区别：这里的分组键必须含频率 ——
    跨频率反推时若把同一频率的行拆到两侧，留出集里就没有新的 F 档，
    检查会退化成"同 F 下的插值"，看不出基线是否真的可迁移。
    """
    by: dict[tuple, list[ExperimentRow]] = {}
    for r in rows:
        by.setdefault(r.condition_key, []).append(r)
    keys = sorted(by.keys(), key=lambda k: str(k))
    info: dict[str, Any] = {"n_rows": len(rows), "n_groups": len(keys)}
    if len(keys) <= 1:
        return list(rows), [], {**info, "note": "只有 1 个工况组，无法留出"}
    import random

    rng = random.Random(seed)
    sh = keys[:]
    rng.shuffle(sh)
    n_hold = max(1, min(int(holdout_groups), len(keys) - 1))
    hold_keys = set(sh[:n_hold])
    train = [r for k in keys if k not in hold_keys for r in by[k]]
    hold = [r for k in keys if k in hold_keys for r in by[k]]
    info.update({
        "n_train": len(train), "n_holdout": len(hold),
        "leakage_groups": sorted(str(k) for k in
                                 ({r.condition_key for r in train} & {r.condition_key for r in hold})),
        "seed": seed,
    })
    return train, hold, info


@dataclass
class BaselineEstimate:
    """从实验 CSV 反推出的**有效基线**（engineering-effective）。

    ⚠️ 不是独立实测的材料常数：它来自对**同批实验**的拟合，
    所以只有**留出集**上的误差才说明它能否迁移。
    """

    material_family: str
    pulse_duration_fs: float
    threshold_J_m2: float
    delta_m: float
    n_train: int = 0
    n_holdout: int = 0
    n_dropped: int = 0
    train_mae_um: float | None = None
    train_median_rel: float | None = None
    holdout_mae_um: float | None = None
    holdout_median_rel: float | None = None
    frequency_span_Hz: tuple[float, ...] = ()
    identifiability: Mapping[str, Any] = field(default_factory=dict)
    dropped_rows: tuple[Mapping[str, Any], ...] = ()
    notes: tuple[str, ...] = ()
    source_files: tuple[str, ...] = ()
    code_version: str = ""

    @property
    def threshold_J_cm2(self) -> float:
        return self.threshold_J_m2 / 1e4

    @property
    def delta_nm(self) -> float:
        return self.delta_m * 1e9

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "ufdemo.baseline_estimate/1",
            "materialFamily": self.material_family,
            "pulseDurationFs": self.pulse_duration_fs,
            "thresholdJm2": self.threshold_J_m2,
            "thresholdJcm2": self.threshold_J_cm2,
            "deltaM": self.delta_m,
            "deltaNm": self.delta_nm,
            "nTrain": self.n_train,
            "nHoldout": self.n_holdout,
            "nDropped": self.n_dropped,
            "trainMaeUm": self.train_mae_um,
            "trainMedianRelErr": self.train_median_rel,
            "holdoutMaeUm": self.holdout_mae_um,
            "holdoutMedianRelErr": self.holdout_median_rel,
            "frequencySpanHz": list(self.frequency_span_Hz),
            "identifiability": dict(self.identifiability),
            "droppedRows": [dict(d) for d in self.dropped_rows],
            "notes": list(self.notes),
            "sourceFiles": list(self.source_files),
            "codeVersion": self.code_version,
            "evidenceStatus": "engineering_effective_from_experiment",
        }


def identify_baseline(
    rows: Sequence[ExperimentRow],
    *,
    spec: PredictionSpec,
    material_family: str = "",
    pulse_duration_fs: float | None = None,
    holdout_groups: int = 3,
    seed: int = 20260913,
    threshold_bounds_J_m2: tuple[float, float] = (1e2, 1e7),
    delta_bounds_m: tuple[float, float] = (1e-9, 1e-4),
    residual_scale_um: float = 1.0,
    max_nfev: int = 60,
    bg: Any | None = None,
) -> BaselineEstimate:
    """反推有效阈值 ``Fth`` 与有效去除尺度 ``δ``。

    拟合在 **log 空间**做（深度跨 2–3 个量级，线性空间里小值会被淹没）：
    ``resid = [ln(D_pred) - ln(D_exp)] / s``。

    用 SciPy ``least_squares``（有边界 + ``soft_l1`` 抗离群），不自造优化器。
    """
    try:
        import numpy as np
        from scipy.optimize import least_squares
    except ImportError as exc:  # pragma: no cover
        raise UFDemoError(
            CONFIG_INVALID, "基线反推需要 SciPy",
            field_path="baseline", actual=None, requirement="安装 scipy",
        ) from exc

    if pulse_duration_fs is None:
        cands = sorted({r.pulse_duration_fs for r in rows})
        if not cands:
            raise UFDemoError(
                CONFIG_INVALID, "没有可用工况",
                field_path="baseline.rows", actual=0, requirement="≥ 1 行",
            )
        pulse_duration_fs = cands[0]

    win, dropped = select_baseline_window(rows, pulse_duration_fs=pulse_duration_fs)
    if len(win) < MIN_ROWS_FOR_BASELINE:
        raise UFDemoError(
            CONFIG_INVALID,
            f"脉宽 {pulse_duration_fs:g} fs 的可用工况不足（{len(win)} < {MIN_ROWS_FOR_BASELINE}）",
            field_path="baseline.rows", actual=len(win),
            requirement=f"≥ {MIN_ROWS_FOR_BASELINE} 行",
            suggestion="换一个脉宽，或补数据；**不得**用相近脉宽凑。",
        )

    freqs = tuple(sorted({r.repetition_rate_kHz * 1e3 for r in win}))
    train, hold, split_info = _group_split_rows(win, holdout_groups=holdout_groups, seed=seed)
    bg = bg or load_shared_background()

    # 反推**必须跨频率**：单一 F 档时 Fth 与 δ 共线，定不出两个参数。
    if len({r.repetition_rate_kHz for r in train}) < 2:
        raise UFDemoError(
            CONFIG_INVALID,
            "训练集只有单一重复频率，无法同时定出 Fth 与 δ",
            field_path="baseline.frequency_span",
            actual=sorted({r.repetition_rate_kHz for r in train}),
            requirement="≥ 2 个不同重复频率（否则 ln(F/Fth) 与 δ 共线）",
            suggestion="把该脉宽下的多个频率都放进训练集。",
        )

    def predict_all(fth: float, delta: float, set_rows: Sequence[ExperimentRow]) -> list[float | None]:
        sp = PredictionSpec(
            material_card_file=spec.material_card_file,
            window_um=spec.window_um, dx_um=spec.dx_um,
            observation=spec.observation,
            response_override={
                "kind": "log_fixed",
                "output_semantics": "event_depth_increment",
                "fluence_basis": "incident_peak_fluence",
                "depth_direction": "surface_normal",
                "threshold_J_m2": float(fth),
                "delta_m": float(delta),
            },
        )
        out: list[float | None] = []
        for r in set_rows:
            res = predict_mean_depth(r, spec=sp, gain=1.0, bg=bg)
            out.append(res.get("predicted_depth_um") if res.get("ok") else None)
        return out

    def residual(x: Any) -> Any:
        fth = math.exp(float(x[0]))       # 在 log 空间拟合：两个量都跨若干数量级
        delta = math.exp(float(x[1]))
        preds = predict_all(fth, delta, train)
        rs = []
        for p, r in zip(preds, train):
            if p is None or p <= 0:
                # 预测不出（条件不匹配/阈值以上无去除）→ 给一个**明确的大残差**，
                # 不静默当 0，也不剔除 —— 否则优化器会往"预测全为零"的方向跑。
                rs.append(math.log(1e6))
                continue
            rs.append(math.log(p) - math.log(r.mean_depth_um))
        return np.asarray(rs, dtype=float)

    x0 = np.asarray([math.log(1e5), math.log(3.9e-7)])   # 初值：10 J/cm²、390 nm
    lo = np.asarray([math.log(threshold_bounds_J_m2[0]), math.log(delta_bounds_m[0])])
    hi = np.asarray([math.log(threshold_bounds_J_m2[1]), math.log(delta_bounds_m[1])])
    sol = least_squares(residual, x0=x0, bounds=(lo, hi), loss="soft_l1", max_nfev=max_nfev)
    fth = math.exp(float(sol.x[0]))
    delta = math.exp(float(sol.x[1]))

    def metrics(set_rows: Sequence[ExperimentRow]) -> tuple[float | None, float | None]:
        preds = predict_all(fth, delta, set_rows)
        pairs = [(p, r.mean_depth_um) for p, r in zip(preds, set_rows)
                 if p is not None and r.mean_depth_um > 0]
        if not pairs:
            return None, None
        mae = sum(abs(p - e) for p, e in pairs) / len(pairs)
        rel = sorted(abs(p - e) / e for p, e in pairs)
        med = rel[len(rel) // 2] if len(rel) % 2 else 0.5 * (rel[len(rel) // 2 - 1] + rel[len(rel) // 2])
        return float(mae), float(med)

    tr_mae, tr_rel = metrics(train)
    ho_mae, ho_rel = (metrics(hold) if hold else (None, None))

    # 参数敏感性：报告 d(深度)/d(δ) 与 d/d(Fth) 的相对幅度，说明可辨识性
    base_pred = predict_all(fth, delta, train[:3])
    sens: dict[str, Any] = {}
    for tag, f_mul, d_mul in (("delta_x2", 1.0, 2.0), ("fth_x2", 2.0, 1.0)):
        alt = predict_all(fth * f_mul, delta * d_mul, train[:3])
        diffs = [abs((b or 0) - (a or 0)) / (abs(b) + 1e-12)
                 for a, b in zip(alt, base_pred) if b]
        sens[tag] = float(sum(diffs) / len(diffs)) if diffs else None
    sens["note"] = "相对幅度越大越可辨识；两者接近说明参数耦合。"

    notes = [
        "反推值是**工程有效参数**，不是独立实测的材料常数。",
        "只有**留出集**误差才说明能否迁移；训练集误差是拟合优度。",
        f"训练集覆盖 {len({r.repetition_rate_kHz for r in train})} 个频率档"
        f"（全部 {len(freqs)} 档）—— 跨频率才能解耦 Fth 与 δ。",
        "与 C4 的标定增益 a **不同时放开**（两者都控幅度，会互相抵消）。",
    ]
    if dropped:
        notes.append(f"{len(dropped)} 行因均值非正被排除，已列出（不参与拟合）。")
    if not hold:
        notes.append("未能划分留出集：本结论**不构成**可迁移性证据。")

    from . import __version__

    return BaselineEstimate(
        material_family=material_family,
        pulse_duration_fs=float(pulse_duration_fs),
        threshold_J_m2=fth,
        delta_m=delta,
        n_train=len(train),
        n_holdout=len(hold),
        n_dropped=len(dropped),
        train_mae_um=tr_mae, train_median_rel=tr_rel,
        holdout_mae_um=ho_mae, holdout_median_rel=ho_rel,
        frequency_span_Hz=freqs,
        identifiability={**sens, "split": split_info},
        dropped_rows=tuple(dropped),
        notes=tuple(notes),
        source_files=(spec.material_card_file,),
        code_version=str(__version__),
    )


def save_baseline(est: BaselineEstimate, path: str | Path) -> Path:
    """落盘基线估计（**不覆写任何原始材料卡**）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(est.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def baseline_to_card_patch(est: BaselineEstimate) -> dict[str, Any]:
    """把基线估计转成可以合并进材料卡的 ``response`` patch。

    ⚠️ 这只是**候选**：``evidence_status`` 必须由人工确认后才能升级，
    本函数不替人做这个决定，因此返回的 patch 里显式标注来源。
    """
    return {
        "kind": "log_fixed_effective",
        "output_semantics": "event_depth_increment",
        "fluence_basis": "incident_peak_fluence",
        "depth_direction": "surface_normal",
        "threshold_J_m2": est.threshold_J_m2,
        "threshold_kind": "engineering_effective_from_experiment",
        "delta_m": est.delta_m,
        "_provenance": {
            "method": "identify_baseline_from_experiment_csv",
            "pulse_duration_fs": est.pulse_duration_fs,
            "n_train": est.n_train,
            "n_holdout": est.n_holdout,
            "holdout_median_rel_err": est.holdout_median_rel,
            "note": "工程有效参数（对同批实验拟合所得），非独立实测常数；"
                    "不可直接当作材料卡已标定，需人工确认后才可升级证据状态。",
        },
    }
