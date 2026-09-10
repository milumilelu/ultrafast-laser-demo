"""高度场、相界面与局部历史（执行细则 4.1、5.2 第 8 步、6.5 节）。

采用物理高度 ``h(x,y)``，z 轴向外，初始面为 ``h0``。
累计垂直去除深度 ``d = h0 - h >= 0``。平面正入射基线按 ``h <- h - a`` 更新。

数组形状固定 ``(ny, nx)``：第 0 维 y，第 1 维 x。主场 float64，
``phase_id`` 为 uint16，计数为 uint32 并检查溢出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .config import GridConfig, LaserConfig
from .errors import NUMERIC_NONFINITE, RESOURCE_BUDGET_EXCEEDED, UFDemoError

UINT32_MAX = 2 ** 32 - 1


@dataclass
class UpdateDiagnostics:
    """一次事件提交后的诊断（细则 5.2 第 8 步：只提交一次）。"""

    event_index: int
    candidate_volume_internal: float
    applied_volume_internal: float
    clipped_volume_internal: float
    n_ablating_cells: int
    counter_before_max: int
    counter_after_max: int
    clipped_events: int = 0
    notes: list[str] = field(default_factory=list)

    COMBINE_FIELDS = ("candidate_volume_internal", "applied_volume_internal", "clipped_volume_internal", "n_ablating_cells")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_index": self.event_index,
            "candidate_volume_internal": self.candidate_volume_internal,
            "applied_volume_internal": self.applied_volume_internal,
            "unapplied_candidate_removal_volume_internal": self.clipped_volume_internal,
            "n_ablating_cells": self.n_ablating_cells,
            "counter_before_max": self.counter_before_max,
            "counter_after_max": self.counter_after_max,
            "clipped_events": self.clipped_events,
            "notes": list(self.notes),
            "volume_name_note": "未应用候选去除体积不是剩余能量，也不是热损失。",
        }


class _StructureProtocol:
    """阶段 G（T11–T13）才实现的结构接口占位。

    M0 只有单一均质相，因此 ``phase_at`` 恒为 0、``next_different_interface``
    返回 ``inf``。相界面截断属批次 G，配置层已拦截。
    """

    is_uniform = True

    def phase_at(self, x, y, z):  # pragma: no cover - M2 实现
        raise NotImplementedError("结构化相界面属批次 G（T11–T13）")

    def next_different_interface(self, x, y, z):  # pragma: no cover - M2 实现
        raise NotImplementedError("结构化相界面属批次 G（T11–T13）")


@dataclass
class SurfaceState:
    grid: GridConfig
    laser: LaserConfig
    x: Any
    y: Any
    height: Any
    initial_height: Any
    phase_id: Any
    exposure_count: Any
    cumulative_fluence: Any
    illumination_count: Any
    warning_mask: Any
    warnings: list[str] = field(default_factory=list)
    counters_max: int = 0
    history_enabled: bool = False
    structure: Any = field(default_factory=_StructureProtocol)

    # -- 构造 ---------------------------------------------------------------
    @staticmethod
    def initialize(grid: GridConfig, laser: LaserConfig, *, history_enabled: bool = False) -> "SurfaceState":
        import numpy as np

        x = grid.axis("x")
        y = grid.axis("y")
        h0 = np.full((grid.ny, grid.nx), grid.initial_height_m, dtype=np.float64)
        return SurfaceState(
            grid=grid,
            laser=laser,
            x=x,
            y=y,
            height=h0.copy(),
            initial_height=h0.copy(),
            phase_id=np.zeros((grid.ny, grid.nx), dtype=np.uint16),
            exposure_count=np.zeros((grid.ny, grid.nx), dtype=np.uint32),
            cumulative_fluence=np.zeros((grid.ny, grid.nx), dtype=np.float64),
            illumination_count=np.zeros((grid.ny, grid.nx), dtype=np.uint32),
            warning_mask=np.zeros((grid.ny, grid.nx), dtype=bool),
            history_enabled=history_enabled,
        )

    # -- 索引 ---------------------------------------------------------------
    def nearest_index_x(self, x_value: float) -> int:
        import numpy as np

        return int(np.argmin(np.abs(self.x - x_value)))

    def nearest_index_y(self, y_value: float) -> int:
        import numpy as np

        return int(np.argmin(np.abs(self.y - y_value)))

    # -- 更新 ---------------------------------------------------------------
    def apply_increment(
        self,
        increment: Any,
        *,
        event_index: int,
        section: tuple[int, int, int, int],
        structure: Any = None,
        candidate_volume_internal: float | None = None,
        history: Mapping[str, Any] | None = None,
    ) -> UpdateDiagnostics:
        """一次提交本事件的高度、历史和暴露相更新（细则 5.2 第 8 步）。"""
        import numpy as np

        iy0, iy1, ix0, ix1 = section
        vals = np.asarray(increment, dtype=np.float64)[iy0:iy1, ix0:ix1] if increment.shape != (iy1 - iy0, ix1 - ix0) else np.asarray(increment, dtype=np.float64)
        if vals.shape != (iy1 - iy0, ix1 - ix0):
            raise UFDemoError(
                NUMERIC_NONFINITE,
                "候选去除量与窗口形状不匹配",
                field_path="surface.apply_increment",
                actual={"increment": list(vals.shape), "window": [iy1 - iy0, ix1 - ix0]},
                requirement="两者一致",
            )
        if not np.all(np.isfinite(vals)):
            raise UFDemoError(NUMERIC_NONFINITE, "候选去除量含非有限值", field_path="surface.apply_increment", actual="non-finite")
        if np.any(vals < 0.0):
            raise UFDemoError(NUMERIC_NONFINITE, "候选去除量为负", field_path="surface.apply_increment", actual=float(np.min(vals)))

        dA = self.grid.dx_m * self.grid.dy_m
        cand = float(candidate_volume_internal) if candidate_volume_internal is not None else float(np.sum(vals) * dA)

        # 结构截断（批次 G）：M0 为均质相，直接应用
        applied = vals
        clipped = 0.0
        if structure is not None and not getattr(structure, "is_uniform", True):
            raise UFDemoError(
                RESOURCE_BUDGET_EXCEEDED,
                "结构化相界面的高度更新属批次 G（T11–T13）",
                field_path="solver.structured_interface",
                actual=True,
                suggestion="设 solver.structured_interface=false。",
            )

        self.height[iy0:iy1, ix0:ix1] -= applied

        # 计数（uint32 溢出检查）
        window = self.exposure_count[iy0:iy1, ix0:ix1]
        touched = applied > 0.0
        before = int(window.max()) if window.size else 0
        if touched.any():
            if int(window[touched].max()) >= UINT32_MAX:
                raise UFDemoError(
                    RESOURCE_BUDGET_EXCEEDED,
                    "受照计数溢出 uint32",
                    field_path="surface.exposure_count",
                    actual=UINT32_MAX,
                    requirement="计数 < 2^32",
                    suggestion="改用 uint64 或分段统计。",
                )
            window[touched] += 1
        after = int(window.max()) if window.size else 0
        self.counters_max = max(self.counters_max, after)

        applied_vol = float(np.sum(applied) * dA)
        return UpdateDiagnostics(
            event_index=event_index,
            candidate_volume_internal=cand,
            applied_volume_internal=applied_vol,
            clipped_volume_internal=max(0.0, cand - applied_vol),
            n_ablating_cells=int(np.count_nonzero(touched)),
            counter_before_max=before,
            counter_after_max=after,
        )

    def accumulate_illumination(self, section: tuple[int, int, int, int], fluence: Any, mask: Any) -> float:
        """累计入射剂量与照射诊断（细则 5.4：局部入射能流之和）。"""
        import numpy as np

        iy0, iy1, ix0, ix1 = section
        f = np.asarray(fluence, dtype=np.float64)
        m = np.asarray(mask, dtype=bool)
        self.cumulative_fluence[iy0:iy1, ix0:ix1] += np.where(m, f, 0.0)
        illum = self.illumination_count[iy0:iy1, ix0:ix1]
        if m.any():
            if int(illum[m].max()) >= UINT32_MAX:
                raise UFDemoError(RESOURCE_BUDGET_EXCEEDED, "照射计数溢出 uint32", field_path="surface.illumination_count")
            illum[m] += 1
        return float(np.sum(f[m]) * self.grid.dx_m * self.grid.dy_m) if m.any() else 0.0

    # -- 只读视图 -----------------------------------------------------------
    @property
    def depth(self):
        """累计垂直去除深度 ``d = h0 - h >= 0``。"""
        return self.initial_height - self.height

    def max_depth(self) -> float:
        import numpy as np

        return float(np.max(self.depth))

    def to_snapshot(self, *, event_index: int, time_s: float) -> dict[str, Any]:
        import numpy as np

        return {
            "event_index": event_index,
            "time_s": time_s,
            "height": self.height.astype(np.float64),
            "depth": self.depth.astype(np.float64),
            "phase_id": self.phase_id.astype(np.uint16),
            "cumulative_fluence": self.cumulative_fluence.astype(np.float64),
            "illumination_count": self.illumination_count.astype(np.uint32),
            "exposure_count": self.exposure_count.astype(np.uint32),
            "warning_mask": self.warning_mask.astype(np.uint8),
        }
