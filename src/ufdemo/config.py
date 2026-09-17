"""执行配置、单位系统与跨字段校验（执行细则第 4 章、5.1 节）。

本模块是唯一把 JSON 变成可信对象的入口。三条硬规则：

1. 物理模式内部 SI；合成模式内部统一无量纲（``x/L_ref``、``h/L_ref``、
   ``w/L_ref``、``zR/L_ref``、``F/F_ref``，能量为 ``E/(F_ref*L_ref^2)``）。
2. ``unknown`` 表示条件尚未确认，``null`` 表示数据缺失；两者都不代表 0。
3. 能量与功率同时给出时必须做一致性检查；``zR`` 与 ``M2`` 同时给出时同理。
   任何冲突都报错，绝不静默取一个。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import (
    CONDITION_MISMATCH,
    CONFIG_INVALID,
    ENERGY_CONFLICT,
    GEOMETRY_UNSUPPORTED,
    MATERIAL_CAPABILITY_MISSING,
    NOT_IMPLEMENTED,
    RESOURCE_BUDGET_EXCEEDED,
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
    # U07：**实测来源**。此前枚举里只有「解析定义 / 合成 / 迁移 / 用户表」，
    # 结果真实实验数据在曲线卡层**无处安放** —— 曲线目录里只能有 fixture，
    # 求解器也就永远吃不到真实数据。这两个值补上那条通路。
    "published_measurement",     # 已发表论文中明确给出的实测数值
    "digitized_measurement",     # 从实验图人工数字化读取（容差另行标注）
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

# ⚠️ **本枚举不扩容**（细则 4.3）：它是**曲线 / 材料响应**的语义集合，
# `tables.CURVE_ROUTES` 必须对其中每一项登记去向，`assert_increment_semantics`
# 也以它为准。**实测数据的端点语义不属于这个枚举** —— 那是另一个概念轴
# （观测语义，见 `datasets.OBSERVATION_SEMANTICS`），
# 只在「数据集权限」层使用，**既不进曲线枚举，也永不进事件核**。

# 只有该语义允许进入逐事件增量主循环（细则 4.3 / 7 节）
INCREMENT_SEMANTICS: tuple[str, ...] = (SEMANTIC_EVENT_INCREMENT,)

# 几何反馈开关（细则 2.3）：前者用于 G01/G02，后者单独验证
GEOMETRY_FEEDBACK_MODES: tuple[str, ...] = ("fixed_geometry", "axial_defocus")

# 批次 J（T18）：斜入射/动态角度的软件支持范围。**单一权威来源**在 geometry.py，
# 这里只做转出，避免两处各写一份而漂移（细则 9.2：「是软件数值/展示范围，
# 不是七类材料的物理边界」）。
def _supported_geometry_range() -> tuple[float, float]:
    from .geometry import MAX_INCIDENCE_DEG, MIN_NZ

    return float(MIN_NZ), float(MAX_INCIDENCE_DEG)


MIN_SUPPORTED_NZ, MAX_SUPPORTED_INCIDENCE_DEG = _supported_geometry_range()

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


def _require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise UFDemoError(CONFIG_INVALID, f"{path} 必须是布尔值", field_path=path, actual=value, requirement="true 或 false")
    return value


def _require_positive_int(value: Any, path: str) -> int:
    """正整数校验（批量/限额类配置用）。布尔值被显式拒绝（bool 是 int 的子类）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise UFDemoError(
            CONFIG_INVALID,
            f"{path} 必须是正整数",
            field_path=path,
            actual=value,
            requirement=">= 1 的整数",
        )
    return int(value)


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
    # 批次 J（T18）：倾斜初始平面。斜率是**无量纲**的 (h_x, h_y)，
    # 用于构造解析斜平面并验证 Δh=-a_n/n_z 与法向解析值。
    initial_slope_x: float = 0.0
    initial_slope_y: float = 0.0
    dtype_field: str = "float64"
    dtype_phase: str = "uint16"
    dtype_count: str = "uint32"

    AXIS_ORDER = ("y", "x")  # 细则 4.1

    INITIAL_SURFACES: tuple[str, ...] = ("flat", "tilted_plane")

    @property
    def initial_slope(self) -> tuple[float, float]:
        return (float(self.initial_slope_x), float(self.initial_slope_y))

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
        surf = _require_str(
            raw.get("initial_surface", "flat"), "grid.initial_surface", GridConfig.INITIAL_SURFACES
        )
        sx = _require_finite(raw.get("initial_slope_x", 0.0), "grid.initial_slope_x")
        sy = _require_finite(raw.get("initial_slope_y", 0.0), "grid.initial_slope_y")
        if surf == "flat" and (sx != 0.0 or sy != 0.0):
            raise UFDemoError(
                CONFIG_INVALID,
                "grid.initial_surface=flat 时不得给非零斜率",
                field_path="grid.initial_slope_x",
                actual={"slope_x": sx, "slope_y": sy},
                requirement="flat ⇒ slope=(0,0)",
                suggestion="改用 initial_surface=tilted_plane，或把斜率置零。",
            )
        if surf == "tilted_plane":
            if sx == 0.0 and sy == 0.0:
                raise UFDemoError(
                    CONFIG_INVALID,
                    "grid.initial_surface=tilted_plane 需要非零斜率",
                    field_path="grid.initial_slope_x",
                    actual={"slope_x": sx, "slope_y": sy},
                    requirement="tilted_plane ⇒ slope≠(0,0)",
                    suggestion="给出 initial_slope_x / initial_slope_y（无量纲 h_x、h_y）。",
                )
            # 与本工程支持范围一致：n_z = 1/sqrt(1+sx²+sy²) >= 0.5（约 60° 倾角）
            nz = 1.0 / math.sqrt(1.0 + sx * sx + sy * sy)
            if nz < MIN_SUPPORTED_NZ - 1e-12:
                raise UFDemoError(
                    GEOMETRY_UNSUPPORTED,
                    "初始平面倾角超出软件支持范围",
                    field_path="grid.initial_slope_x",
                    actual={"slope_x": sx, "slope_y": sy, "n_z": nz},
                    requirement=f"n_z >= {MIN_SUPPORTED_NZ:g}",
                    suggestion="减小斜率；超范围时停止该模式，不裁剪角度继续。",
                )
        return GridConfig(
            nx=nx,
            ny=ny,
            dx_m=unit.length_to_internal(dx),
            dy_m=unit.length_to_internal(dy),
            center_x_m=unit.length_to_internal(cx),
            center_y_m=unit.length_to_internal(cy),
            initial_height_m=unit.length_to_internal(h0),
            initial_surface=surf,
            initial_slope_x=sx,
            initial_slope_y=sy,
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
            "initial_slope_x": self.initial_slope_x,
            "initial_slope_y": self.initial_slope_y,
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
        zr_declared = None
        if zr is not None:
            zr_declared = unit.length_to_internal(
                _require_finite_positive(zr, "laser.rayleigh_range_m")
            )
        m2_v = None
        if m2 is not None:
            m2_v = _require_finite_positive(m2, "laser.m2")
            # M² 是**光束质量因子**，物理上 ≥ 1（=1 即理想高斯）。
            # 小于 1 无物理意义；若要做人工数学测试请走显式测试语义，不要借这个字段。
            if m2_v < 1.0:
                raise UFDemoError(
                    CONFIG_INVALID,
                    "laser.m2 必须 ≥ 1（光束质量因子；理想高斯为 1）",
                    field_path="laser.m2",
                    actual=m2_v,
                    requirement="M² ≥ 1",
                    suggestion="核对 M² 定义；人工数学测试请另设语义，不要混用该字段。",
                )

        def _zr_implied_by_m2() -> float:
            """由 M² 反算瑞利长度：``z_R = π w0² / (M² λ)``。

            ⚠️ **M² 在分母**。此前实现写成 ``π w0² · M² / λ``（方向反了），
            实测 `M²=2, w0=10 µm, λ=1030 nm` 会给出 `610.0180 µm` 而非 `152.5045 µm`（差 4 倍）。
            因为仓库所有示例的 `m2` 都是 `null`、且没有任何测试用 `laser.m2`，
            这条检查长期零覆盖，反向公式才得以存活。
            """
            if wavelength is None:
                raise UFDemoError(
                    CONFIG_INVALID,
                    "给出 m2 但缺少 wavelength_m，无法导出瑞利长度",
                    field_path="laser.wavelength_m",
                    actual=None,
                    requirement="给出 m2 时必须同时给出波长（z_R = π w0²/(M²λ)）",
                    suggestion="补 wavelength_m，或改用 rayleigh_range_m 直接给出 zR。",
                )
            return math.pi * (w0 ** 2) / (m2_v * wavelength)

        zr_internal = None
        if zr_declared is not None and m2_v is not None:
            # 细则 4.2：zR 与 M2 同时输入时做一致性检查（不用其中一个覆盖另一个）
            zr_expected = _zr_implied_by_m2()
            rel = abs(zr_declared - zr_expected) / max(abs(zr_declared), abs(zr_expected))
            if rel > 1e-6:
                raise UFDemoError(
                    CONFIG_INVALID,
                    "rayleigh_range_m 与 m2 不一致",
                    field_path="laser.rayleigh_range_m",
                    actual={"rayleigh_range_m": zr_declared, "m2": m2_v,
                            "implied_rayleigh_range_m": zr_expected, "relative_difference": rel},
                    requirement="zR = pi*w0^2/(M2*lambda)，相对差 ≤ 1e-6",
                    suggestion="核对高斯束约定后只保留一致的数值。",
                )
            zr_internal = zr_declared
        elif zr_declared is not None:
            zr_internal = zr_declared
        elif m2_v is not None:
            # **只给 M² 时必须导出 zR，不得留空**：
            # `beam.w_of_s(w0, s, zR=None)` 把空值解释为**准直不发散**并直接返回 w0，
            # 于是「填了 M²」看起来生效、实际按理想准直算（静默降级）。
            zr_internal = _zr_implied_by_m2()

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
        t0_v = _require_finite(raw.get("t0_s", 0.0), "path.t0_s")
        tol_v = _require_finite(raw.get("time_tolerance_s", 1e-12), "path.time_tolerance_s")
        if tol_v < 0.0:
            raise UFDemoError(CONFIG_INVALID, "path.time_tolerance_s 必须为非负有限数", field_path="path.time_tolerance_s", actual=tol_v, requirement=">= 0")
        if len(segs_raw) == 0:
            # 明确允许空路径，但由 validate 阶段给出 ZERO_EVENTS 语义处理
            return PathConfig(segments=[], t0_s=t0_v, time_tolerance_s=tol_v)

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
            if not isinstance(a, Sequence) or isinstance(a, (str, bytes)) or len(a) != 3:
                raise UFDemoError(CONFIG_INVALID, f"{p}.start_xyz_m 必须是长度为 3 的数组", field_path=f"{p}.start_xyz_m", actual=a)
            if not isinstance(b, Sequence) or isinstance(b, (str, bytes)) or len(b) != 3:
                raise UFDemoError(CONFIG_INVALID, f"{p}.end_xyz_m 必须是长度为 3 的数组", field_path=f"{p}.end_xyz_m", actual=b)
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
                    laser_on=_require_bool(s.get("laser_on", True), f"{p}.laser_on"),
                    label=str(s.get("label", "")),
                )
            )
        return PathConfig(
            segments=segs,
            t0_s=t0_v,
            time_tolerance_s=tol_v,
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
    #: **焦平面策略**。本项目只开放 ``fixed_original_surface``：焦平面恒为
    #: **加工前的原始上表面** z0，不随槽底下降做 Z 调整（任务书 §6）。
    #:
    #: ⚠️ 与 ``geometry_feedback`` 是**两个不同的开关**（背景文件里也这么写）：
    #:   * ``focus_strategy`` 决定**焦平面的位置**（恒为 z0）；
    #:   * ``geometry_feedback="axial_defocus"`` 提供**被动离焦**
    #:     ``w(d) = w0·sqrt(1+(d/zR)²)`` —— 表面下降使光斑变大，这是几何反馈，
    #:     不是主动调焦。**不得**因为停用动态入射角就连带停用它。
    #:
    #: 逐层对焦 / 动态补偿 / 焦点跟随表面**未实现**：声明其它值一律拒绝
    #: （fail closed），不做静默降级。
    focus_strategy: str = "fixed_original_surface"
    #: **窗口半径策略的默认值**（ADR-0022）：``above_threshold`` 只计算
    #: 「能流可能超过响应阈值」的区域 —— 对去除量**逐位等价**（阈值型核在
    #: ``F <= F_th`` 处返回恰好 0），只把剂量观测量的统计范围缩小。
    #: 核心模式不再让用户在主界面理解多种窗口算法（外部审查 IV 建议）；
    #: 需要 ε 尾部截断口径的完整剂量统计时显式选 ``tail_epsilon``。
    window_radius_policy: str = "above_threshold"
    history_enabled: bool = False
    tail_epsilon: float = 1e-8
    #: **窗口半径策略**（性能开关，默认关闭）。取值：
    #:
    #: * ``"tail_epsilon"``（默认）= 既有行为：窗口半径 = ``cut_radius(w, tail_epsilon)``，
    #:   即把能流尾部按 ε=1e-8 截断（约 3.035w）。
    #: * ``"above_threshold"`` = 只保留**能流可能超过响应阈值**的半径
    #:   ``w*sqrt(ln(F_peak/F_th)/2) * window_threshold_margin``。
    #:
    #: ⚠️ 为什么可以不改变结果：阈值型响应律（如 `log_fixed`）在 ``F <= F_th`` 处
    #: **返回恰好 0**，所以该半径之外的格子对去除量的贡献本来就是 0。
    #: 但**剂量观测量**（``cumulative_fluence`` / ``illumination_count`` /
    #: 估计截获能量）的统计范围会随之缩小 —— 求解诊断 ``fluence_ledger``
    #: 里如实上报被裁掉的格子数与截断比例，不得当成「域截断」解释。
    #: 深孔算例（深度 ≫ zR）下窗口内 99% 的格子属于此类，可省掉 90%+ 的计算。
    window_radius_policy: str = "tail_epsilon"
    #: ``above_threshold`` 策略的安全余量（倍）。``1.0`` = 恰好取到 ``F = F_th`` 的半径；
    #: 默认略留余量，避免网格离散与斜面情形下切到边界格。
    window_threshold_margin: float = 1.25
    memory_budget_bytes: int = 2 * 1024 ** 3
    budget_safety_factor: float = 1.5
    cancel_check_interval: int = 256
    # M3 组合默认关闭（细则 8 节末）
    multiline_incubation: bool = False
    structured_interface: bool = False
    # 动态角度属批次 J（T18）；此处提前占位，用于与分相截断做互斥校验
    dynamic_angle: bool = False
    # 局部核后端（T16）：off = NumPy 参考；numba = 可选 JIT 局部核（缺失时回退并警告）
    acceleration: str = "off"
    # 冻结几何分组批量的步长控制（T17）。仅在 mode="grouped" 时生效。
    batch_size: int = 64
    min_batch_size: int = 1
    local_rel_tol: float = 1e-3
    local_abs_tol_internal: float = 0.0
    geometry_drift_limit: float = 0.25
    max_cell_block: int = 1 << 22
    #: **响应曲线卡文件名**（U07）。非空时，逐事件核改由该实测曲线驱动
    #: （`TabulatedEventLaw`），而不是材料卡的解析对数律。
    #: 这是「真实数据 → 真实求解」的唯一入口；留空则维持原行为。
    response_curve: str | None = None
    #: **标定正增益 a**（C4）。逐事件增量的全局比例：``Δd_cal = a·Δd_base``。
    #: ⚠️ 它作用在**每个脉冲的几何更新之前**，后续脉冲会按新表面重算被动离焦 ——
    #: 因此 ``D(a) ≠ a·D(1)``。**不得**改成只在结果页乘系数。
    #: ``1.0`` = 基线（未标定），这也是默认值。
    response_gain: float = 1.0
    extra: Mapping[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "SolverConfig":
        mode = _require_str(raw.get("mode", "reference"), "solver.mode", ("reference", "grouped"))
        gf = _require_str(raw.get("geometry_feedback", "fixed_geometry"), "solver.geometry_feedback", GEOMETRY_FEEDBACK_MODES)
        focus_strategy = str(raw.get("focus_strategy", "fixed_original_surface") or "").strip()
        if focus_strategy != "fixed_original_surface":
            raise UFDemoError(
                CONFIG_INVALID,
                "solver.focus_strategy 只支持 fixed_original_surface",
                field_path="solver.focus_strategy",
                actual=focus_strategy,
                requirement="'fixed_original_surface'（焦平面恒为加工前原始上表面）",
                suggestion=(
                    "本项目**不提供**逐层 Z 调整 / 焦点跟随表面 / 动态补偿（任务书 §6）。"
                    "被动离焦是另一个开关：用 solver.geometry_feedback='axial_defocus'。"
                ),
            )
        eps = _require_finite_positive(raw.get("tail_epsilon", 1e-8), "solver.tail_epsilon")
        if not (0.0 < eps < 1.0):
            raise UFDemoError(
                CONFIG_INVALID,
                "solver.tail_epsilon 必须落在 (0,1)",
                field_path="solver.tail_epsilon",
                actual=eps,
                requirement="0 < epsilon < 1（默认 1e-8，是数值设置，不是物理损伤阈值）",
            )
        wpol = _require_str(
            raw.get("window_radius_policy", "above_threshold"),
            "solver.window_radius_policy",
            ("tail_epsilon", "above_threshold"),
        )
        wmargin = _require_finite_positive(
            raw.get("window_threshold_margin", 1.25), "solver.window_threshold_margin"
        )
        if wmargin < 1.0:
            raise UFDemoError(
                CONFIG_INVALID,
                "solver.window_threshold_margin 必须 >= 1",
                field_path="solver.window_threshold_margin",
                actual=wmargin,
                requirement=">= 1.0（1.0 = 恰好取到 F = 阈值的半径）",
                suggestion="该余量只影响窗口大小与剂量观测量口径，不影响去除量。",
            )
        budget = raw.get("memory_budget_bytes", 2 * 1024 ** 3)
        if not isinstance(budget, int) or budget <= 0:
            raise UFDemoError(CONFIG_INVALID, "solver.memory_budget_bytes 必须是正整数", field_path="solver.memory_budget_bytes", actual=budget)
        accel = _require_str(raw.get("acceleration", "off"), "solver.acceleration", ("off", "numba"))
        batch_size = _require_positive_int(raw.get("batch_size", 64), "solver.batch_size")
        min_batch_size = _require_positive_int(raw.get("min_batch_size", 1), "solver.min_batch_size")
        if min_batch_size > batch_size:
            raise UFDemoError(
                CONFIG_INVALID,
                "solver.min_batch_size 不得大于 batch_size",
                field_path="solver.min_batch_size",
                actual={"min_batch_size": min_batch_size, "batch_size": batch_size},
                requirement="min_batch_size <= batch_size",
            )
        rel_tol = _require_finite_positive(raw.get("local_rel_tol", 1e-3), "solver.local_rel_tol")
        if not (0.0 < rel_tol < 1.0):
            raise UFDemoError(
                CONFIG_INVALID,
                "solver.local_rel_tol 必须落在 (0,1)",
                field_path="solver.local_rel_tol",
                actual=rel_tol,
                requirement="0 < local_rel_tol < 1（局部步长误差的相对容差）",
                suggestion="细则 9.1 用它控制 batch_size；最终验收仍以完整逐脉冲对照为准。",
            )
        abs_tol = raw.get("local_abs_tol_internal", 0.0)
        if not isinstance(abs_tol, (int, float)) or not math.isfinite(float(abs_tol)) or float(abs_tol) < 0.0:
            raise UFDemoError(
                CONFIG_INVALID,
                "solver.local_abs_tol_internal 必须是非负有限数",
                field_path="solver.local_abs_tol_internal",
                actual=abs_tol,
                requirement=">= 0（内部长度单位；细则 10 节建议取 0.01*delta_test）",
            )
        drift_limit = _require_finite_positive(raw.get("geometry_drift_limit", 0.25), "solver.geometry_drift_limit")
        max_cell_block = _require_positive_int(raw.get("max_cell_block", 1 << 22), "solver.max_cell_block")
        budget_factor = _require_finite_positive(raw.get("budget_safety_factor", 1.5), "solver.budget_safety_factor")
        cancel_interval = _require_positive_int(raw.get("cancel_check_interval", 256), "solver.cancel_check_interval")
        return SolverConfig(
            mode=mode,
            geometry_feedback=gf,
            focus_strategy=focus_strategy,
            history_enabled=_require_bool(raw.get("history_enabled", False), "solver.history_enabled"),
            tail_epsilon=eps,
            window_radius_policy=wpol,
            window_threshold_margin=wmargin,
            memory_budget_bytes=budget,
            budget_safety_factor=budget_factor,
            cancel_check_interval=cancel_interval,
            multiline_incubation=_require_bool(raw.get("multiline_incubation", False), "solver.multiline_incubation"),
            structured_interface=_require_bool(raw.get("structured_interface", False), "solver.structured_interface"),
            dynamic_angle=_require_bool(raw.get("dynamic_angle", False), "solver.dynamic_angle"),
            acceleration=accel,
            batch_size=batch_size,
            min_batch_size=min_batch_size,
            local_rel_tol=rel_tol,
            local_abs_tol_internal=float(abs_tol),
            geometry_drift_limit=drift_limit,
            max_cell_block=max_cell_block,
            response_curve=(str(raw["response_curve"]).strip()
                            if str(raw.get("response_curve") or "").strip() else None),
            response_gain=_require_positive_gain(raw.get("response_gain", 1.0)),
            extra=dict(raw.get("extra", {}) or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "geometry_feedback": self.geometry_feedback,
            "focus_strategy": self.focus_strategy,
            "history_enabled": self.history_enabled,
            "tail_epsilon": self.tail_epsilon,
            "window_radius_policy": self.window_radius_policy,
            "window_threshold_margin": self.window_threshold_margin,
            "memory_budget_bytes": self.memory_budget_bytes,
            "budget_safety_factor": self.budget_safety_factor,
            "cancel_check_interval": self.cancel_check_interval,
            "multiline_incubation": self.multiline_incubation,
            "structured_interface": self.structured_interface,
            "dynamic_angle": self.dynamic_angle,
            "acceleration": self.acceleration,
            "batch_size": self.batch_size,
            "min_batch_size": self.min_batch_size,
            "local_rel_tol": self.local_rel_tol,
            "local_abs_tol_internal": self.local_abs_tol_internal,
            "geometry_drift_limit": self.geometry_drift_limit,
            "max_cell_block": self.max_cell_block,
            "response_curve": self.response_curve,
            "response_gain": self.response_gain,
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
# 2d. ThresholdProtocolConfig（批次 H / T14）
# ---------------------------------------------------------------------------

# 受限阈值协议允许的能流基准（与 ``thresholds.THRESHOLD_FLUENCE_BASES`` 同值。
# 此处复制常量是为了让配置层能独立拒绝，不必反向依赖 thresholds 模块。）
THRESHOLD_FLUENCE_BASES: tuple[str, ...] = ("per_event_incident",)

# 明确拒绝的基准名 -> 可读原因（累计量不得与单脉冲阈值比较）
_REJECTED_THRESHOLD_BASES: dict[str, str] = {
    "cumulative_fluence": "累计入射剂量≠单脉冲峰值能流；两者量纲相同但不可比较",
    "cumulative_dose": "累计入射剂量≠单脉冲峰值能流；两者量纲相同但不可比较",
    "mean_fluence": "平均值不能用于逐事件超阈判定",
    "total_fluence": "累计量不能用于逐事件超阈判定",
}


@dataclass
class ThresholdProtocolConfig:
    """受限阈值协议的显式选择（批次 H / T14）。

    默认 ``enabled=False``：不记录超阈标记，界面如实报不可用。开启后：

    * ``fluence_basis`` 只能是 ``per_event_incident``——**在配置层**拒绝累计/平均基准，
      不靠界面禁用；
    * 卡内有多个条件对应阈值（如 SiC 的改性/去除两类阈值）时**必须**给出
      ``candidate_index``，否则运行前报错，避免静默把改性阈值当去除阈值。
    """

    enabled: bool = False
    observable_name: str = "fluence_above_threshold"
    fluence_basis: str = "per_event_incident"
    candidate_index: int | None = None
    record_mask: bool = True

    @staticmethod
    def from_dict(raw: Mapping[str, Any] | None) -> "ThresholdProtocolConfig":
        raw = raw or {}
        if not isinstance(raw, Mapping):
            raise UFDemoError(
                CONFIG_INVALID,
                "threshold_protocol 必须是 JSON 对象",
                field_path="threshold_protocol",
                actual=type(raw).__name__,
            )
        enabled = _require_bool(raw.get("enabled", False), "threshold_protocol.enabled")
        basis = str(raw.get("fluence_basis", "per_event_incident"))
        if basis in _REJECTED_THRESHOLD_BASES:
            raise UFDemoError(
                CONFIG_INVALID,
                f"threshold_protocol.fluence_basis={basis!r} 不可用",
                field_path="threshold_protocol.fluence_basis",
                actual=basis,
                requirement=f"取值属于 {list(THRESHOLD_FLUENCE_BASES)}",
                suggestion=_REJECTED_THRESHOLD_BASES[basis] + "。请改用 per_event_incident。",
            )
        if basis not in THRESHOLD_FLUENCE_BASES:
            raise UFDemoError(
                CONFIG_INVALID,
                f"threshold_protocol.fluence_basis={basis!r} 未登记",
                field_path="threshold_protocol.fluence_basis",
                actual=basis,
                requirement=f"取值属于 {list(THRESHOLD_FLUENCE_BASES)}",
            )
        ci = raw.get("candidate_index")
        if ci is not None and (not isinstance(ci, int) or isinstance(ci, bool) or ci < 0):
            raise UFDemoError(
                CONFIG_INVALID,
                "threshold_protocol.candidate_index 必须是非负整数",
                field_path="threshold_protocol.candidate_index",
                actual=ci,
                suggestion="多候选阈值需显式选择；不选则运行前报错而不是静默取默认。",
            )
        return ThresholdProtocolConfig(
            enabled=enabled,
            observable_name=str(raw.get("observable_name", "fluence_above_threshold")),
            fluence_basis=basis,
            candidate_index=ci,
            record_mask=_require_bool(raw.get("record_mask", True), "threshold_protocol.record_mask"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "observable_name": self.observable_name,
            "fluence_basis": self.fluence_basis,
            "candidate_index": self.candidate_index,
            "record_mask": self.record_mask,
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
    threshold: "ThresholdProtocolConfig" = field(default_factory=lambda: ThresholdProtocolConfig())
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
        threshold = ThresholdProtocolConfig.from_dict(raw.get("threshold_protocol"))
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
            threshold=threshold,
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
            "threshold_protocol": self.threshold.to_dict(),
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
    if not (math.isfinite(float(t0)) and math.isfinite(float(f)) and f > 0):
        return 0
    # iter_events 使用相对整数时钟 t_j=t0+j/f，j 从 0 开始。
    t_end_max = max(s.end_s for s in path.segments)
    if t_end_max <= t0:
        return 0
    scale = max(1.0, abs((t_end_max - t0) * f))
    idx_tol = max(1e-9, 1e-12 * scale)
    j_end = int(math.floor((t_end_max - t0) * f + idx_tol))
    if j_end < 0:
        return 0
    tol = max(0.0, float(path.time_tolerance_s))
    active = 0
    for s in path.segments:
        if not s.laser_on or s.end_s <= s.start_s:
            continue
        j_lo = int(math.ceil((s.start_s - t0 - tol) * f - idx_tol))
        j_hi = int(math.ceil((s.end_s - t0 - tol) * f - idx_tol)) - 1
        j_lo = max(0, j_lo)
        j_hi = min(j_end, j_hi)
        if j_hi >= j_lo:
            active += j_hi - j_lo + 1
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
        # 批次 H：受限阈值协议的两个观测量（开启时才分配，此处按最坏情况计入）
        "threshold_exceedance_count": n * u32,
        "threshold_exceeded_mask": n * u8,
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
    # --- U07：**曲线驱动的求解不走参考评估器** -----------------------------
    # 下面这些条件（protocol_id / 能流基准 / 有效脉冲数定义 / 重复频率）
    # 约束的是**协议复现**那条路径。本次若由实测曲线驱动逐事件求解，
    # 评估器根本不会被调用 —— 硬套这些条件是把「对象判错」，不是守红线。
    # 曲线自身的适用条件（波长/脉宽是否匹配）仍由核构造时的闸门检查，
    # 那才是真正该管的「这条曲线能不能用」。
    #
    # ⚠️ 必须用 getattr 链：能力入口探针用**轻量桩配置**（SimpleNamespace）
    # 调用本函数，它没有 `solver` 字段 —— 直接 `config.solver.x` 会抛
    # AttributeError，把两条 red-line 探针打成「失效」（本工程实测踩到）。
    if getattr(getattr(config, "solver", None), "response_curve", None):
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


def _require_positive_gain(value: Any) -> float:
    """标定增益必须是**正有限数**。

    为什么不允许 ≤ 0：增益是去除量的比例因子。非正意味着「不去除」或「反向生长」，
    两者都不是本模型的语义，静默接受会让标定结果失去物理意义。
    """
    try:
        g = float(value)
    except (TypeError, ValueError) as exc:
        raise UFDemoError(
            CONFIG_INVALID,
            "标定增益必须是数值",
            field_path="solver.response_gain",
            actual=value,
            requirement="正有限数",
        ) from exc
    if not math.isfinite(g) or g <= 0:
        raise UFDemoError(
            CONFIG_INVALID,
            "标定增益必须是正有限数",
            field_path="solver.response_gain",
            actual=g,
            requirement="> 0（1.0 = 未标定基线）",
            suggestion="增益 ≤ 0 没有物理意义；若基线全为 0，应回对照环节修阈值/光学，而不是调增益。",
        )
    return g


def validate_run(config: RunConfig, material: Any) -> ValidationReport:
    """执行前的准入校验（细则 5.1 前两步 + 4.2 契约）。"""
    notes: list[str] = []
    warnings: list[str] = []
    errors: list[dict[str, Any]] = []

    allowed = tuple(getattr(material, "allowed_run_modes", ()) or ())
    evidence = getattr(material, "evidence_status", None)

    def fail(err: UFDemoError) -> None:
        errors.append(err.to_dict())

    # 当前响应核只实现无历史单脉冲语义；拒绝 history_enabled，避免
    # 配置声明与实际计算静默不一致。
    if config.solver.history_enabled:
        fail(
            UFDemoError(
                NOT_IMPLEMENTED,
                "solver.history_enabled 当前尚未实现",
                field_path="solver.history_enabled",
                actual=True,
                requirement="history_enabled=false（当前响应核仅支持无历史单脉冲）",
                suggestion="关闭 history_enabled；启用历史耦合需提供经过验证的孵化响应核。",
            )
        )

    # 0. 焦平面必须恒为**加工前的原始上表面**（solver.focus_strategy=fixed_original_surface）
    #    from_dict 只拦得住「声明了别的策略」；这里挡住**声明对、路径却把焦点放别处**
    #    这种静默用错焦平面的情形（例如手写配置把焦点放到了槽底）。
    #    判据是**构造性**的：每一段的 z 都必须等于 grid.initial_height_m。
    if str(getattr(config.solver, "focus_strategy", "fixed_original_surface")) == "fixed_original_surface":
        z0 = float(getattr(config.grid, "initial_height_m", 0.0) or 0.0)
        segs = tuple(getattr(config.path, "segments", ()) or ())
        offenders: list[tuple[Any, str, float]] = []
        for seg in segs:
            for key in ("start_xyz_m", "end_xyz_m"):
                xyz = getattr(seg, key, None)
                if xyz is None:
                    continue
                if abs(float(xyz[2]) - z0) > 1e-12:
                    offenders.append((getattr(seg, "segment_id", "?"), key, float(xyz[2])))
        if offenders:
            sid, key, z_val = offenders[0]
            fail(
                UFDemoError(
                    CONFIG_INVALID,
                    "焦平面必须恒为加工前的原始上表面",
                    field_path=f"path.segments[{sid}].{key}[2]",
                    actual={"z_m": z_val, "grid.initial_height_m": z0,
                            "n_offending_endpoints": len(offenders)},
                    requirement="每一段的 z 都等于 grid.initial_height_m",
                    suggestion=(
                        "把路径的 z 设成原始表面高度。本项目**不提供**逐层 Z 调整 / "
                        "焦点跟随表面 / 动态补偿（任务书 §6）；若确实要模拟离焦偏置，"
                        "需要先显式开放另一种 focus_strategy。"
                    ),
                )
            )
        else:
            notes.append(
                f"焦平面：固定于加工前原始上表面 z0 = {z0 * 1e6:.6f} µm，"
                f"{len(segs)} 段端点全部一致；被动离焦由 geometry_feedback 单独决定"
                "（axial_defocus ⇒ w(d)=w0·sqrt(1+(d/zR)²)）。"
            )

    # 1. 模式准入
    if allowed and config.run_mode not in allowed:
        # --- U07 豁免：**深度来源可以来自曲线，而不只是材料卡** -------------
        # 原规则的意图是「没有深度来源就不能出物理深度」。材料卡没有 δ 时
        # 它确实不能；但若本次运行指定了**实测增量曲线**，深度来源就存在了。
        # 判据应从「卡自己有没有」改成「有没有深度来源」——
        # **这是把门槛判对，不是放宽**：
        #   · 只对 `reference_case` 生效（threshold_only 等不受影响）；
        #   · 曲线是否真为 `event_depth_increment` 仍由核构造时的**既有闸门**
        #     （`assert_curve_can_enter_event_kernel`）把关，拦不住就抛错；
        #   · 准入报告里**明写**深度来源是曲线，避免被读成「材料卡已具备该能力」。
        curve_name = getattr(config.solver, "response_curve", None)
        if curve_name and config.run_mode == "reference_case":
            notes.append(
                f"深度来源：响应曲线 `{curve_name}`（材料卡 "
                f"`{config.material_id}` 自身只声明 {list(allowed)}；"
                "本次的物理深度由该曲线提供，不代表材料卡已具备通用深度能力）"
            )
        else:
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
    _ref_errs = check_reference_conditions(config, material)
    if not _ref_errs and getattr(config.solver, "response_curve", None) \
            and config.run_mode == "reference_case":
        # 不静默跳过：报告里明写「为什么这次没做协议校验」——
        # 否则以后有人看到「没有协议错误」会以为协议被核对过了。
        notes.append(
            f"已跳过材料卡参考协议校验：本次由响应曲线 "
            f"`{getattr(config.solver, 'response_curve', None)}` 驱动逐事件求解，"
            "不调用参考评估器；曲线的适用条件（波长/脉宽）由核构造时校验。"
        )
    for e in _ref_errs:
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

    # 8. 几何组合准入（细则 2.3、9.2；批次 J / T18 起开放斜入射与动态角度）
    kx, ky, kz = config.laser.direction_unit
    if kz <= 0.0:
        fail(
            UFDemoError(
                GEOMETRY_UNSUPPORTED,
                "laser.direction_unit 的 z 分量必须为正",
                field_path="laser.direction_unit",
                actual=list(config.laser.direction_unit),
                requirement="k_z > 0（本工程取「光轴正向」约定，使 μ=k·n>0 表示被照射）",
                suggestion=(
                    "把方向整体取反。符号约定见 docs/decisions/"
                    "ADR-0015-oblique-incidence-and-visibility.md。"
                ),
            )
        )
    oblique = not (abs(kx) < 1e-15 and abs(ky) < 1e-15 and abs(kz - 1.0) < 1e-15)
    if oblique:
        inc_deg = math.degrees(math.acos(min(1.0, max(-1.0, kz))))
        if inc_deg > MAX_SUPPORTED_INCIDENCE_DEG + 1e-9:
            fail(
                UFDemoError(
                    GEOMETRY_UNSUPPORTED,
                    "入射角超出软件支持范围",
                    field_path="laser.direction_unit",
                    actual=f"{inc_deg:.4f}°",
                    requirement=(
                        f"入射角 <= {MAX_SUPPORTED_INCIDENCE_DEG:g}°"
                        "（软件数值/展示范围，不是材料物理边界）"
                    ),
                    suggestion="减小入射角；超范围时停止该模式，不裁剪角度继续。",
                )
            )
        warnings.append(
            f"已启用斜入射（相对光轴 {inc_deg:.3f}°）：表面能流按 F_s=μ·F_⊥ 投影，"
            "只作用于可见的首次交点；μ 按"
            + ("逐点法向（动态角度）" if config.solver.dynamic_angle else "解析平面法向")
            + "计算。未提供材料偏振吸收依据时**只做几何修正**，不预测吸收差异。"
        )
    if oblique and config.solver.structured_interface:
        fail(
            UFDemoError(
                CONFIG_INVALID,
                "斜入射与分相结构不得同时启用",
                field_path="solver",
                actual={"oblique_incidence": True, "structured_interface": True},
                requirement="法向去除路径与垂直相列路径不得混用（细则 8 节末）",
                suggestion="改用正入射，或关闭分相结构；扩展须单独定义几何意义并增加测试。",
            )
        )
    # 批次 I（T16/T17）：分组批量是**独立求解模式**，其红线在配置层拦截。
    # 允许的组合：mode=grouped（冻结几何分组），acceleration 只决定局部核后端。
    if config.solver.mode == "grouped":
        if config.solver.structured_interface:
            fail(
                UFDemoError(
                    CONFIG_INVALID,
                    "分组批量不支持分相结构",
                    field_path="solver.mode",
                    actual={"mode": "grouped", "structured_interface": True},
                    requirement="第一版批量仅允许同相结构（细则 9.1 末）",
                    suggestion="改用 mode=reference，或关闭 structure_type 引发分的分相结构。",
                )
            )
        if config.solver.history_enabled:
            fail(
                UFDemoError(
                    CONFIG_INVALID,
                    "分组批量不支持历史耦合",
                    field_path="solver.mode",
                    actual={"mode": "grouped", "history_enabled": True},
                    requirement="第一版批量不得改变事件顺序依赖（细则 9.1）",
                    suggestion="设 solver.history_enabled=false，或改用 mode=reference。",
                )
            )
        if config.solver.dynamic_angle:
            fail(
                UFDemoError(
                    CONFIG_INVALID,
                    "「批量 + 动态角度」组合未开放",
                    field_path="solver.mode",
                    actual={"mode": "grouped", "dynamic_angle": True},
                    requirement="两个增强在第一版不得同时启用（细则 9.1 末）",
                    suggestion="二选一；动态角度属批次 J（T18）。",
                )
            )
    if config.solver.acceleration != "off":
        # Numba 是可选依赖：缺失时**回退 NumPy**（任务书 8 节），
        # 但必须显式给出警告，不静默降级为"看起来一样但没加速"。
        from .accelerators import numba_available

        if not numba_available():
            warnings.append(
                "请求了 solver.acceleration=numba，但当前环境缺少 numba；"
                "已回退到 NumPy 局部核（结果同式、逐位一致，只是没有 JIT 加速）。"
                "安装方式：pip install -e \".[accel]\"。"
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
        # 批次 J（T18）起开放：逐点法向 + 可见性。范围检查在 beam 层逐事件执行
        # （超出 n_z/入射角范围即停并报位置，见 geometry.check_geometry_range）。
        warnings.append(
            "已启用动态角度：法向由当前窗口高度梯度计算（内部中心差分/边界单边差分），"
            f"并按首次交点判定可见性；支持范围为 n_z >= {MIN_SUPPORTED_NZ:g}、"
            f"入射角 <= {MAX_SUPPORTED_INCIDENCE_DEG:g}°，超出即停止该模式。"
            "未提供材料偏振吸收依据时只做几何修正。"
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
                # --- U07：增量来源可以是**实测曲线**，而不只是材料卡 ----------
                # 材料卡的 `response` 里没有 δ / 阈值，是因为**它那批数据**
                # （平均去除率、阈值参考等）不适合当逐事件增量 —— 这条拒绝本身是对的。
                # 但本次运行若指定了 `solver.response_curve`，**材料卡的 response
                # 根本不会被使用**：增量由曲线提供。此时不该被"卡的能力"拦住。
                #
                # 边界（确保没有放开不该放的）：
                #   · 只对 `reference_case` 生效；
                #   · 必须**显式**指定曲线（留空即维持原拒绝）；
                #   · 曲线是否为 `event_depth_increment`、固定条件是否匹配，
                #     仍由核构造时的**既有闸门**把关（拦不住就抛错，不会静默错算）；
                #   · 报告里明写来源，避免被读成「材料卡已具备增量能力」。
                _cn = getattr(config.solver, "response_curve", None)
                if _cn and config.run_mode == "reference_case":
                    notes.append(
                        f"逐事件增量来源：响应曲线 `{_cn}`（材料卡 `{config.material_id}` "
                        f"自身的 response 未被使用；卡内拒绝理由：{cap_reason}）"
                    )
                else:
                    fail(
                        UFDemoError(
                            MATERIAL_CAPABILITY_MISSING,
                            f"材料卡不支持逐事件去除增量：{cap_reason}",
                            field_path="material_id",
                            actual=config.material_id,
                            requirement="需要 event_depth_increment 能力（完整阈值 + 去除尺度 + 匹配条件），或显式指定 solver.response_curve",
                            suggestion=(
                                "补齐 δ 与阈值并确认条件；或指定一条实测增量曲线"
                                "（solver.response_curve）；不得用相近材料、不同脉宽或纳秒数据补成「完整参数」。"
                            ),
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


# ===========================================================================
# 共用实验背景（C1）
# ===========================================================================
#
# 七类材料跑在**同一台设备、同一套光学系统**上，因此功率 / 波长 / NA / M² / 焦点策略
# 这些**固定项共享**（用户 2026-09-13 确认）。但**不共享**烧蚀阈值与去除尺度 ——
# 同一平台不等于覆盖原始工况，也不意味着七材料共用一套响应参数。
#
# ⚠️ 关键设计：本模块**不改动** `SolverConfig` 的字段默认值。
# 底层默认仍是 `fixed_geometry`，既有算例（A–I 的逐位一致基准）不受影响；
# 「业务默认用 axial_defocus」由 `shared_background_patch()` 表达。
# 这样「业务默认」与「底层默认」分开，改前者不会静默改后者的数值行为。

#: 共享背景文件（相对工程根）。
SHARED_BACKGROUND_REL = "data/config/shared_experiment_background.json"


@dataclass
class SharedExperimentBackground:
    """七材料共用的固定实验背景。**不含**阈值 / 去除尺度 / 标定增益。"""

    raw: Mapping[str, Any]

    # -- 功率 ----------------------------------------------------------------
    @property
    def post_objective_power_W(self) -> float:
        """物镜后平均功率 —— 计算单脉冲能量**只能用这个**。"""
        return float(self.raw["power"]["post_objective_mean_power_W"])

    @property
    def software_setpoint_W(self) -> float:
        """软件设定功率，**仅作记录**，不参与脉冲能量计算。"""
        return float(self.raw["power"]["software_setpoint_W"])

    def pulse_energy_J(self, repetition_rate_Hz: float) -> float:
        """``E_p = P_物镜后 / f``（把规则写进代码，免得各处各算一遍）。"""
        if not (math.isfinite(repetition_rate_Hz) and repetition_rate_Hz > 0):
            raise UFDemoError(
                CONFIG_INVALID,
                "重复频率必须是正有限数才能算脉冲能量",
                field_path="laser.repetition_rate_Hz",
                actual=repetition_rate_Hz,
                requirement="> 0",
            )
        return self.post_objective_power_W / float(repetition_rate_Hz)

    # -- 光学 ----------------------------------------------------------------
    @property
    def wavelength_m(self) -> float:
        return float(self.raw["optics"]["wavelength_nm"]) * 1e-9

    @property
    def m2(self) -> float:
        return float(self.raw["optics"]["m2"])

    @property
    def numerical_aperture(self) -> float:
        return float(self.raw["optics"]["numerical_aperture"])

    def derived_waist_m(self) -> float:
        """由 M²、λ、NA 推导 1/e² 束腰半径：``w0 = M²·λ/(π·NA)``。"""
        return self.m2 * self.wavelength_m / (math.pi * self.numerical_aperture)

    def derived_rayleigh_m(self) -> float:
        """由 w0 推导瑞利长度：``zR = π·w0²/(M²·λ)``。"""
        w0 = self.derived_waist_m()
        return math.pi * w0 * w0 / (self.m2 * self.wavelength_m)

    # -- 焦点 ----------------------------------------------------------------
    @property
    def focus_strategy(self) -> str:
        return str(self.raw["focus"]["focus_strategy"])

    @property
    def geometry_feedback(self) -> str:
        return str(self.raw["focus"]["geometry_feedback"])

    # -- 工艺事实：实际单线宽度（≠ 光学束腰） --------------------------------
    @property
    def experiment_machined_region_um(self) -> tuple[float, float] | None:
        """实验表声明的**加工区尺寸** ``(wx, wy)``（μm）；未声明时 ``None``。"""
        d = self.raw.get("experiment_statistic") or {}
        v = d.get("machined_region_um")
        if not v:
            return None
        return (float(v[0]), float(v[1]))

    @property
    def experiment_depth_statistic(self) -> str | None:
        """实验表声明的**深度统计口径**（``full_region_mean`` 等）；未声明时 ``None``。"""
        d = self.raw.get("experiment_statistic") or {}
        v = str(d.get("depth_statistic") or "").strip()
        return v or None

    @property
    def effective_line_width_m(self) -> float | None:
        """**实际烧蚀单线宽度**（用户声明的工艺事实）。

        ⚠️ 与 nominal ``2w0`` **不是一回事**：它包含阈值以上区域的展宽与热影响。
        返回 ``None`` 表示未声明 —— 此时不得拿 nominal 顶上（那会把"实测宽度"
        悄悄替换成"光学计算值"，正是任务书反复禁止的那类混淆）。
        """
        proc = self.raw.get("process") or {}
        v = proc.get("effective_single_line_width_um")
        return None if v is None else float(v) * 1e-6

    @property
    def machined_shape(self) -> str:
        """加工出来的形貌类型。矩形区域走弓字形填充 → ``rectangular_pocket``。"""
        return str((self.raw.get("morphology") or {}).get("machined_shape") or "unknown")

    @staticmethod
    def ablated_half_width_m(
        *, spot_radius_m: float, pulse_energy_J: float, threshold_J_m2: float,
    ) -> float:
        """给定光斑与阈值，**烧蚀**半宽（不是光斑半宽）。

        ``F(r) = F0·exp(-2r²/w²)``；去除发生在 ``F > Fth`` 处：:

            r_abl = w · sqrt(ln(F0/Fth)/2),   F0 = 2E/(πw²)

        这个量随 ``w`` **先增后减**（w 太大时 F0 掉到阈值以下，反而不烧蚀），
        所以反推等效半径时要取物理上合理的那一侧。
        """
        if not (spot_radius_m > 0 and pulse_energy_J > 0 and threshold_J_m2 > 0):
            return 0.0
        f0 = 2.0 * pulse_energy_J / (math.pi * spot_radius_m * spot_radius_m)
        if f0 <= threshold_J_m2:
            return 0.0
        return spot_radius_m * math.sqrt(math.log(f0 / threshold_J_m2) / 2.0)

    def equivalent_spot_radius_m(
        self, *, pulse_energy_J: float, threshold_J_m2: float,
    ) -> tuple[float, dict[str, Any]]:
        """反推**等效光斑半径**，使模型给出的烧蚀宽度等于声明的单线宽度。

        为什么要这一层：名义光学束腰（0.874 μm）与**实测单线宽度**（5 μm）
        差 26%（宽 26% ⇒ 面积差 59%），直接用名义值会让覆盖/搭接判断失真 ——
        那正是之前 h/N 规划"覆盖只有 5–24%"的根源之一。

        返回 ``(半径, 诊断)``；未声明单线宽度时返回名义 w0 并标注 ``declared=False``。
        """
        w_nominal = self.derived_waist_m()
        width = self.effective_line_width_m
        if width is None:
            return w_nominal, {
                "declared": False,
                "source": "nominal_optics",
                "note": "未声明实际单线宽度 → 用名义 w0（**这不是实测宽度**）",
                "spot_radius_um": w_nominal * 1e6,
            }
        target = width / 2.0
        # 烧蚀半宽随 w 先增后减：先找极大点，再在 [w0, w_max] 上二分（物理上合理的一侧）
        c = math.log(2.0 * pulse_energy_J / (math.pi * threshold_J_m2))
        w_max = math.sqrt(math.exp(c - 1.0))
        half_at = lambda w: self.ablated_half_width_m(  # noqa: E731
            spot_radius_m=w, pulse_energy_J=pulse_energy_J, threshold_J_m2=threshold_J_m2)
        if w_max <= w_nominal or half_at(w_max) < target:
            return w_nominal, {
                "declared": True,
                "source": "unreachable",
                "note": (f"声明宽度 {width*1e6:.3f} μm 在该能量/阈值下**不可能达到**"
                         f"（最大只有 {half_at(w_max)*2e6:.3f} μm）→ 退回名义 w0"),
                "spot_radius_um": w_nominal * 1e6,
                "max_achievable_width_um": half_at(w_max) * 2e6,
            }
        lo, hi = w_nominal, w_max * 0.999999
        for _ in range(200):
            mid = 0.5 * (lo + hi)
            if half_at(mid) < target:
                lo = mid
            else:
                hi = mid
        w_eq = 0.5 * (lo + hi)
        return w_eq, {
            "declared": True,
            "source": "derived_from_declared_line_width",
            "declared_width_um": width * 1e6,
            "nominal_waist_um": w_nominal * 1e6,
            "spot_radius_um": w_eq * 1e6,
            "nominal_width_um": half_at(w_nominal) * 2e6,
            "note": (f"由「实测单线宽度 {width*1e6:.3f} μm」+ 阈值 {threshold_J_m2/1e4:.3f} J/cm² "
                     f"反推等效 w = {w_eq*1e6:.4f} μm；名义 w0={w_nominal*1e6:.4f} μm "
                     f"只作光学记录（它给出的宽度是 {half_at(w_nominal)*2e6:.3f} μm）"),
        }

    def to_dict(self) -> dict[str, Any]:
        return dict(self.raw)

    # -- 自洽校验 ------------------------------------------------------------
    def check_self_consistency(self, *, rel_tol: float = 0.01) -> dict[str, Any]:
        """校验资料给的 nominal 值与公式推导**自洽**。

        为什么值得做：这两个数是整条光路的地基（离焦、能流、覆盖全依赖它们）。
        若有人手改了其中一个却没改另一个，公式与资料就脱节了 ——
        这里直接报出来，而不是让它静默参与后面所有计算。
        """
        w0_doc = float(self.raw["optics"]["nominal_waist_radius_um"]) * 1e-6
        zr_doc = float(self.raw["optics"]["nominal_rayleigh_length_um"]) * 1e-6
        w0_calc, zr_calc = self.derived_waist_m(), self.derived_rayleigh_m()
        rel_w0 = abs(w0_calc - w0_doc) / w0_doc
        rel_zr = abs(zr_calc - zr_doc) / zr_doc
        ok = rel_w0 <= rel_tol and rel_zr <= rel_tol
        report = {
            "ok": ok,
            "waist_doc_um": w0_doc * 1e6,
            "waist_derived_um": w0_calc * 1e6,
            "waist_rel_diff": rel_w0,
            "rayleigh_doc_um": zr_doc * 1e6,
            "rayleigh_derived_um": zr_calc * 1e6,
            "rayleigh_rel_diff": rel_zr,
            "rel_tol": rel_tol,
        }
        if not ok:
            raise UFDemoError(
                CONFIG_INVALID,
                "共享实验背景的光学参数与公式推导不自洽",
                field_path="optics.nominal_waist_radius_um|nominal_rayleigh_length_um",
                actual={"w0_um": w0_calc * 1e6, "zR_um": zr_calc * 1e6},
                requirement=f"与 w0=M²λ/(πNA)、zR=πw0²/(M²λ) 的相对差 ≤ {rel_tol:.1%}",
                suggestion="核对背景文件的 nominal 值；两者必须由同一组 M²/λ/NA 推出。",
            )
        return report


def load_shared_background(path: str | Path | None = None) -> SharedExperimentBackground:
    """读共享实验背景；默认取工程根的 ``data/config/shared_experiment_background.json``。"""
    if path is None:
        # 用 resource_root 而不是 project_root：wheel 安装后后者指向**不存在**的
        # 目录（包在 site-packages），背景文件会读不到；resource_root 会正确回退。
        from . import resource_root as _root

        path = _root() / SHARED_BACKGROUND_REL
    p = Path(path)
    if not p.exists():
        raise UFDemoError(
            CONFIG_INVALID,
            f"找不到共享实验背景文件：{p}",
            field_path="shared_experiment_background",
            actual=str(p),
            requirement="文件存在",
            suggestion="确认 data/config/shared_experiment_background.json 已就位。",
        )
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UFDemoError(
            CONFIG_INVALID, "共享实验背景 JSON 解析失败",
            field_path=str(p), actual=str(exc), suggestion="修正 JSON 语法。",
        ) from exc
    bg = SharedExperimentBackground(raw=raw)
    bg.check_self_consistency()   # 读到就校验，别让错值流进后续计算
    return bg


def shared_background_patch(
    bg: SharedExperimentBackground, *, repetition_rate_Hz: float,
    threshold_J_m2: float | None = None,
) -> dict[str, Any]:
    """把共享背景映射成可合并进 ``RunConfig`` raw 的 patch。

    这是「业务默认」的载体：调用方把它 merge 到自己的参数 dict 上即可，
    **无需**另造一套底层配置系统。

    映射（与既有字段一一对应，不新增底层字段）::

        laser.wavelength_m         ← optics.wavelength_nm
        laser.spot_radius_m        ← 公式推导 w0（**按名义光学冻结**，不随工况/拟合变化；
                                      曾在此按「声明的单线宽度」反推等效光斑 —— 已废，
                                      那会使光学随频率与假定 F_th 漂移，见 ADR-0020）
        laser.rayleigh_range_m     ← 公式推导 zR（同样冻结）
        laser.m2                   ← optics.m2
        laser.pulse_energy_J       ← P_物镜后 / f
        solver.geometry_feedback   ← focus.geometry_feedback（axial_defocus）
        solver.dynamic_angle       ← focus.dynamic_angle（false）
    """
    energy = bg.pulse_energy_J(repetition_rate_Hz)
    #
    # ⚠️ **光斑按名义光学冻结**（ADR-0020，用户 2026-09-15 明确"这个一定要改"）。
    #
    # 曾经的做法：给了阈值就按「声明的实测单线宽度」**反推等效光斑**并替换 w0、
    # 连带改 zR。实测它使光学随**频率与假定的 F_th** 漂移
    # （F_th=78496 时 2/20/40/200 kHz → w0 1.1334/1.3254/1.4085/**1.7026** µm，
    #  zR 3.2649→**7.3678** µm；名义 0.8743/1.9429）：
    #   * w0 = M²λ/(πNA) 只由 λ/NA/M² 决定，与频率、能量、阈值**无关**；
    #   * 反推方程里带着 E_p = P/f ⇒ 每换一个频率就把光学系统"重新标定"一次；
    #   * 5 μm 是**加工出来的线宽**（多脉冲扫描的结果），不是单脉冲高斯足迹的直径，
    #     把它反解成 w0 等于把频率依赖计入两次，还把 δ/F_th 的误差吸收进光学自由度。
    # ⇒ 光学冻结；声明的单线宽度降级为**对照量**（见下方 basis），不再是装配输入。
    w = bg.derived_waist_m()
    zr = bg.derived_rayleigh_m()
    basis: dict[str, Any] = {
        "source": "frozen_nominal_optics",
        "spot_radius_um": w * 1e6,
        "nominal_waist_um": w * 1e6,
        "nominal_rayleigh_um": zr * 1e6,
        "declared_line_width_um": (bg.effective_line_width_m * 1e6
                                   if bg.effective_line_width_m is not None else None),
        "note": (
            "光斑/瑞利长度按**名义光学冻结**（w0=M²λ/(πNA)、zR=πw0²/(M²λ)），"
            "与频率、能量、假定的 F_th **无关**。声明的单线宽度是**加工结果**，"
            "只作对照量，不再反推光斑（ADR-0020）。"
        ),
    }
    if threshold_J_m2 is not None and threshold_J_m2 > 0:
        # **对照量**：名义光学下模型的首击烧蚀宽度 vs 声明的实测单线宽度。
        # 差得多 → 该去查 F_th/δ 或搭接模型，**不是**去改光斑。
        _half = bg.ablated_half_width_m(spot_radius_m=w, pulse_energy_J=energy,
                                        threshold_J_m2=float(threshold_J_m2))
        _model_w = 2.0 * _half
        basis.update({
            "threshold_j_m2": float(threshold_J_m2),
            "model_first_shot_width_um": _model_w * 1e6,
        })
        if bg.effective_line_width_m is not None and _model_w > 0:
            basis["declared_over_model_width"] = (
                bg.effective_line_width_m / _model_w)
    return {
        "laser": {
            "wavelength_m": bg.wavelength_m,
            "spot_radius_m": w,
            "m2": bg.m2,
            "pulse_energy_J": energy,
            "repetition_rate_Hz": float(repetition_rate_Hz),
            "rayleigh_range_m": zr,
            "_spot_radius_basis": basis,
        },
        "solver": {
            # ⚠️ 这两个**必须同时**出现：固定焦点（不主动调焦）与保留被动轴向离焦
            # 是**两件事**。停用动态入射角不得连带停用轴向离焦 —— 分开写清楚。
            "geometry_feedback": bg.geometry_feedback,
            "dynamic_angle": bool(bg.raw["focus"]["dynamic_angle"]),
        },
        "_shared_background_note": (
            "以上字段继承自共用实验背景（用户确认，非本次独立测量）；"
            "各 CSV 的脉宽/频率/速度/间距/遍数**仍按原值**，未被覆盖。"
        ),
    }
