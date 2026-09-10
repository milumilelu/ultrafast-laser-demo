"""体积、ROI 与截面统计（执行细则 5.4 节、任务书 6.5 节）。

* 去除体积 ``V = sum(h0 - h) * dx * dy``（水平投影单元面积）。
* ROI 第一版按**单元中心落入掩膜**计算，并保存掩膜规则与实际面积。
  空 ROI 拒绝或返回明确不可用状态。
* 全域 ``V/A`` 与各 ROI 平均分别输出，不混用。
* 所有统计从求解场计算；显示降采样只影响展示，不影响统计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import CONFIG_INVALID, UFDemoError


@dataclass
class RoiSpec:
    name: str
    radius_m: float | None = None
    center_xy_m: tuple[float, float] | None = None
    bounds_xy_m: tuple[float, float, float, float] | None = None  # x0, x1, y0, y1

    @staticmethod
    def from_dict(raw: Mapping[str, Any], idx: int) -> "RoiSpec":
        if not isinstance(raw, Mapping):
            raise UFDemoError(CONFIG_INVALID, f"output.roi[{idx}] 必须是对象", field_path=f"output.roi[{idx}]", actual=raw)
        name = str(raw.get("name", f"roi_{idx}"))
        radius = raw.get("radius_m")
        center = raw.get("center_xy_m")
        bounds = raw.get("bounds_xy_m")
        if radius is None and bounds is None:
            raise UFDemoError(
                CONFIG_INVALID,
                f"ROI {name} 既无 radius_m 也无 bounds_xy_m",
                field_path=f"output.roi[{idx}]",
                actual=raw,
                requirement="radius_m + center_xy_m，或 bounds_xy_m",
            )
        return RoiSpec(
            name=name,
            radius_m=float(radius) if radius is not None else None,
            center_xy_m=tuple(float(v) for v in center) if center is not None else None,  # type: ignore[arg-type]
            bounds_xy_m=tuple(float(v) for v in bounds) if bounds is not None else None,  # type: ignore[arg-type]
        )


@dataclass
class RoiResult:
    name: str
    available: bool
    rule: str
    n_cells: int
    actual_area_internal: float
    mean_depth_internal: float | None
    max_depth_internal: float | None
    removal_volume_internal: float | None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "rule": self.rule,
            "n_cells": self.n_cells,
            "actual_area_internal": self.actual_area_internal,
            "mean_depth_internal": self.mean_depth_internal,
            "max_depth_internal": self.max_depth_internal,
            "removal_volume_internal": self.removal_volume_internal,
            "reason": self.reason,
        }


def removal_volume(depth: Any, dx: float, dy: float) -> float:
    import numpy as np

    return float(np.sum(np.asarray(depth, dtype=np.float64)) * dx * dy)


def domain_statistics(surface: Any) -> dict[str, Any]:
    """中心/最大深度、去除体积、全域平均。"""
    import numpy as np

    d = surface.depth
    dA = surface.grid.dx_m * surface.grid.dy_m
    A = surface.grid.domain_area_internal
    V = float(np.sum(d) * dA)
    max_depth = float(np.max(d))
    cy = surface.nearest_index_y(surface.laser.focus_xyz_m[1])
    cx = surface.nearest_index_x(surface.laser.focus_xyz_m[0])
    return {
        "cell_count": surface.grid.cell_count,
        "domain_area_internal": A,
        "removal_volume_internal": V,
        "max_depth_internal": max_depth,
        "center_depth_internal": float(d[cy, cx]),
        "mean_depth_internal": V / A,
        "n_abating_cells": int(np.count_nonzero(d > 0.0)),
        "max_cumulative_fluence_internal": float(np.max(surface.cumulative_fluence)),
        "max_illumination_count": int(np.max(surface.illumination_count)),
        "max_exposure_count": int(np.max(surface.exposure_count)),
        "height_min_internal": float(np.min(surface.height)),
        "height_max_internal": float(np.max(surface.height)),
    }


def roi_mask(surface: Any, roi: RoiSpec) -> tuple[Any, str]:
    """ROI 掩膜：按单元中心是否落入几何区域判定。"""
    import numpy as np

    XX, YY = np.meshgrid(surface.x, surface.y)
    if roi.bounds_xy_m is not None:
        x0, x1, y0, y1 = roi.bounds_xy_m
        m = (XX >= x0) & (XX <= x1) & (YY >= y0) & (YY <= y1)
        rule = "cell_centers_within_rectangular_bounds (x0<=x<=x1, y0<=y<=y1)"
    else:
        c = roi.center_xy_m or (0.0, 0.0)
        r = float(roi.radius_m)
        m = ((XX - c[0]) ** 2 + (YY - c[1]) ** 2) <= r * r
        rule = "cell_centers_within_circle (r <= radius_m)"
    return m, rule


def roi_statistics(surface: Any, rois: Any) -> list[RoiResult]:
    import numpy as np

    out: list[RoiResult] = []
    d = surface.depth
    dA = surface.grid.dx_m * surface.grid.dy_m
    for roi in rois:
        m, rule = roi_mask(surface, roi)
        n = int(np.count_nonzero(m))
        area = n * dA
        if n == 0:
            out.append(
                RoiResult(
                    name=roi.name,
                    available=False,
                    rule=rule,
                    n_cells=0,
                    actual_area_internal=0.0,
                    mean_depth_internal=None,
                    max_depth_internal=None,
                    removal_volume_internal=None,
                    reason="空 ROI：没有任何单元中心落入掩膜，返回不可用而不返回零。",
                )
            )
            continue
        vals = d[m]
        out.append(
            RoiResult(
                name=roi.name,
                available=True,
                rule=rule,
                n_cells=n,
                actual_area_internal=area,
                mean_depth_internal=float(np.mean(vals)),
                max_depth_internal=float(np.max(vals)),
                removal_volume_internal=float(np.sum(vals) * dA),
            )
        )
    return out


def cross_section(surface: Any, *, axis: str = "x", offsets_m: Any = (0.0,)) -> list[dict[str, Any]]:
    """提取截面。``axis='x'`` 表示沿 x 扫描、在给定 y 偏移处取值。"""
    out: list[dict[str, Any]] = []
    for off in offsets_m:
        if axis == "x":
            idx = surface.nearest_index_y(float(off))
            coords = surface.x
            d = surface.depth[idx, :]
            h = surface.height[idx, :]
            actual = float(surface.y[idx])
            label = f"y={actual:.6g}"
        elif axis == "y":
            idx = surface.nearest_index_x(float(off))
            coords = surface.y
            d = surface.depth[:, idx]
            h = surface.height[:, idx]
            actual = float(surface.x[idx])
            label = f"x={actual:.6g}"
        else:
            raise UFDemoError(CONFIG_INVALID, "cross_section.axis 只能是 'x' 或 'y'", field_path="output.cross_section.axis", actual=axis)
        out.append(
            {
                "axis": axis,
                "requested_offset_m": float(off),
                "actual_offset_m": actual,
                "label": label,
                "coord": [float(v) for v in coords],
                "depth": [float(v) for v in d],
                "height": [float(v) for v in h],
            }
        )
    return out
