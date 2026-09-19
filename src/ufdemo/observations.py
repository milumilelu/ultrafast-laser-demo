"""Height-field observation adapters.

The files under ``docs/experiments/height_fields`` are endpoint measurements:
they describe a measured relative surface, not an event-depth increment.  This
module keeps that distinction explicit and converts a field into reproducible
observation statistics for closed-loop validation.

No preprocessing step fills invalid pixels or infers process pairing from a
filename.  A sidecar index may provide metadata, but a caller must provide an
explicit mapping when a design-table row is needed.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .config import CONFIG_INVALID
from .errors import UFDemoError


class HeightFieldError(UFDemoError):
    """Invalid or ambiguous height-field observation."""


def _error(message: str, *, path: str, actual: Any = None, requirement: str = "") -> HeightFieldError:
    return HeightFieldError(
        CONFIG_INVALID,
        message,
        field_path=path,
        actual=actual,
        requirement=requirement,
    )


@dataclass(frozen=True)
class HeightFieldMeta:
    source_path: str
    material: str | None
    sample_id: str
    pass_count: int | None
    shape: tuple[int, int]
    dx_um: float
    dy_um: float
    units: str = "um"
    value_kind: str = "relative_height"
    valid_fraction: float = 0.0
    z_min_um: float | None = None
    z_max_um: float | None = None
    index_meta: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "material": self.material,
            "sample_id": self.sample_id,
            "pass_count": self.pass_count,
            "shape": list(self.shape),
            "dx_um": self.dx_um,
            "dy_um": self.dy_um,
            "units": self.units,
            "value_kind": self.value_kind,
            "valid_fraction": self.valid_fraction,
            "z_min_um": self.z_min_um,
            "z_max_um": self.z_max_um,
            "index_meta": dict(self.index_meta),
        }


@dataclass
class HeightFieldObservation:
    """Raw height field with its physical grid and provenance."""

    z_um: np.ndarray
    valid_mask: np.ndarray
    x_um: np.ndarray
    y_um: np.ndarray
    meta: HeightFieldMeta

    def __post_init__(self) -> None:
        if self.z_um.ndim != 2 or self.valid_mask.shape != self.z_um.shape:
            raise ValueError("z_um and valid_mask must be two-dimensional with equal shape")
        if self.x_um.shape != (self.z_um.shape[1],) or self.y_um.shape != (self.z_um.shape[0],):
            raise ValueError("x_um/y_um do not match the height-field shape")

    @property
    def shape(self) -> tuple[int, int]:
        return tuple(int(v) for v in self.z_um.shape)


@dataclass
class ObservationSummary:
    """Processed endpoint observation and scalar statistics.

    ``depth_um`` is retained as an array so a caller can compare a common
    observation operator with a simulated height field.  It is never exposed
    as a response-law increment.
    """

    depth_um: np.ndarray
    valid_mask: np.ndarray
    mean_depth_um: float | None
    median_depth_um: float | None
    p10_depth_um: float | None
    max_depth_um: float | None
    valid_fraction: float
    n_valid: int
    area_um2: float
    volume_um3: float | None
    detrend: str
    reference: str
    sign: str
    threshold_um: float | None
    erode_px: int
    roi: tuple[float, float, float, float] | None
    source_path: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mean_depth_um": self.mean_depth_um,
            "median_depth_um": self.median_depth_um,
            "p10_depth_um": self.p10_depth_um,
            "max_depth_um": self.max_depth_um,
            "valid_fraction": self.valid_fraction,
            "n_valid": self.n_valid,
            "area_um2": self.area_um2,
            "volume_um3": self.volume_um3,
            "detrend": self.detrend,
            "reference": self.reference,
            "sign": self.sign,
            "threshold_um": self.threshold_um,
            "erode_px": self.erode_px,
            "roi": list(self.roi) if self.roi else None,
            "source_path": self.source_path,
            "metadata": dict(self.metadata),
        }


def _as_optional_int(value: Any) -> int | None:
    if value is None or str(value).strip().lower() in {"", "none", "null", "nan"}:
        return None
    try:
        x = int(value)
    except (TypeError, ValueError):
        return None
    return x


def _index_entry(npz_path: Path, index_path: Path | None, entry: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if entry is not None:
        return dict(entry)
    if index_path is None:
        candidate = npz_path.parent / "_index.json"
        index_path = candidate if candidate.exists() else None
    if index_path is None:
        return {}
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error("高度场索引无法读取", path="height_field.index", actual=str(index_path)) from exc
    entries: list[Mapping[str, Any]] = []
    if isinstance(raw, list):
        entries = [v for v in raw if isinstance(v, Mapping)]
    elif isinstance(raw, Mapping):
        candidate = raw.get("entries", raw.get("files", raw))
        if isinstance(candidate, list):
            entries = [v for v in candidate if isinstance(v, Mapping)]
        elif isinstance(candidate, Mapping):
            entries = [dict(v, file=k) if isinstance(v, Mapping) else {"file": k} for k, v in candidate.items()]
    name = npz_path.name
    matches = [e for e in entries if str(e.get("file", e.get("name", ""))) == name]
    if len(matches) > 1:
        raise _error("高度场索引中存在重复文件条目", path="height_field.index", actual=name)
    return matches[0] if matches else {}


def _step(entry: Mapping[str, Any], key: str, fallback: Any = None) -> float | None:
    value = entry.get(key)
    if value is None:
        value = entry.get("px_um")
    if value is None:
        value = fallback
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def load_height_field(
    npz_path: str | Path,
    *,
    index_path: str | Path | None = None,
    entry: Mapping[str, Any] | None = None,
) -> HeightFieldObservation:
    """Load one ``.npz`` field without altering NaNs or orientation."""

    path = Path(npz_path)
    if not path.exists():
        raise _error("高度场文件不存在", path="height_field.path", actual=str(path), requirement="文件存在")
    try:
        with np.load(path, allow_pickle=False) as data:
            if "z" not in data.files:
                raise _error("高度场缺少 z 数组", path="height_field.z", actual=data.files, requirement="npz 包含 key=z")
            z = np.asarray(data["z"])
    except HeightFieldError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _error("高度场 npz 无法读取", path="height_field.path", actual=str(path)) from exc
    if z.ndim != 2 or not np.issubdtype(z.dtype, np.number):
        raise _error("高度场 z 必须是二维数值数组", path="height_field.z", actual=str(z.shape), requirement="二维 float 数组")
    z = np.asarray(z, dtype=np.float64)
    meta_entry = _index_entry(path, None if index_path is None else Path(index_path), entry)
    shape_value = meta_entry.get("shape")
    if shape_value is not None:
        try:
            declared = tuple(int(v) for v in shape_value)
        except (TypeError, ValueError) as exc:
            raise _error("高度场索引 shape 无效", path="height_field.index.shape", actual=shape_value) from exc
        if declared != tuple(z.shape):
            raise _error("高度场实际 shape 与索引不一致", path="height_field.shape", actual={"actual": list(z.shape), "declared": list(declared)})
    dx = _step(meta_entry, "dx_um")
    dy = _step(meta_entry, "dy_um", dx)
    if dx is None or dy is None:
        raise _error("高度场缺少有效像素尺寸", path="height_field.pixel_size", actual=dict(meta_entry), requirement="px_um 或 dx_um/dy_um > 0")
    units = str(meta_entry.get("units", "um")).strip().lower().replace("μ", "u").replace("µ", "u")
    if units not in {"um", "micrometer", "micrometre"}:
        raise _error(
            "高度场单位不是受支持的微米单位",
            path="height_field.units",
            actual=meta_entry.get("units"),
            requirement="um/µm；不会自动把 m 转换为 µm",
        )
    valid = np.isfinite(z)
    vals = z[valid]
    sample_id = str(meta_entry.get("sample_id", meta_entry.get("id", path.stem)))
    material = meta_entry.get("material", meta_entry.get("material_id"))
    pass_count = _as_optional_int(meta_entry.get("pass_count"))
    actual_fraction = float(np.mean(valid)) if valid.size else 0.0
    meta = HeightFieldMeta(
        source_path=str(path), material=None if material is None else str(material), sample_id=sample_id,
        pass_count=pass_count, shape=tuple(int(v) for v in z.shape), dx_um=dx, dy_um=dy,
        units="um", value_kind=str(meta_entry.get("value_kind", "relative_height")),
        valid_fraction=actual_fraction, z_min_um=(float(np.min(vals)) if vals.size else None),
        z_max_um=(float(np.max(vals)) if vals.size else None), index_meta=dict(meta_entry),
    )
    x = (np.arange(z.shape[1], dtype=np.float64) + 0.5) * dx
    y = (np.arange(z.shape[0], dtype=np.float64) + 0.5) * dy
    return HeightFieldObservation(z_um=z, valid_mask=valid, x_um=x, y_um=y, meta=meta)


def _erosion(mask: np.ndarray, iterations: int) -> np.ndarray:
    if iterations <= 0:
        return mask.copy()
    out = mask.copy()
    for _ in range(iterations):
        p = np.pad(out, 1, constant_values=False)
        out = (
            p[:-2, :-2] & p[:-2, 1:-1] & p[:-2, 2:] &
            p[1:-1, :-2] & p[1:-1, 1:-1] & p[1:-1, 2:] &
            p[2:, :-2] & p[2:, 1:-1] & p[2:, 2:]
        )
    return out


def _design_matrix(x: np.ndarray, y: np.ndarray, kind: str) -> np.ndarray:
    if kind == "linear":
        return np.column_stack((np.ones_like(x), x, y))
    if kind == "quadratic":
        return np.column_stack((np.ones_like(x), x, y, x * x, x * y, y * y))
    raise ValueError(kind)


def derive_observation(
    field: HeightFieldObservation,
    *,
    detrend: str = "none",
    reference: str = "zero",
    sign: str = "depth",
    threshold_um: float | None = None,
    erode_px: int = 0,
    roi: Sequence[float] | None = None,
    reference_mask: np.ndarray | None = None,
    reference_percentile: float = 95.0,
) -> ObservationSummary:
    """Derive a depth observation while keeping all transforms auditable.

    ``reference_mask`` is required for ``reference='unprocessed_mask'``.  A
    percentile reference is deliberately opt-in because a groove can occupy a
    large fraction of a field and make that heuristic invalid.
    """

    if detrend not in {"none", "linear", "quadratic"}:
        raise _error("不支持的去趋势方式", path="height_field.detrend", actual=detrend, requirement="none|linear|quadratic")
    if reference not in {"zero", "median", "percentile", "unprocessed_mask"}:
        raise _error("不支持的参考面方式", path="height_field.reference", actual=reference)
    if sign not in {"raw", "depth"}:
        raise _error("不支持的高度符号", path="height_field.sign", actual=sign, requirement="raw|depth")
    if erode_px < 0:
        raise _error("erode_px 不能为负", path="height_field.erode_px", actual=erode_px)
    z = np.asarray(field.z_um, dtype=np.float64).copy()
    valid = np.asarray(field.valid_mask, dtype=bool).copy() & np.isfinite(z)
    if reference == "unprocessed_mask":
        if reference_mask is None or np.asarray(reference_mask).shape != z.shape:
            raise _error("unprocessed_mask 参考面需要同形状 reference_mask", path="height_field.reference_mask", actual=None)
        ref_mask = np.asarray(reference_mask, dtype=bool) & valid
    else:
        ref_mask = valid
    ref_values = z[ref_mask]
    if reference == "zero":
        baseline = np.zeros_like(z)
    elif reference == "median":
        if not ref_values.size:
            raise _error("参考面没有有效像素", path="height_field.reference", actual=reference)
        baseline = np.full_like(z, float(np.median(ref_values)))
    elif reference == "percentile":
        if not ref_values.size or not (0.0 <= reference_percentile <= 100.0):
            raise _error("percentile 参考面参数无效", path="height_field.reference_percentile", actual=reference_percentile)
        baseline = np.full_like(z, float(np.percentile(ref_values, reference_percentile)))
    else:
        if not ref_values.size:
            raise _error("unprocessed_mask 没有有效像素", path="height_field.reference_mask", actual=0)
        baseline = np.full_like(z, float(np.median(ref_values)))
    if detrend != "none":
        yy, xx = np.meshgrid(field.y_um, field.x_um, indexing="ij")
        fit_mask = ref_mask
        if np.count_nonzero(fit_mask) < (3 if detrend == "linear" else 6):
            raise _error("去趋势参考点不足", path="height_field.detrend", actual=int(np.count_nonzero(fit_mask)))
        A = _design_matrix(xx[fit_mask], yy[fit_mask], detrend)
        coef, *_ = np.linalg.lstsq(A, z[fit_mask], rcond=None)
        baseline = _design_matrix(xx, yy, detrend) @ coef
    processed = z - baseline
    values = processed if sign == "raw" else -processed
    if roi is not None:
        if len(roi) != 4:
            raise _error("roi 必须是 (x0,x1,y0,y1)", path="height_field.roi", actual=roi)
        x0, x1, y0, y1 = (float(v) for v in roi)
        if not (x1 > x0 and y1 > y0):
            raise _error("roi 边界必须递增", path="height_field.roi", actual=roi)
        yy, xx = np.meshgrid(field.y_um, field.x_um, indexing="ij")
        valid &= (xx >= x0) & (xx < x1) & (yy >= y0) & (yy < y1)
    if threshold_um is not None:
        if not math.isfinite(float(threshold_um)):
            raise _error("threshold_um 必须有限", path="height_field.threshold_um", actual=threshold_um)
        valid &= values >= float(threshold_um)
    valid = _erosion(valid, int(erode_px))
    flat = values[valid]
    total = int(values.size)
    n = int(flat.size)
    area = float(n * field.meta.dx_um * field.meta.dy_um)
    return ObservationSummary(
        depth_um=values,
        valid_mask=valid,
        mean_depth_um=(float(np.mean(flat)) if n else None),
        median_depth_um=(float(np.median(flat)) if n else None),
        p10_depth_um=(float(np.percentile(flat, 10.0)) if n else None),
        max_depth_um=(float(np.max(flat)) if n else None),
        valid_fraction=(float(n / total) if total else 0.0), n_valid=n,
        area_um2=area, volume_um3=(float(np.sum(flat) * field.meta.dx_um * field.meta.dy_um) if n else None),
        detrend=detrend, reference=reference, sign=sign,
        threshold_um=None if threshold_um is None else float(threshold_um), erode_px=int(erode_px),
        roi=None if roi is None else tuple(float(v) for v in roi), source_path=field.meta.source_path,
        metadata={"field_meta": field.meta.to_dict(), "raw_value_kind": field.meta.value_kind},
    )


__all__ = [
    "HeightFieldError", "HeightFieldMeta", "HeightFieldObservation", "ObservationSummary",
    "load_height_field", "derive_observation",
]
