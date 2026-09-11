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
    n_clipped_cells: int = 0
    phase_switch_cells: int = 0
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
            "n_clipped_cells": self.n_clipped_cells,
            "phase_switch_cells": self.phase_switch_cells,
            "notes": list(self.notes),
            "volume_name_note": "未应用候选去除体积不是剩余能量，也不是热损失。",
        }


class _StructureProtocol:
    """单相均质结构的占位实现（批次 G 起由 ``ufdemo.structure`` 提供真实实现）。

    ``phase_at`` 恒为 0、``next_different_interface`` 返回 ``inf``，因此
    ``apply_increment`` 的截断分支不会被触发，M0 路径逐位不变。
    """

    is_uniform = True

    def phase_at(self, x, y, z):  # pragma: no cover - 由 uniform 分支短路
        raise NotImplementedError("单相均质结构不提供 phase_at")

    def next_different_interface(self, x, y, z):  # pragma: no cover - 由 uniform 分支短路
        raise NotImplementedError("单相均质结构不提供 next_different_interface")


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
    threshold_exceedance_count: Any = None
    threshold_exceeded_mask: Any = None
    warnings: list[str] = field(default_factory=list)
    counters_max: int = 0
    history_enabled: bool = False
    structure: Any = field(default_factory=_StructureProtocol)

    # -- 构造 ---------------------------------------------------------------
    @staticmethod
    def initialize(
        grid: GridConfig,
        laser: LaserConfig,
        *,
        history_enabled: bool = False,
        threshold_protocol: bool = False,
    ) -> "SurfaceState":
        import numpy as np

        x = grid.axis("x")
        y = grid.axis("y")
        # 批次 J（T18）：初始面可以是**解析斜平面** h = h0 + s_x*(x-cx) + s_y*(y-cy)。
        # 斜率无量纲（h_x、h_y），乘以长度坐标后自然得到长度。用于验证
        # Δh = -a_n/n_z 与法向解析值（`geometry.analytic_plane_normal`）。
        if getattr(grid, "initial_surface", "flat") == "tilted_plane":
            sx, sy = grid.initial_slope
            XX = x[None, :] - float(grid.center_x_m)
            YY = y[:, None] - float(grid.center_y_m)
            h0 = float(grid.initial_height_m) + sx * XX + sy * YY
            h0 = np.ascontiguousarray(h0, dtype=np.float64)
        else:
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
            # 批次 H：受限阈值协议的观测量。**只在协议开启时分配**——未开启时保持
            # None，落盘与界面都如实报「不提供」，不返回全 0 假数组。
            threshold_exceedance_count=(
                np.zeros((grid.ny, grid.nx), dtype=np.uint32) if threshold_protocol else None
            ),
            threshold_exceeded_mask=(
                np.zeros((grid.ny, grid.nx), dtype=bool) if threshold_protocol else None
            ),
            history_enabled=history_enabled,
        )

    # -- 相标签 -------------------------------------------------------------
    def initialize_phases(self, structure: Any) -> None:
        """按结构给初始表面打相标签（批次 G）。均质结构直接返回，不改动。"""
        import numpy as np

        if structure is None or getattr(structure, "is_uniform", True):
            return
        self.structure = structure
        xx = np.broadcast_to(self.x[None, :], self.height.shape)
        yy = np.broadcast_to(self.y[:, None], self.height.shape)
        pid = np.asarray(structure.phase_at(xx, yy, self.initial_height)).astype(np.uint16)
        if pid.shape != self.height.shape:
            raise UFDemoError(
                NUMERIC_NONFINITE,
                "相标签形状与高度场不一致",
                field_path="structure.phase_at",
                actual=list(pid.shape),
                requirement=f"与网格形状 {list(self.height.shape)} 一致",
            )
        self.phase_id = pid

    def phase_counts(self) -> dict[str, int]:
        """当前相标签的单元计数；均质结构返回空字典。"""
        st = self.structure
        if st is None or getattr(st, "is_uniform", True):
            return {}
        return st.phase_cell_counts(self.phase_id)

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

        # 第 7 步：相界面截断（批次 G）。均质相直接应用，逐位不变。
        applied = vals
        n_clipped_cells = 0
        phase_switch_cells = 0
        notes: list[str] = []
        if structure is not None and not getattr(structure, "is_uniform", True):
            xg = np.broadcast_to(self.x[ix0:ix1][None, :], vals.shape)
            yg = np.broadcast_to(self.y[iy0:iy1][:, None], vals.shape)
            zg = self.height[iy0:iy1, ix0:ix1]
            dist = np.asarray(structure.next_different_interface(xg, yg, zg), dtype=np.float64)
            if dist.shape != vals.shape:
                raise UFDemoError(
                    NUMERIC_NONFINITE,
                    "相界面距离与候选去除量形状不匹配",
                    field_path="structure.next_different_interface",
                    actual=list(np.shape(dist)),
                    requirement=f"与窗口形状 {list(vals.shape)} 一致",
                )
            if not np.all(np.isfinite(dist) | np.isinf(dist)):
                raise UFDemoError(NUMERIC_NONFINITE, "相界面距离含 NaN", field_path="structure.next_different_interface")
            # 细则 8 节界面更新规则 2、3：实际去除取「候选」与「到界面距离」的较小值
            applied = np.minimum(vals, np.maximum(dist, 0.0))
            n_clipped_cells = int(np.count_nonzero(applied < vals))
            if n_clipped_cells:
                notes.append(
                    "本事件有单元格被相界面截断：实际去除取候选与到界面距离的较小值；"
                    "被截断的候选量计入「未应用候选去除体积」。"
                )
        self.height[iy0:iy1, ix0:ix1] -= applied

        # 第 8 步：达到界面后更新相标签（下一真实脉冲才对新相响应）
        if structure is not None and not getattr(structure, "is_uniform", True):
            touched = applied > 0.0
            if np.any(touched):
                xg = np.broadcast_to(self.x[ix0:ix1][None, :], vals.shape)
                yg = np.broadcast_to(self.y[iy0:iy1][:, None], vals.shape)
                new_pid = np.asarray(structure.phase_at(xg, yg, self.height[iy0:iy1, ix0:ix1])).astype(np.uint16)
                old = self.phase_id[iy0:iy1, ix0:ix1]
                changed = touched & (new_pid != old)
                phase_switch_cells = int(np.count_nonzero(changed))
                if phase_switch_cells:
                    self.phase_id[iy0:iy1, ix0:ix1] = np.where(changed, new_pid, old)
                    notes.append(
                        f"本事件后有 {phase_switch_cells} 个单元格暴露到新相；"
                        "新相响应从下一个真实脉冲开始，本脉冲不对新相重复施加完整能量。"
                    )

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
            clipped_events=1 if n_clipped_cells else 0,
            n_clipped_cells=n_clipped_cells,
            phase_switch_cells=phase_switch_cells,
            notes=notes,
        )

    # -- 批量块提交（批次 I / T17）------------------------------------------
    def apply_block_increment(
        self,
        delta_h: Any,
        *,
        touch_counts: Any = None,
        fluence_sum: Any = None,
        illum_counts: Any = None,
        exceed_counts: Any = None,
        exceed_or: Any = None,
        section: tuple[int, int, int, int] | None = None,
    ) -> dict[str, Any]:
        """一次性提交一个事件块在**冻结几何**下累计出的增量与计数。

        与 :meth:`apply_increment` 的区别：这里接收的是**已经累加好的**块级
        数组（每个脉冲的非线性响应已在 `accelerators.accumulate_block` 内
        分别计算后求和），因此本方法**不做**相界面截断——批量第一版仅允许
        同相、无历史路径（红线在配置层拦截）。

        计数语义与逐脉冲完全一致：``touch_counts`` 是每个单元在块内被去除的
        **次数**，不是布尔量，直接累加即可与逐脉冲路径对齐。
        """
        import numpy as np

        if section is None:
            iy0, iy1, ix0, ix1 = 0, self.grid.ny, 0, self.grid.nx
        else:
            iy0, iy1, ix0, ix1 = section
        win = (slice(iy0, iy1), slice(ix0, ix1))

        d = np.asarray(delta_h, dtype=np.float64)[win]
        if not np.all(np.isfinite(d)):
            raise UFDemoError(NUMERIC_NONFINITE, "块增量含非有限值", field_path="surface.apply_block_increment", actual="non-finite")
        if np.any(d < 0.0):
            raise UFDemoError(NUMERIC_NONFINITE, "块增量为负", field_path="surface.apply_block_increment", actual=float(np.min(d)))
        if d.shape != (iy1 - iy0, ix1 - ix0):
            raise UFDemoError(
                NUMERIC_NONFINITE,
                "块增量形状与窗口不匹配",
                field_path="surface.apply_block_increment",
                actual=list(d.shape),
                requirement=f"[{iy1 - iy0}, {ix1 - ix0}]",
            )

        self.height[win] -= d

        n_touched_cells = 0
        counter_before = 0
        counter_after = 0
        if touch_counts is not None:
            tc = np.asarray(touch_counts)[win]
            exp = self.exposure_count[win]
            counter_before = int(exp.max()) if exp.size else 0
            if tc.any():
                if int(exp.max()) + int(tc.max()) >= UINT32_MAX:
                    raise UFDemoError(
                        RESOURCE_BUDGET_EXCEEDED,
                        "受照计数溢出 uint32（块提交）",
                        field_path="surface.exposure_count",
                        actual=int(exp.max()) + int(tc.max()),
                        requirement="计数 < 2^32",
                        suggestion="缩小 batch_size 或分段统计。",
                    )
                n_touched_cells = int(np.count_nonzero(tc))
                exp += tc.astype(exp.dtype)
            counter_after = int(exp.max()) if exp.size else 0
            self.counters_max = max(self.counters_max, counter_after)

        if fluence_sum is not None:
            self.cumulative_fluence[win] += np.asarray(fluence_sum, dtype=np.float64)[win]

        if illum_counts is not None:
            ic = np.asarray(illum_counts)[win]
            illum = self.illumination_count[win]
            if ic.any():
                if int(illum.max()) + int(ic.max()) >= UINT32_MAX:
                    raise UFDemoError(
                        RESOURCE_BUDGET_EXCEEDED,
                        "照射计数溢出 uint32（块提交）",
                        field_path="surface.illumination_count",
                    )
                illum += ic.astype(illum.dtype)

        n_exceed_added = 0
        if exceed_counts is not None or exceed_or is not None:
            if self.threshold_exceedance_count is None or self.threshold_exceeded_mask is None:
                raise UFDemoError(
                    RESOURCE_BUDGET_EXCEEDED,
                    "受限阈值协议未开启，无法提交超阈观测量",
                    field_path="surface.threshold_exceedance_count",
                    actual=None,
                    requirement="threshold_protocol=true 时才会分配该观测量",
                )
            if exceed_counts is not None:
                ec = np.asarray(exceed_counts)[win]
                cnt = self.threshold_exceedance_count[win]
                if ec.any():
                    if int(cnt.max()) + int(ec.max()) >= UINT32_MAX:
                        raise UFDemoError(
                            RESOURCE_BUDGET_EXCEEDED,
                            "超阈计数溢出 uint32（块提交）",
                            field_path="surface.threshold_exceedance_count",
                        )
                    cnt += ec.astype(cnt.dtype)
            if exceed_or is not None:
                eo = np.asarray(exceed_or, dtype=bool)[win]
                n_exceed_added = int(np.count_nonzero(eo))
                self.threshold_exceeded_mask[win] |= eo

        return {
            "n_touched_cells": n_touched_cells,
            "counter_before_max": counter_before,
            "counter_after_max": counter_after,
            "applied_volume_internal": float(np.sum(d) * self.grid.dx_m * self.grid.dy_m),
            "n_threshold_added": n_exceed_added,
        }

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

    def accumulate_threshold(self, section: tuple[int, int, int, int], exceed: Any) -> int:
        """累计受限阈值协议的观测量（批次 H / T14）。

        ``exceed`` 必须是**本事件入射能流**的超阈布尔掩膜（由
        ``thresholds.classify_exceedance`` 产生）。本方法只记录分类观测量：

        * 不改高度场、不产生去除量——改性标记≠已去除体积；
        * 协议未开启（数组为 None）时直接报错，避免"悄悄丢弃"。

        返回本次新增的超阈单元数。
        """
        import numpy as np

        if self.threshold_exceedance_count is None or self.threshold_exceeded_mask is None:
            raise UFDemoError(
                RESOURCE_BUDGET_EXCEEDED,
                "受限阈值协议未开启，无法累计超阈观测量",
                field_path="surface.threshold_exceedance_count",
                actual=None,
                requirement="solver 传入 threshold_protocol=true 时才会分配该观测量",
                suggestion="在配置里开启 threshold_protocol.enabled；不开启时界面如实报不可用。",
            )
        if exceed is None:
            return 0
        iy0, iy1, ix0, ix1 = section
        e = np.asarray(exceed, dtype=bool)
        if e.shape != (iy1 - iy0, ix1 - ix0):
            raise UFDemoError(
                NUMERIC_NONFINITE,
                "超阈掩膜形状与窗口不匹配",
                field_path="surface.accumulate_threshold",
                actual=list(e.shape),
                requirement=f"[{iy1 - iy0}, {ix1 - ix0}]",
            )
        if not e.any():
            return 0
        win = self.threshold_exceedance_count[iy0:iy1, ix0:ix1]
        if int(win[e].max()) >= UINT32_MAX:
            raise UFDemoError(
                RESOURCE_BUDGET_EXCEEDED,
                "超阈计数溢出 uint32",
                field_path="surface.threshold_exceedance_count",
                actual=UINT32_MAX,
                requirement="计数 < 2^32",
                suggestion="改用 uint64 或分段统计。",
            )
        win[e] += 1
        self.threshold_exceeded_mask[iy0:iy1, ix0:ix1] |= e
        return int(np.count_nonzero(e))

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

        snap = {
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
        # 受限阈值观测量只在协议开启时存在；未开启不写入假数组
        if self.threshold_exceedance_count is not None:
            snap["threshold_exceedance_count"] = self.threshold_exceedance_count.astype(np.uint32)
        if self.threshold_exceeded_mask is not None:
            snap["threshold_exceeded_mask"] = self.threshold_exceeded_mask.astype(np.uint8)
        return snap
