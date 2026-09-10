"""执行配置、单位系统与跨字段校验（执行细则第 4 章、5.1 节）。

本模块是唯一把 JSON 变成可信对象的入口。三条硬规则：

1. 物理模式内部 SI；合成模式内部统一无量纲（``x/L_ref``、``h/L_ref``、
   ``w/L_ref``、``zR/L_ref``、``F/F_ref``，能量为 ``E/(F_ref*L_ref^2)``）。
2. ``unknown`` 表示条件尚未确认，``null`` 表示数据缺失；两者都不代表 0。
3. 能量与功率同时给出时必须做一致性检查；``zR`` 与 ``M2`` 同时给出时同理。
   任何冲突都报错，绝不静默取一个。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .errors import (
    CONDITION_MISMATCH,
    CONFIG_INVALID,
    ENERGY_CONFLICT,
    GEOMETRY_UNSUPPORTED,
    MATERIAL_CAPABILITY_MISSING,
    NOT_IMPLEMENTED,
    UFDemoError,
)

# ---------------------------------------------------------------------------
# 0. 常量与枚举
# ---------------------------------------------------------------------------

J_CM2_TO_J_M2 = 1.0e4
UM = 1.0e-6
NM = 1.0e-9
FS = 1.0e-15

SCHEMA_VERSION = "1.0"

RUN_MODES: tuple[str, ...] = (
    "reference_case",
    "threshold_only",
    "synthetic_demo",
    "calibrated_case",
)

# 执行细则 2.1：执行层统一使用这五种证据状态（不使用任务书示例里的 synthetic）
EVIDENCE_STATUSES: tuple[str, ...] = (
    "unverified",
    "literature_reported",
    "formula_checked",
    "experiment_reproduced",
    "independently_validated",
)

SOURCE_TYPES: tuple[str, ...] = (
    "analytic_test_definition",
    "synthetic_definition",
    "research_card_migration",
    "user_supplied_table",
)

UNIT_SYSTEMS: tuple[str, ...] = ("SI", "dimensionless")

# 响应语义（任务书 3.2 / 细则 4.3）。只有第一个可以直接加到表面。
SEMANTIC_EVENT_INCREMENT = "event_depth_increment"
SEMANTIC_MEAN_RATE = "mean_depth_per_effective_pulse"
SEMANTIC_CUMULATIVE = "cumulative_depth"
SEMANTIC_TRACK_PASS = "track_or_pass_depth"
SEMANTIC_VOLUME_PER_ENERGY = "volume_per_energy"
SEMANTIC_THRESHOLD_ONLY = "threshold_only"

ALL_SEMANTICS: tuple[str, ...] = (
    SEMANTIC_EVENT_INCREMENT,
    SEMANTIC_MEAN_RATE,
    SEMANTIC_CUMULATIVE,
    SEMANTIC_TRACK_PASS,
    SEMANTIC_VOLUME_PER_ENERGY,
    SEMANTIC_THRESHOLD_ONLY,
)

# 只有该语义允许进入逐事件增量主循环（细则 4.3 / 7 节）
INCREMENT_SEMANTICS: tuple[str, ...] = (SEMANTIC_EVENT_INCREMENT,)

# 几何反馈开关（细则 2.3）：前者用于 G01/G02，后者单独验证
GEOMETRY_FEEDBACK_MODES: tuple[str, ...] = ("fixed_geometry", "axial_defocus")

# 单位系统内部尺度名
DIMENSIONLESS_SCALES = ("L_ref_m", "F_ref_J_m2", "delta_ref_m")


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _require_finite_positive(value: Any, path: str, *, requirement: str = "必须是有限正值") -> float:
    if not _is_finite_number(value):
        raise UFDemoError(
            CONFIG_INVALID,
            f"{path} 不是有限数值",
            field_path=path,
            actual=value,
            requirement=requirement,
            suggestion="改为有限正数；缺失数据用 null，未知条件用 unknown 字符串。",
        )
    value = float(value)
    if value <= 0.0:
        raise UFDemoError(
            CONFIG_INVALID,
            f"{path} 必须为正",
            field_path=path,
            actual=value,
            requirement=requirement,
            suggestion="改为有限正数。",
        )
    return value


def _require_finite(value: Any, path: str, *, allow_zero: bool = True) -> float:
    if not _is_finite_number(value):
        raise UFDemoError(
            CONFIG_INVALID,
            f"{path} 不是有限数值",
            field_path=path,
            actual=value,
            requirement="必须是有限数值（禁止 NaN/Inf/字符串）",
            suggestion="提供有限数值；缺失用 null。",
        )
    value = float(value)
    if not allow_zero and value == 0.0:
        raise UFDemoError(
            CONFIG_INVALID,
            f"{path} 不得为零",
            field_path=path,
            actual=value,
            requirement="非零有限数值",
            suggestion="给出非零值。",
        )
    return value


def _require_str(value: Any, path: str, allowed: Sequence[str] | None = None) -> str:
    if not isinstance(value, str):
        raise UFDemoError(
            CONFIG_INVALID,
            f"{path} 必须是字符串",
            field_path=path,
            actual=value,
            requirement="字符串" + (f"，取值属于 {list(allowed)}" if allowed else ""),
        )
    if allowed is not None and value not in allowed:
        raise UFDemoError(
            CONFIG_INVALID,
            f"{path} 取值非法",
            field_path=path,
            actual=value,
            requirement=f"取值属于 {list(allowed)}",
            suggestion="改用允许值之一。",
        )
    return value


# ---------------------------------------------------------------------------
# 1. 单位上下文
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitContext:
    """把配置物理量换算到内部计算量的换算层。

    - ``SI``：内部即 SI，换算因子全部为 1。
    - ``dimensionless``：内部为归一化量，长度除以 ``L_ref``，能流除以 ``F_ref``，
      能量除以 ``F_ref*L_ref^2``。
    """

    mode: str
    L_ref_m: float | None = None
    F_ref_J_m2: float | None = None
    delta_ref_m: float | None = None
    E_ref_J: float | None = None

    @staticmethod
    def from_config(raw: Mapping[str, Any]) -> "UnitContext":
        mode = _require_str(raw.get("unit_system", "SI"), "unit_system", UNIT_SYSTEMS)
        if mode == "SI":
            return UnitContext(mode="SI")
        scales = raw.get("reference_scales")
        if not isinstance(scales, Mapping):
            raise UFDemoError(
                CONFIG_INVALID,
                "合成模式缺少 reference_scales",
                field_path="reference_scales",
                actual=scales,
                requirement="dimensionless 模式必须显式给出 L_ref_m、F_ref_J_m2、delta_ref_m",
                suggestion="补 reference_scales 块，或改用 unit_system=SI。",
            )
        L = _require_finite_positive(scales.get("L_ref_m"), "reference_scales.L_ref_m")
        F = _require_finite_positive(scales.get("F_ref_J_m2"), "reference_scales.F_ref_J_m2")
        d = _require_finite_positive(scales.get("delta_ref_m"), "reference_scales.delta_ref_m")
        return UnitContext(mode="dimensionless", L_ref_m=L, F_ref_J_m2=F, delta_ref_m=d, E_ref_J=F * L * L)

    # --- 换算 ---------------------------------------------------------------
    def length_to_internal(self, value: float) -> float:
        return value if self.mode == "SI" else value / self.L_ref_m  # type: ignore[operator]

    def fluence_to_internal(self, value: float) -> float:
        return value if self.mode == "SI" else value / self.F_ref_J_m2  # type: ignore[operator]

    def energy_to_internal(self, value: float) -> float:
        return value if self.mode == "SI" else value / self.E_ref_J  # type: ignore[operator]

    def length_from_internal(self, value: float) -> float:
        return value if self.mode == "SI" else value * self.L_ref_m  # type: ignore[operator]

    def fluence_from_internal(self, value: float) -> float:
        return value if self.mode == "SI" else value * self.F_ref_J_m2  # type: ignore[operator]

    def energy_from_internal(self, value: float) -> float:
        return value if self.mode == "SI" else value * self.E_ref_J  # type: ignore[operator]

    # --- 标签（细则 4.1：合成模式禁止自动导出 depth_um）---------------------
    @property
    def length_label(self) -> str:
        return "m" if self.mode == "SI" else "L_ref"

    @property
    def fluence_label(self) -> str:
        return "J/m^2" if self.mode == "SI" else "F_ref"

    @property
    def energy_label(self) -> str:
        return "J" if self.mode == "SI" else "F_ref*L_ref^2"

    @property
    def depth_label(self) -> str:
        """深度标签。

        合成模式下深度是直接从 ``h/L_ref`` 主减得的高度差，与平面几何、光斑、
        离焦同处**同一个**无量纲长度尺度（任务书「合成模式的单位规则」：参与
        几何计算的长度必须采用同一尺度 ``L_ref``），所以标签是 ``L_ref`` 而非
        ``delta_ref``：把 ``10 L_ref`` 写成 ``10 delta_ref`` 会与几何量差 100 倍。
        ``delta_ref_m`` 仍保存在 ``reference_scales`` 中，需要 ``d/delta_ref``
        显示时按 ``delta_ref_m / L_ref_m`` 换算，换算关系不丢失。
        """
        return "m" if self.mode == "SI" else "L_ref"

    @property
    def allows_physical_depth_export(self) -> bool:
        """合成模式禁止导出 ``depth_um``（细则 4.1）。"""
        return self.mode == "SI"

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_system": self.mode,
            "L_ref_m": self.L_ref_m,
            "F_ref_J_m2": self.F_ref_J_m2,
            "delta_ref_m": self.delta_ref_m,
            "E_ref_J": self.E_ref_J,
            "labels": {
                "length": self.length_label,
                "fluence": self.fluence_label,
                "energy": self.energy_label,
                "depth": self.depth_label,
            },
            "physical_depth_export_allowed": self.allows_physical_depth_export,
        }


# ---------------------------------------------------------------------------
# 2. 子配置
# ---------------------------------------------------------------------------


@dataclass
class GridConfig:
    """单元中心规则网格。数组形状固定 ``(ny, nx)``：第 0 维 y，第 1 维 x。"""

    nx: int
    ny: int
    dx_m: float
    dy_m: float
    center_x_m: float
    center_y_m: float
    initial_height_m: float = 0.0
    initial_surface: str = "flat"
    dtype_field: str = "float64"
    dtype_phase: str = "uint16"
    dtype_count: str = "uint32"

    AXIS_ORDER = ("y", "x")  # 细则 4.1

    @staticmethod
    def from_dict(raw: Mapping[str, Any], unit: UnitContext) -> "GridConfig":
        nx = raw.get("nx")
        ny = raw.get("ny")
        for name, value in (("nx", nx), ("ny", ny)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 3:
                raise UFDemoError(
                    CONFIG_INVALID,
                    f"grid.{name} 必须是 ≥3 的整数",
                    field_path=f"grid.{name}",
                    actual=value,
                    requirement="整数，且 ≥3（单元中心网格至少 3 个采样才能做中心差分）",
                )
        dx = _require_finite_positive(raw.get("dx_m"), "grid.dx_m", requirement="有限正间距")
        dy = _require_finite_positive(raw.get("dy_m"), "grid.dy_m", requirement="有限正间距")
        origin = raw.get("origin", "cell_center")
        if origin != "cell_center":
            raise UFDemoError(
                CONFIG_INVALID,
                "grid.origin 仅支持 cell_center",
                field_path="grid.origin",
                actual=origin,
                requirement="cell_center",
                suggestion="细则 4.1 固定使用单元中心规则网格。",
            )
        cx = _require_finite(raw.get("center_x_m", 0.0), "grid.center_x_m")
        cy = _require_finite(raw.get("center_y_m", 0.0), "grid.center_y_m")
        h0 = _require_finite(raw.get("initial_height_m", 0.0), "grid.initial_height_m")
        return GridConfig(
            nx=nx,
            ny=ny,
            dx_m=unit.length_to_internal(dx),
            dy_m=unit.length_to_internal(dy),
            center_x_m=unit.length_to_internal(cx),
            center_y_m=unit.length_to_internal(cy),
            initial_height_m=unit.length_to_internal(h0),
            initial_surface=str(raw.get("initial_surface", "flat")),
        )

    def axis(self, which: str):
        import numpy as np

        if which == "x":
            n, c, d = self.nx, self.center_x_m, self.dx_m
        elif which == "y":
            n, c, d = self.ny, self.center_y_m, self.dy_m
        else:  # pragma: no cover
            raise ValueError(which)
        idx = np.arange(n, dtype=np.float64) - (n - 1) / 2.0
        return c + idx * d

    @property
    def domain_area_internal(self) -> float:
        return self.nx * self.dx_m * self.ny * self.dy_m

    @property
    def cell_count(self) -> int:
        return self.nx * self.ny

    def to_dict(self) -> dict[str, Any]:
        return {
            "nx": self.nx,
            "ny": self.ny,
            "dx_m": self.dx_m,
            "dy_m": self.dy_m,
            "center_x_m": self.center_x_m,
            "center_y_m": self.center_y_m,
            "initial_height_m": self.initial_height_m,
            "initial_surface": self.initial_surface,
            "origin": "cell_center",
            "axis_order": list(self.AXIS_ORDER),
            "dtype_field": self.dtype_field,
            "dtype_phase": self.dtype_phase,
            "dtype_count": self.dtype_count,
            "domain_area": self.domain_area_internal,
        }


@dataclass
class LaserConfig:
    wavelength_m: float | None
    pulse_duration_s: float | None
    pulse_energy_J: float
    spot_radius_m: float
    focus_xyz_m: tuple[float, float, float]
    direction_unit: tuple[float, float, float]
    repetition_rate_Hz: float | None = None
    average_power_W: float | None = None
    rayleigh_range_m: float | None = None
    m2: float | None = None
    power_measurement_location: str | None = None
    parameter_sources: tuple[str, ...] = ()
    direction_unit_input: tuple[float, float, float] | None = None
    direction_renormalized: bool = False
    condition_unit_labels: Mapping[str, str] = field(default_factory=dict)

    ENERGY_REL_TOL = 1e-6  # 细则 4.2：初始工程容差，相对 1e-6

    @staticmethod
    def from_dict(raw: Mapping[str, Any], unit: UnitContext) -> "LaserConfig":
        if not isinstance(raw, Mapping):
            raise UFDemoError(CONFIG_INVALID, "laser 必须是对象", field_path="laser", actual=raw)

        wl = raw.get("wavelength_m", "unknown")
        wavelength = None
        if wl not in (None, "unknown"):
            wavelength = unit.length_to_internal(_require_finite_positive(wl, "laser.wavelength_m"))

        pd = raw.get("pulse_duration_s", "unknown")
        pulse_duration = None
        if pd not in (None, "unknown"):
            pulse_duration = _require_finite_positive(pd, "laser.pulse_duration_s")

        energy = raw.get("pulse_energy_J", None)
        power = raw.get("average_power_W", None)
        f_rep = raw.get("repetition_rate_Hz", None)

        if energy is None and power is None:
            raise UFDemoError(
                CONFIG_INVALID,
                "必须给出 pulse_energy_J 或 average_power_W + repetition_rate_Hz",
                field_path="laser",
                actual=None,
                requirement="二者之一",
                suggestion="补 pulse_energy_J，或同时补 average_power_W 与 repetition_rate_Hz。",
            )

        if energy is not None and power is not None:
            # 一致性检查，不允许静默取一个
            if f_rep is None:
                raise UFDemoError(
                    ENERGY_CONFLICT,
                    "同时给出能量与功率，但缺少 repetition_rate_Hz，无法核对一致性",
                    field_path="laser.repetition_rate_Hz",
                    actual=None,
                    requirement="同时给出能量、功率与频率，或只给出其中之一",
                    suggestion="补 repetition_rate_Hz，或删去其中一个能量输入。",
                )
            f_rep_v = _require_finite_positive(f_rep, "laser.repetition_rate_Hz")
            e_from_power = float(power) / f_rep_v
            rel = abs(float(energy) - e_from_power) / max(abs(float(energy)), abs(e_from_power))
            if rel > LaserConfig.ENERGY_REL_TOL:
                raise UFDemoError(
                    ENERGY_CONFLICT,
                    "pulse_energy_J 与 average_power_W/repetition_rate_Hz 不一致",
                    field_path="laser.pulse_energy_J",
                    actual={"pulse_energy_J": energy, "average_power_W": power, "repetition_rate_Hz": f_rep_v, "implied_pulse_energy_J": e_from_power, "relative_difference": rel},
                    requirement=f"相对差 ≤ {LaserConfig.ENERGY_REL_TOL:g}",
                    suggestion="核对功率测量位置（样品面或激光器出口）后只保留一致的数值。",
                )

        if energy is not None:
            energy_internal = unit.energy_to_internal(_require_finite_positive(energy, "laser.pulse_energy_J"))
            power_v = unit.energy_to_internal(float(power)) * 1.0 if power is not None else None
            freq_v = _require_finite_positive(f_rep, "laser.repetition_rate_Hz") if f_rep is not None else None
        else:
            freq_v = _require_finite_positive(f_rep, "laser.repetition_rate_Hz")
            p_internal = unit.energy_to_internal(_require_finite_positive(power, "laser.average_power_W"))
            energy_internal = p_internal / freq_v
            power_v = p_internal

        w0 = unit.length_to_internal(_require_finite_positive(raw.get("spot_radius_m"), "laser.spot_radius_m"))

        focus = raw.get("focus_xyz_m", [0.0, 0.0, 0.0])
        if not isinstance(focus, Sequence) or len(focus) != 3:
            raise UFDemoError(CONFIG_INVALID, "laser.focus_xyz_m 必须是长度 3 的数组", field_path="laser.focus_xyz_m", actual=focus)
        focus_internal = tuple(unit.length_to_internal(_require_finite(v, f"laser.focus_xyz_m[{i}]")) for i, v in enumerate(focus))

        direction = raw.get("direction_unit", [0.0, 0.0, 1.0])
        if not isinstance(direction, Sequence) or len(direction) != 3:
            raise UFDemoError(CONFIG_INVALID, "laser.direction_unit 必须是长度 3 的数组", field_path="laser.direction_unit", actual=direction)
        dvals = tuple(_require_finite(v, f"laser.direction_unit[{i}]") for i, v in enumerate(direction))
        norm = math.sqrt(sum(v * v for v in dvals))
        if norm == 0.0 or not math.isfinite(norm):
            raise UFDemoError(
                CONFIG_INVALID,
                "laser.direction_unit 必须非零",
                field_path="laser.direction_unit",
                actual=direction,
                requirement="非零有限向量",
            )
        direction_unit = tuple(v / norm for v in dvals)
        direction_renormalized = abs(norm - 1.0) > 1e-12

        zr = raw.get("rayleigh_range_m", None)
        m2 = raw.get("m2", None)
        zr_internal = None
        if zr is not None:
            zr_internal = unit.length_to_internal(_require_finite_positive(zr, "laser.rayleigh_range_m"))
        m2_v = None
        if m2 is not None:
            m2_v = _require_finite_positive(m2, "laser.m2")
        if zr_internal is not None and m2_v is not None:
            # 细则 4.2：zR 与 M2 同时输入时做一致性检查（不用其中一个覆盖另一个）
            if wavelength is None:
                raise UFDemoError(
                    CONFIG_INVALID,
                    "同时给出 rayleigh_range_m 与 m2，但缺少 wavelength_m，无法核对一致性",
                    field_path="laser.wavelength_m",
                    actual=None,
                    requirement="同时给出 zR、M2、波长，或只给出其中之一",
                    suggestion="补 wavelength_m，或删去其中一个输入。",
                )
            zr_expected = math.pi * (w0 ** 2) * m2_v / wavelength
            rel = abs(zr_internal - zr_expected) / max(abs(zr_internal), abs(zr_expected))
            if rel > 1e-6:
                raise UFDemoError(
                    CONFIG_INVALID,
                    "rayleigh_range_m 与 m2 不一致",
                    field_path="laser.rayleigh_range_m",
                    actual={"rayleigh_range_m": zr_internal, "m2": m2_v,
                            "implied_rayleigh_range_m": zr_expected, "relative_difference": rel},
                    requirement="zR = pi*w0^2*M2/lambda，相对差 ≤ 1e-6",
                    suggestion="核对高斯束约定后只保留一致的数值。",
                )

        if raw.get("repetition_rate_Hz", None) is None and energy is not None:
            freq_v = None

        return LaserConfig(
            wavelength_m=wavelength,
            pulse_duration_s=pulse_duration,
            pulse_energy_J=energy_internal,
            spot_radius_m=w0,
            focus_xyz_m=focus_internal,  # type: ignore[arg-type]
            direction_unit=direction_unit,  # type: ignore[arg-type]
            repetition_rate_Hz=freq_v,
            average_power_W=power_v,
            rayleigh_range_m=zr_internal,
            m2=m2_v,
            power_measurement_location=raw.get("power_measurement_location"),
            parameter_sources=tuple(raw.get("parameter_sources", ()) or ()),
            direction_unit_input=dvals,
            direction_renormalized=direction_renormalized,
            condition_unit_labels={
                "wavelength": unit.length_label,
                "pulse_duration": "s",
                "spot_radius": unit.length_label,
            },
        )

    @property
    def peak_fluence_internal(self) -> float:
        """平面 1/e² 高斯在焦点的峰值能流 ``2E/(pi w0^2)``。"""
        return 2.0 * self.pulse_energy_J / (math.pi * self.spot_radius_m ** 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wavelength_m": self.wavelength_m,
            "pulse_duration_s": self.pulse_duration_s,
            "pulse_energy_J": self.pulse_energy_J,
            "spot_radius_m": self.spot_radius_m,
            "focus_xyz_m": list(self.focus_xyz_m),
            "direction_unit": list(self.direction_unit),
            "repetition_rate_Hz": self.repetition_rate_Hz,
            "average_power_W": self.average_power_W,
            "rayleigh_range_m": self.rayleigh_range_m,
            "m2": self.m2,
            "power_measurement_location": self.power_measurement_location,
            "parameter_sources": list(self.parameter_sources),
            "direction_unit_input": list(self.direction_unit_input) if self.direction_unit_input else None,
            "direction_unit_was_renormalized": self.direction_renormalized,
        }


# 轨道段
@dataclass
class PathSegment:
    segment_id: int
    pass_id: int
    start_s: float
    end_s: float
    start_xyz_m: tuple[float, float, float]
    end_xyz_m: tuple[float, float, float]
    speed_m_s: float | None = None
    laser_on: bool = True
    label: str = ""

    def position_at(self, t: float, tol: float) -> tuple[float, float, float]:
        span = self.end_s - self.start_s
        if span <= 0.0:
            return self.start_xyz_m
        frac = (t - self.start_s) / span
        frac = min(max(frac, 0.0), 1.0)
        return tuple(a + (b - a) * frac for a, b in zip(self.start_xyz_m, self.end_xyz_m))  # type: ignore[return-value]

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "pass_id": self.pass_id,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "start_xyz_m": list(self.start_xyz_m),
            "end_xyz_m": list(self.end_xyz_m),
            "speed_m_s": self.speed_m_s,
            "laser_on": self.laser_on,
            "label": self.label,
        }


@dataclass
class PathConfig:
    segments: list[PathSegment]
    t0_s: float = 0.0
    time_tolerance_s: float = 1e-12
    transition_rule: str = "left_closed_right_open"

    @staticmethod
    def from_dict(raw: Mapping[str, Any], laser: LaserConfig, unit: UnitContext) -> "PathConfig":
        segs_raw = raw.get("segments", [])
        if not isinstance(segs_raw, Sequence):
            raise UFDemoError(CONFIG_INVALID, "path.segments 必须是数组", field_path="path.segments", actual=segs_raw)
        if len(segs_raw) == 0:
            # 明确允许空路径，但由 validate 阶段给出 ZERO_EVENTS 语义处理
            return PathConfig(segments=[], t0_s=float(raw.get("t0_s", 0.0)))

        if laser.repetition_rate_Hz is None:
            raise UFDemoError(
                CONFIG_INVALID,
                "路径事件流需要 repetition_rate_Hz",
                field_path="laser.repetition_rate_Hz",
                actual=None,
                requirement="有限正频率",
                suggestion="补 repetition_rate_Hz（统一时钟 t_j = t0 + j/f）。",
            )

        segs: list[PathSegment] = []
        for i, s in enumerate(segs_raw):
            if not isinstance(s, Mapping):
                raise UFDemoError(CONFIG_INVALID, f"path.segments[{i}] 必须是对象", field_path=f"path.segments[{i}]", actual=s)
            p = f"path.segments[{i}]"
            t_start = _require_finite(s.get("start_s"), f"{p}.start_s")
            t_end = _require_finite(s.get("end_s"), f"{p}.end_s")
            if t_end < t_start:
                raise UFDemoError(
                    CONFIG_INVALID,
                    f"{p}.end_s 小于 start_s",
                    field_path=f"{p}.end_s",
                    actual=t_end,
                    requirement="end_s ≥ start_s",
                )
            a = s.get("start_xyz_m", None)
            b = s.get("end_xyz_m", None)
            if a is None or b is None:
                raise UFDemoError(CONFIG_INVALID, f"{p} 需要 start_xyz_m 与 end_xyz_m", field_path=p, actual=s)
            start_xyz = tuple(unit.length_to_internal(_require_finite(v, f"{p}.start_xyz_m[{k}]")) for k, v in enumerate(a))
            end_xyz = tuple(unit.length_to_internal(_require_finite(v, f"{p}.end_xyz_m[{k}]")) for k, v in enumerate(b))
            speed = s.get("speed_m_s", None)
            speed_v = None
            if speed is not None:
                speed_v = abs(unit.length_to_internal(_require_finite(speed, f"{p}.speed_m_s")))
            segs.append(
                PathSegment(
                    segment_id=int(s.get("segment_id", i)),
                    pass_id=int(s.get("pass_id", 0)),
                    start_s=t_start,
                    end_s=t_end,
                    start_xyz_m=start_xyz,  # type: ignore[arg-type]
                    end_xyz_m=end_xyz,  # type: ignore[arg-type]
                    speed_m_s=speed_v,
                    laser_on=bool(s.get("laser_on", True)),
                    label=str(s.get("label", "")),
                )
            )
        return PathConfig(
            segments=segs,
            t0_s=float(raw.get("t0_s", 0.0)),
            time_tolerance_s=float(raw.get("time_tolerance_s", 1e-12)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "t0_s": self.t0_s,
            "time_tolerance_s": self.time_tolerance_s,
            "transition_rule": self.transition_rule,
            "segments": [s.to_dict() for s in self.segments],
        }


@dataclass
class SolverConfig:
    mode: str = "reference"
    geometry_feedback: str = "fixed_geometry"
    history_enabled: bool = False
    tail_epsilon: float = 1e-8
    memory_budget_bytes: int = 2 * 1024 ** 3
    budget_safety_factor: float = 1.5
    cancel_check_interval: int = 256
    # M3 组合默认关闭（细则 8 节末）
    multiline_incubation: bool = False
    structured_interface: bool = False
    # 动态角度属批次 J（T18）；此处提前占位，用于与分相截断做互斥校验
    dynamic_angle: bool = False
    acceleration: str = "off"
    extra: Mapping[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "SolverConfig":
        mode = _require_str(raw.get("mode", "reference"), "solver.mode", ("reference", "grouped"))
        gf = _require_str(raw.get("geometry_feedback", "fixed_geometry"), "solver.geometry_feedback", GEOMETRY_FEEDBACK_MODES)
        eps = _require_finite_positive(raw.get("tail_epsilon", 1e-8), "solver.tail_epsilon")
        if not (0.0 < eps < 1.0):
            raise UFDemoError(
                CONFIG_INVALID,
                "solver.tail_epsilon 必须落在 (0,1)",
                field_path="solver.tail_epsilon",
                actual=eps,
                requirement="0 < epsilon < 1（默认 1e-8，是数值设置，不是物理损伤阈值）",
            )
        budget = raw.get("memory_budget_bytes", 2 * 1024 ** 3)
        if not isinstance(budget, int) or budget <= 0:
            raise UFDemoError(CONFIG_INVALID, "solver.memory_budget_bytes 必须是正整数", field_path="solver.memory_budget_bytes", actual=budget)
        accel = _require_str(raw.get("acceleration", "off"), "solver.acceleration", ("off", "numba"))
        return SolverConfig(
            mode=mode,
            geometry_feedback=gf,
            history_enabled=bool(raw.get("history_enabled", False)),
            tail_epsilon=eps,
            memory_budget_bytes=budget,
            budget_safety_factor=float(raw.get("budget_safety_factor", 1.5)),
            cancel_check_interval=int(raw.get("cancel_check_interval", 256)),
            multiline_incubation=bool(raw.get("multiline_incubation", False)),
            structured_interface=bool(raw.get("structured_interface", False)),
            dynamic_angle=bool(raw.get("dynamic_angle", False)),
            acceleration=accel,
            extra=dict(raw.get("extra", {}) or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "geometry_feedback": self.geometry_feedback,
            "history_enabled": self.history_enabled,
            "tail_epsilon": self.tail_epsilon,
            "memory_budget_bytes": self.memory_budget_bytes,
            "budget_safety_factor": self.budget_safety_factor,
            "cancel_check_interval": self.cancel_check_interval,
            "multiline_incubation": self.multiline_incubation,
            "structured_interface": self.structured_interface,
            "dynamic_angle": self.dynamic_angle,
            "acceleration": self.acceleration,
        }


@dataclass
class OutputConfig:
    output_dir: str | None = None
    snapshot_policy: str = "events"
    snapshot_events: tuple[int, ...] = ()
    snapshot_every_n_passes: int | None = None
    max_snapshots: int = 8
    max_snapshot_bytes: int = 256 * 1024 ** 2
    events_csv_max_rows: int = 200_000
    cross_section: Mapping[str, Any] | None = None
    roi: tuple[Mapping[str, Any], ...] = ()

    @staticmethod
    def from_dict(raw: Mapping[str, Any] | None) -> "OutputConfig":
        raw = raw or {}
        policy = _require_str(raw.get("snapshot_policy", "events"), "output.snapshot_policy", ("events", "passes", "none"))
        events = tuple(int(v) for v in (raw.get("snapshot_events") or ()))
        return OutputConfig(
            output_dir=raw.get("output_dir"),
            snapshot_policy=policy,
            snapshot_events=events,
            snapshot_every_n_passes=raw.get("snapshot_every_n_passes"),
            max_snapshots=int(raw.get("max_snapshots", 8)),
            max_snapshot_bytes=int(raw.get("max_snapshot_bytes", 256 * 1024 ** 2)),
            events_csv_max_rows=int(raw.get("events_csv_max_rows", 200_000)),
            cross_section=raw.get("cross_section"),
            roi=tuple(raw.get("roi") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_dir": self.output_dir,
            "snapshot_policy": self.snapshot_policy,
            "snapshot_events": list(self.snapshot_events),
            "snapshot_every_n_passes": self.snapshot_every_n_passes,
            "max_snapshots": self.max_snapshots,
            "max_snapshot_bytes": self.max_snapshot_bytes,
            "events_csv_max_rows": self.events_csv_max_rows,
            "cross_section": self.cross_section,
            "roi": list(self.roi),
        }


# ---------------------------------------------------------------------------
# 2c. StructureConfig（批次 G / T11–T13）
# ---------------------------------------------------------------------------

# 与 ``structure.STRUCTURE_TYPES`` 同义；此处复制常量是为了不让 config 反向依赖
# 几何模块（几何模块在方法内部才导入 response，链条仍保持单向）。
STRUCTURE_TYPES: tuple[str, ...] = (
    "homogeneous",
    "particle_composite",
    "laminated_fiber_composite",
)

# 合成结构的相响应能力名（与 materials.CAP_SYNTHETIC_STRUCTURE 同值）。
_CAP_SYNTHETIC_STRUCTURE = "synthetic_structure"


@dataclass
class StructureConfig:
    """合成复合材料的相结构配置。

    默认 ``structure_type="homogeneous"``，即沿用 M0 的单相路径。其它类型必须
    显式开启 ``solver.structured_interface``，两者不一致时在配置层拒绝——
    不允许"配了结构但被静默忽略"，也不允许"开了开关却没有结构"。
    """

    structure_type: str = "homogeneous"
    seed: int = 0
    phases: tuple[Mapping[str, Any], ...] = ()
    layers: tuple[Mapping[str, Any], ...] = ()
    particles: Mapping[str, Any] | None = None
    target_volume_fraction: float | None = None

    @property
    def enabled(self) -> bool:
        return self.structure_type != "homogeneous"

    @staticmethod
    def from_dict(raw: Mapping[str, Any] | None) -> "StructureConfig":
        raw = raw or {}
        if not isinstance(raw, Mapping):
            raise UFDemoError(CONFIG_INVALID, "structure 必须是 JSON 对象", field_path="structure", actual=type(raw).__name__)
        stype = _require_str(raw.get("structure_type", "homogeneous"), "structure.structure_type", STRUCTURE_TYPES)
        seed = raw.get("seed", 0)
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise UFDemoError(
                CONFIG_INVALID,
                "structure.seed 必须是非负整数",
                field_path="structure.seed",
                actual=seed,
                suggestion="固定种子才能复现几何；不写时默认 0。",
            )
        tvf = raw.get("target_volume_fraction")
        if tvf is not None and (not isinstance(tvf, (int, float)) or isinstance(tvf, bool) or not (0.0 < float(tvf) < 1.0)):
            raise UFDemoError(
                CONFIG_INVALID,
                "structure.target_volume_fraction 必须落在 (0,1)",
                field_path="structure.target_volume_fraction",
                actual=tvf,
                requirement="0 < Vf < 1",
                suggestion="目标比例只是生成参数；有限随机样本不必然等于它，实际值另报。",
            )
        return StructureConfig(
            structure_type=stype,
            seed=seed,
            phases=tuple(raw.get("phases") or ()),
            layers=tuple(raw.get("layers") or ()),
            particles=raw.get("particles"),
            target_volume_fraction=(None if tvf is None else float(tvf)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "structure_type": self.structure_type,
            "seed": self.seed,
            "phases": [dict(p) for p in self.phases],
            "layers": [dict(layer) for layer in self.layers],
            "particles": None if self.particles is None else dict(self.particles),
            "target_volume_fraction": self.target_volume_fraction,
        }


# ---------------------------------------------------------------------------
# 3. RunConfig
# ---------------------------------------------------------------------------


@dataclass
class RunConfig:
    schema_version: str
    run_mode: str
    unit: UnitContext
    material_id: str
    grid: GridConfig
    laser: LaserConfig
    path: PathConfig
    solver: SolverConfig
    output: OutputConfig
    structure: StructureConfig = field(default_factory=StructureConfig)
    seed: int = 0
    material_card_file: str | None = None
    label: str = ""
    reference_conditions: Mapping[str, Any] | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(raw: Mapping[str, Any], *, base_dir: str | None = None) -> "RunConfig":
        from pathlib import Path as _P

        if not isinstance(raw, Mapping):
            raise UFDemoError(CONFIG_INVALID, "顶层配置必须是 JSON 对象", field_path="$", actual=type(raw).__name__)
        sv = _require_str(raw.get("schema_version", SCHEMA_VERSION), "schema_version")
        if sv != SCHEMA_VERSION:
            raise UFDemoError(
                CONFIG_INVALID,
                "schema_version 不受支持",
                field_path="schema_version",
                actual=sv,
                requirement=f"当前支持 {SCHEMA_VERSION}",
            )
        run_mode = _require_str(raw.get("run_mode"), "run_mode", RUN_MODES)
        unit = UnitContext.from_config(raw)
        material_id = _require_str(raw.get("material_id"), "material_id")
        card_file = raw.get("material_card_file")
        if card_file is not None and base_dir is not None:
            cand = _P(base_dir) / card_file
            if not _P(card_file).is_absolute() and cand.exists():
                card_file = str(cand.resolve())
        grid = GridConfig.from_dict(raw.get("grid") or {}, unit)
        laser = LaserConfig.from_dict(raw.get("laser") or {}, unit)
        path = PathConfig.from_dict(raw.get("path") or {}, laser, unit)
        solver = SolverConfig.from_dict(raw.get("solver") or {})
        output = OutputConfig.from_dict(raw.get("output"))
        structure = StructureConfig.from_dict(raw.get("structure"))
        seed = raw.get("seed", 0)
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise UFDemoError(CONFIG_INVALID, "seed 必须是非负整数", field_path="seed", actual=seed)
        return RunConfig(
            schema_version=sv,
            run_mode=run_mode,
            unit=unit,
            material_id=material_id,
            grid=grid,
            laser=laser,
            path=path,
            solver=solver,
            output=output,
            structure=structure,
            seed=seed,
            material_card_file=card_file,
            label=str(raw.get("label", "")),
            reference_conditions=raw.get("reference_conditions"),
            raw=dict(raw),
        )

    def to_dict(self, *, include_internal: bool = False) -> dict[str, Any]:
        """``include_internal=False`` 时回放原始输入（快照用），否则给出内部换算值。"""
        if not include_internal and self.raw:
            return dict(self.raw)
        return {
            "schema_version": self.schema_version,
            "run_mode": self.run_mode,
            "material_id": self.material_id,
            "material_card_file": self.material_card_file,
            "seed": self.seed,
            "label": self.label,
            "unit": self.unit.to_dict(),
            "grid": self.grid.to_dict(),
            "laser": self.laser.to_dict(),
            "path": self.path.to_dict(),
            "solver": self.solver.to_dict(),
            "output": self.output.to_dict(),
            "structure": self.structure.to_dict(),
        }

    @property
    def is_analytic_fixture(self) -> bool:
        return self.run_mode in ("reference_case", "threshold_only") and self.unit.mode == "SI"


@dataclass
class ValidationReport:
    ok: bool
    run_mode: str
    unit_system: str
    material_id: str
    evidence_status: str | None
    allowed_run_modes: tuple[str, ...]
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    estimated_events: int | None = None
    estimated_memory_bytes: int | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "run_mode": self.run_mode,
            "unit_system": self.unit_system,
            "material_id": self.material_id,
            "evidence_status": self.evidence_status,
            "allowed_run_modes": list(self.allowed_run_modes),
            "notes": list(self.notes),
            "warnings": list(self.warnings),
            "estimated_events": self.estimated_events,
            "estimated_memory_bytes": self.estimated_memory_bytes,
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# 4. 准入校验
# ---------------------------------------------------------------------------


def estimate_events(path: PathConfig, laser: LaserConfig) -> int:
    """按统一时钟估算事件数（不构造事件列表）。"""
    if laser.repetition_rate_Hz is None or not path.segments:
        return 0
    f = laser.repetition_rate_Hz
    t0 = path.t0_s
    if t0 < 0:
        t0 = 0.0
    start_index = int(math.ceil(t0 * f - 1e-9))
    t_end_max = max(s.end_s for s in path.segments)
    if t_end_max <= t0:
        return 0
    last_index = int(math.floor(t_end_max * f - 1e-12))
    n_clock = max(0, last_index - start_index + 1)
    # 只有落在出光段内的事件才真正出光
    tol = path.time_tolerance_s
    active = 0
    on_segments = [s for s in path.segments if s.laser_on and s.end_s > s.start_s]
    if not on_segments:
        return 0
    # 逐段计数，避免全量扫描
    for s in on_segments:
        a = int(math.ceil(max(s.start_s, start_index / f) * f - tol * f))
        b = int(math.floor(min(s.end_s, (last_index + 1) / f) * f - tol * f))
        a = max(a, start_index)
        b = min(b, last_index)
        if b >= a:
            active += b - a + 1
    # 去掉重叠段重复计数（区间左闭右开，正常不重叠）
    del n_clock
    return active


def estimate_memory_bytes(grid: GridConfig, n_events: int, solver: SolverConfig) -> dict[str, int]:
    """按细则 5.1 用实际计划数组的形状和 dtype 估算，并计入临时 patch 与快照。"""
    import numpy as np

    n = grid.cell_count
    f64 = np.dtype(np.float64).itemsize
    u16 = np.dtype(np.uint16).itemsize
    u32 = np.dtype(np.uint32).itemsize
    u8 = np.dtype(np.bool_).itemsize

    persistent = {
        "height": n * f64,
        "initial_height": n * f64,
        "phase_id": n * u16,
        "cumulative_fluence": n * f64,
        "exposure_count": n * u32,
        "illumination_count": n * u32,
        "warning_mask": n * u8,
    }
    # 临时 patch：按 tail_epsilon 的裁剪半径上界（3.035w）估计局部窗口
    # 这里用全网格上界的保守估计，实际由 beam_patch 决定
    transient = {
        "fluence_patch": n * f64,
        "candidate_increment": n * f64,
        "applied_increment": n * f64,
        "patch_mask": n * u8,
    }
    snapshot_bytes = 0
    if solver.extra.get("snapshot_policy", "events") != "none":
        max_snaps = int(solver.extra.get("max_snapshots", 8))
        snapshot_bytes = max_snaps * (n * f64 + n * u16 + n * u32)
    total = sum(persistent.values()) + sum(transient.values()) + snapshot_bytes
    total = int(total * solver.budget_safety_factor)
    return {**persistent, **transient, "snapshots": snapshot_bytes, "total_with_safety_factor": total}


def check_reference_conditions(config: RunConfig, material: Any) -> list[UFDemoError]:
    """严格参考模式的条件与协议匹配（细则 2.2、4.4 第 5 项）。

    超出或无法确认关键条件时**拒绝**定量执行；不允许校验失败后自动切换模式。
    人工解析 fixture（``fixture_only``）跳过本检查，因为其条件由解析定义给出。
    """
    errs: list[UFDemoError] = []
    proto = dict(getattr(material, "reference_protocol", {}) or {})
    if config.run_mode != "reference_case" or not proto:
        return errs
    if getattr(material, "fixture_only", False):
        return errs

    given = dict(config.reference_conditions or {})
    mid = getattr(material, "id", "?")

    # 默认未启用的候选值（例如脉宽未核实的金刚石 2.32 J/cm²）不得在参考模式启用
    if getattr(material, "enabled_by_default", True) is False:
        errs.append(
            UFDemoError(
                CONDITION_MISMATCH,
                f"材料卡 {mid} 的候选值默认禁用：{getattr(material, 'blocked_reason', None) or '关键条件未核实'}",
                field_path="material_id",
                actual=mid,
                requirement="候选值的完整条件核实后才设为默认",
                suggestion="补全脉宽等条件并单独建卡，不要把它直接设成默认。",
            )
        )
        return errs

    pid = proto.get("protocol_id")
    if pid and given.get("protocol_id") != pid:
        errs.append(
            UFDemoError(
                CONDITION_MISMATCH,
                f"参考协议不匹配（材料卡 {mid} 要求 protocol_id={pid!r}）",
                field_path="reference_conditions.protocol_id",
                actual=given.get("protocol_id"),
                requirement=f"protocol_id == {pid!r}",
                suggestion="选择卡中已确认的协议，或改用 synthetic_demo 并显式标注非物理预测。",
            )
        )

    fb = dict(getattr(material, "response", {}) or {}).get("fluence_basis")
    gfb = given.get("fluence_basis")
    if fb and gfb is not None and gfb != fb:
        errs.append(
            UFDemoError(
                CONDITION_MISMATCH,
                "能流基准不匹配",
                field_path="reference_conditions.fluence_basis",
                actual=gfb,
                requirement=f"fluence_basis == {fb!r}",
                suggestion="阈值已按入射峰值标定时不得再乘一次吸收率，也不能把吸收能流当入射能流。",
            )
        )
    if fb and gfb is None and proto.get("require_fluence_basis_match", True):
        errs.append(
            UFDemoError(
                CONDITION_MISMATCH,
                "未确认能流基准，无法判定阈值与求解器是否一致",
                field_path="reference_conditions.fluence_basis",
                actual=None,
                requirement=f"必须显式声明 {fb!r}",
                suggestion="补 reference_conditions.fluence_basis。",
            )
        )

    rh = dict(proto.get("required_history", {}) or {})
    need_n = rh.get("effective_count")
    if need_n is not None:
        got_n = given.get("effective_count")
        if got_n is None:
            errs.append(
                UFDemoError(
                    CONDITION_MISMATCH,
                    "未确认有效脉冲数定义，拒绝定量执行",
                    field_path="reference_conditions.effective_count",
                    actual=None,
                    requirement=f"必须显式给出该协议的有效脉冲数（卡内为 {need_n}）",
                    suggestion="补 effective_count；不得把 N=10 阈值当作单脉冲阈值使用。",
                )
            )
        elif abs(float(got_n) - float(need_n)) > 1e-9:
            errs.append(
                UFDemoError(
                    CONDITION_MISMATCH,
                    "有效脉冲数与材料卡协议不一致",
                    field_path="reference_conditions.effective_count",
                    actual=got_n,
                    requirement=f"== {need_n}（卡内该阈值的定义条件）",
                    suggestion="该阈值的 N 定义不可替换；单脉冲阈值与 N 脉冲阈值是不同类型。",
                )
            )

    mapping = {
        "wavelength_m": config.laser.wavelength_m,
        "pulse_duration_s": config.laser.pulse_duration_s,
        "repetition_rate_Hz": config.laser.repetition_rate_Hz,
        "spot_radius_m": config.laser.spot_radius_m,
    }
    zh = {
        "wavelength_m": "波长",
        "pulse_duration_s": "脉宽",
        "repetition_rate_Hz": "重复频率",
        "spot_radius_m": "1/e² 光斑半径",
    }
    for key, spec in (proto.get("required_laser", {}) or {}).items():
        value = spec.get("value") if isinstance(spec, Mapping) else spec
        tol = float(spec.get("rel_tol", 1e-6)) if isinstance(spec, Mapping) else 1e-6
        actual = mapping.get(key)
        if value is None:
            continue
        if actual is None:
            errs.append(
                UFDemoError(
                    CONDITION_MISMATCH,
                    f"激光{zh.get(key, key)}未确认，参考模式拒绝定量执行",
                    field_path=f"laser.{key}",
                    actual=None,
                    requirement=f"该协议要求 {value}（相对容差 {tol:g}）",
                    suggestion="补齐条件，或改用显式 synthetic_demo；禁止校验失败后自动切换模式。",
                )
            )
        elif abs(float(actual) - float(value)) / abs(float(value)) > tol:
            errs.append(
                UFDemoError(
                    CONDITION_MISMATCH,
                    f"激光{zh.get(key, key)}与材料卡协议不匹配",
                    field_path=f"laser.{key}",
                    actual=float(actual),
                    requirement=f"{value}（相对容差 {tol:g}）",
                    suggestion="核对参数来源；超出有效窗口时不得继续作为 reference_case 输出。",
                )
            )
    return errs


def validate_run(config: RunConfig, material: Any) -> ValidationReport:
    """执行前的准入校验（细则 5.1 前两步 + 4.2 契约）。"""
    notes: list[str] = []
    warnings: list[str] = []
    errors: list[dict[str, Any]] = []

    allowed = tuple(getattr(material, "allowed_run_modes", ()) or ())
    evidence = getattr(material, "evidence_status", None)

    def fail(err: UFDemoError) -> None:
        errors.append(err.to_dict())

    # 1. 模式准入
    if allowed and config.run_mode not in allowed:
        fail(
            UFDemoError(
                MATERIAL_CAPABILITY_MISSING,
                f"材料卡 {config.material_id} 不允许 run_mode={config.run_mode}",
                field_path="run_mode",
                actual=config.run_mode,
                requirement=f"取值属于 {list(allowed)}",
                suggestion="改用允许的模式，或先把材料卡升级到该模式。",
            )
        )

    if config.run_mode == "calibrated_case":
        # 细则 2.1：必须关联独立验证报告、有效条件和批准使用的卡版本
        missing = []
        if not getattr(material, "independent_validation_report", None):
            missing.append("independent_validation_report")
        if not getattr(material, "approved_card_version", None):
            missing.append("approved_card_version")
        if missing:
            fail(
                UFDemoError(
                    CONDITION_MISMATCH,
                    "calibrated_case 缺少独立验证关联",
                    field_path="run_mode",
                    actual={"missing": missing},
                    requirement="必须有关联的独立验证报告与已批准卡版本",
                    suggestion="先完成独立验证并登记卡版本；仅填写完整数值不开放该模式。",
                )
            )

    # 2. 证据状态合法性
    if evidence is not None and evidence not in EVIDENCE_STATUSES:
        fail(
            UFDemoError(
                CONFIG_INVALID,
                "材料卡 evidence_status 非法",
                field_path="material.evidence_status",
                actual=evidence,
                requirement=f"取值属于 {list(EVIDENCE_STATUSES)}",
                suggestion="细则 2.1：执行层统一使用五种证据状态，不用 synthetic。",
            )
        )

    # 3. 严格参考模式的条件与协议匹配（细则 2.2）
    for e in check_reference_conditions(config, material):
        fail(e)
    if config.run_mode == "reference_case":
        notes.append("reference_case：输出物理单位，但一直显示验证状态；软件跑通不提升实验验证状态。")

    # 4. 合成模式的显式选择与无损转换
    if config.unit.mode == "dimensionless":
        if config.run_mode != "synthetic_demo":
            fail(
                UFDemoError(
                    CONFIG_INVALID,
                    "无量纲单位只允许 synthetic_demo",
                    field_path="run_mode",
                    actual=config.run_mode,
                    requirement="unit_system=dimensionless ⇒ run_mode=synthetic_demo",
                    suggestion="合成演示必须由用户显式选择，其配置与参考配置分别保存。",
                )
            )
        else:
            notes.append("合成演示：所有长度按 L_ref 归一、能流按 F_ref 归一，禁止导出物理 μm 深度。")
            # 合成模式下「只有一个长度尺度」：卡里的 J/m^2 与 m 是物理量，
            # 直接当成 F_ref / L_ref 单位会差出多个数量级（本工程默认 100 倍以上）。
            # 因此除了分相结构（响应完全来自 structure.phases 的内联定义）之外，
            # 无量纲配置只能配已归一化的合成卡。
            raw_resp = dict((getattr(material, "raw", {}) or {}).get("response", {}) or {})
            si_fields = [k for k in ("threshold_J_m2", "delta_m") if raw_resp.get(k) is not None]
            if si_fields:
                if not config.solver.structured_interface:
                    fail(
                        UFDemoError(
                            CONFIG_INVALID,
                            "无量纲配置不能直接把 SI 材料卡的响应当内部量使用",
                            field_path="material_card_file",
                            actual={"card": getattr(material, "id", None), "si_fields": si_fields},
                            requirement=(
                                "合成模式必须使用已按 F_ref / L_ref 归一化的响应"
                                "（threshold_over_F_ref / delta_over_L_ref），"
                                "或改为分相结构并从 structure.phases 内联给出各相响应"
                            ),
                            suggestion=(
                                "卡中的阈值单位是 J/m^2、去除尺度单位是 m；"
                                "把它们当作 F_ref / L_ref 单位的内部量会整体差若干数量级。"
                                "请改用合成卡，或开启 solver.structured_interface 并把响应写进 phases。"
                            ),
                        )
                    )
                else:
                    notes.append(
                        "分相结构：逐事件响应取自 structure.phases 的内联合成定义；"
                        f"材料卡仅提供身份与能力，其 SI 响应字段 {si_fields} 不参与计算（各相阈值单独标定）。"
                    )
            if config.material_card_file is None:
                warnings.append("合成演示未提供 material_card_file，将只使用配置内置响应。")
    else:
        if config.run_mode == "synthetic_demo":
            warnings.append("synthetic_demo 使用 SI 单位时仍然只是流程演示，不构成材料物理预测。")

    # 5. 阈值展示模式的语义标记
    if config.run_mode == "threshold_only":
        notes.append("threshold_only：removal_available=false，体积与深度统计写 null，界面显示“不提供”。")

    # 6. 来源类型
    st = getattr(material, "source_type", None)
    if st is not None and st not in SOURCE_TYPES:
        fail(
            UFDemoError(
                CONFIG_INVALID,
                "材料卡 source_type 非法",
                field_path="material.source_type",
                actual=st,
                requirement=f"取值属于 {list(SOURCE_TYPES)}",
            )
        )

    # 7. 路径与事件
    n_events = estimate_events(config.path, config.laser)
    if n_events == 0:
        notes.append("零事件运行：允许，结果为零去除，并明确标注 zero_events。")
    if config.path.segments:
        no_speed: list[int] = []
        for s in config.path.segments:
            moving = (s.end_s - s.start_s) > 0 and s.start_xyz_m != s.end_xyz_m
            if moving and s.speed_m_s is not None:
                span = s.end_s - s.start_s
                dist = math.dist(s.start_xyz_m, s.end_xyz_m)
                implied = dist / span
                if implied > 0 and abs(s.speed_m_s - implied) / implied > 1e-6:
                    warnings.append(
                        f"段 {s.segment_id} 的 speed_m_s={s.speed_m_s:.6g} 与段长/时长推得的 "
                        f"{implied:.6g} 不一致；位置实际按起止点线性插值。"
                    )
            if s.laser_on and s.speed_m_s not in (None, 0.0) and not moving and (s.end_s - s.start_s) > 0:
                warnings.append(f"段 {s.segment_id} 出光但起止点相同（定点驻留），确认是否为本意。")
            if s.speed_m_s is None and moving:
                no_speed.append(s.segment_id)
        if no_speed:
            shown = no_speed[:5]
            suffix = f" 等 {len(no_speed)} 段" if len(no_speed) > 5 else ""
            warnings.append(f"段 {shown}{suffix} 未给 speed_m_s；位置由起止点线性插值得到。")

    # 8. 几何组合准入（细则 2.3、8 节末：M0 只开放正入射）
    kz = abs(config.laser.direction_unit[2])
    kx = abs(config.laser.direction_unit[0])
    ky = abs(config.laser.direction_unit[1])
    if not (kz > 1.0 - 1e-12 and kx < 1e-12 and ky < 1e-12):
        fail(
            UFDemoError(
                GEOMETRY_UNSUPPORTED,
                "斜入射在 M0 未开放",
                field_path="laser.direction_unit",
                actual=list(config.laser.direction_unit),
                requirement="|k_z|=1 且 k_x=k_y=0（M0 默认正入射）",
                suggestion="改用正入射。斜入射、法向厚度转换与遮挡属 M3（T18），且未实现前不允许提供任意曲面开关。",
            )
        )
    if config.solver.acceleration != "off":
        fail(
            UFDemoError(
                NOT_IMPLEMENTED,
                "批量加速模式尚未实现",
                field_path="solver.acceleration",
                actual=config.solver.acceleration,
                requirement="本批（A–C / M0）只提供逐脉冲参考模式",
                suggestion="设 solver.acceleration=off；分组模式属于批次 I（T17）。",
            )
        )
    if config.solver.multiline_incubation:
        fail(
            UFDemoError(
                NOT_IMPLEMENTED,
                "多层跨相孵化尚未实现",
                field_path="solver.multiline_incubation",
                actual=True,
                requirement="M0 不启用复合材料跨层孵化（细则 5.2 末）",
                suggestion="设 false；属批次 G/H。",
            )
        )
    if config.solver.multiline_incubation:
        fail(
            UFDemoError(
                NOT_IMPLEMENTED,
                "多层跨相孵化尚未实现",
                field_path="solver.multiline_incubation",
                actual=True,
                requirement="M0 不启用复合材料跨层孵化（细则 5.2 末）",
                suggestion="设 false；属批次 H。",
            )
        )

    # 8b. 结构化相界面（批次 G / T11–T13）：一致性 + 互斥 + 结构 schema
    st = getattr(config, "structure", None)
    structure_enabled = bool(st is not None and st.enabled)
    if structure_enabled and not config.solver.structured_interface:
        fail(
            UFDemoError(
                CONFIG_INVALID,
                "配置了相结构，但 solver.structured_interface=false",
                field_path="solver.structured_interface",
                actual=False,
                requirement="带 structure 的配置必须显式开启 solver.structured_interface",
                suggestion=(
                    "设 solver.structured_interface=true（软件不会静默忽略已配置的相结构），"
                    "或把 structure.structure_type 改回 homogeneous。"
                ),
            )
        )
    if config.solver.structured_interface and not structure_enabled:
        fail(
            UFDemoError(
                CONFIG_INVALID,
                "solver.structured_interface=true，但未提供相结构",
                field_path="structure",
                actual=None,
                requirement="需要 structure.structure_type ∈ particle_composite / laminated_fiber_composite",
                suggestion="补齐 structure 块，或设 solver.structured_interface=false。",
            )
        )
    if config.solver.structured_interface and config.solver.dynamic_angle:
        fail(
            UFDemoError(
                CONFIG_INVALID,
                "分相截断与动态角度不得同时启用",
                field_path="solver",
                actual={"structured_interface": True, "dynamic_angle": True},
                requirement="二者互斥（执行细则 8 节末）",
                suggestion=(
                    "法向去除与垂直相列路径混用会让几何意义不唯一；"
                    "先关闭其一。扩展时必须单独定义几何意义并增加测试。"
                ),
            )
        )
    if config.solver.dynamic_angle:
        fail(
            UFDemoError(
                NOT_IMPLEMENTED,
                "动态角度尚未实现",
                field_path="solver.dynamic_angle",
                actual=True,
                requirement="动态角度属批次 J（T18）",
                suggestion="设 solver.dynamic_angle=false。",
            )
        )
    if structure_enabled:
        from .structure import check_structure_config

        struct_errors, struct_notes, struct_warnings = check_structure_config(config, material)
        for e in struct_errors:
            fail(e)
        notes.extend(struct_notes)
        warnings.extend(struct_warnings)

    # 9. 资源预算
    solver_for_budget = SolverConfig(**{**config.solver.__dict__, "extra": {"snapshot_policy": config.output.snapshot_policy, "max_snapshots": config.output.max_snapshots}})
    mem = estimate_memory_bytes(config.grid, n_events, solver_for_budget)
    if mem["total_with_safety_factor"] > config.solver.memory_budget_bytes:
        fail(
            UFDemoError(
                RESOURCE_BUDGET_EXCEEDED,
                "预估内存超出预算",
                field_path="solver.memory_budget_bytes",
                actual={"estimated_bytes": mem["total_with_safety_factor"], "budget_bytes": config.solver.memory_budget_bytes},
                requirement="预估内存（含 1.5 倍预留）≤ 预算",
                suggestion="增大 memory_budget_bytes、缩小网格/区间，或关闭快照；禁止静默变粗网格。",
            )
        )

    # 10. 材料能力（深度请求）
    caps = getattr(material, "capabilities", {}) or {}

    def _cap_available(name: str) -> tuple[bool, str]:
        c = caps.get(name)
        if c is None:
            return False, "能力未登记"
        if isinstance(c, Mapping):
            return bool(c.get("available", False)), str(c.get("reason", "未提供原因"))
        return bool(getattr(c, "available", False)), str(getattr(c, "reason", "未提供原因"))

    if config.run_mode in ("reference_case", "synthetic_demo"):
        ok_cap, cap_reason = _cap_available(SEMANTIC_EVENT_INCREMENT)
        if not ok_cap:
            if structure_enabled:
                # 合成结构模式：逐事件响应由**分相内联定义**提供，父卡只提供结构身份
                # 与资料来源。这与细则 7 节「铝基 SiC 只开放无量纲合成颗粒」一致。
                ok_struct, struct_reason = _cap_available(_CAP_SYNTHETIC_STRUCTURE)
                if not ok_struct:
                    fail(
                        UFDemoError(
                            MATERIAL_CAPABILITY_MISSING,
                            f"材料卡不开放合成结构：{struct_reason}",
                            field_path="material_id",
                            actual=config.material_id,
                            requirement="需要 synthetic_structure 能力（或父卡自带逐事件去除核）",
                            suggestion="改用开放合成结构的材料卡，或补齐父卡的逐事件核。",
                        )
                    )
                else:
                    notes.append(
                        "合成结构模式：逐事件去除核由分相内联定义提供（父卡仅提供结构身份）；"
                        f"父卡逐事件核不可用：{cap_reason}"
                    )
            else:
                fail(
                    UFDemoError(
                        MATERIAL_CAPABILITY_MISSING,
                        f"材料卡不支持逐事件去除增量：{cap_reason}",
                        field_path="material_id",
                        actual=config.material_id,
                        requirement="需要 event_depth_increment 能力（完整阈值 + 去除尺度 + 匹配条件）",
                        suggestion="补齐 δ 与阈值并确认条件；不得用相近材料、不同脉宽或纳秒数据补成“完整参数”。",
                    )
                )

    return ValidationReport(
        ok=not errors,
        run_mode=config.run_mode,
        unit_system=config.unit.mode,
        material_id=config.material_id,
        evidence_status=evidence,
        allowed_run_modes=allowed,
        notes=notes,
        warnings=warnings,
        estimated_events=n_events,
        estimated_memory_bytes=mem["total_with_safety_factor"],
        errors=errors,
    )



def load_config(path: str) -> RunConfig:
    """读取并校验一个配置 JSON 文件。"""
    import json
    from pathlib import Path as _P

    p = _P(path)
    if not p.exists():
        raise UFDemoError(
            CONFIG_INVALID,
            "配置文件不存在",
            field_path="<file>",
            actual=str(p),
            suggestion="核对路径。",
        )
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UFDemoError(
            CONFIG_INVALID,
            "配置 JSON 解析失败",
            field_path="<file>",
            actual=str(exc),
            suggestion="修正 JSON 语法。",
        ) from exc
    cfg = RunConfig.from_dict(raw, base_dir=str(p.parent))
    # 相对材料卡路径：先按配置文件所在目录解析，再回退到工程根目录
    if cfg.material_card_file is not None:
        from pathlib import Path as _P2

        from . import project_root as _root

        cf = _P2(cfg.material_card_file)
        if not cf.is_absolute():
            for candidate in (p.parent / cf, _root() / cf):
                if candidate.exists():
                    cfg.material_card_file = str(candidate.resolve())
                    break
    return cfg
