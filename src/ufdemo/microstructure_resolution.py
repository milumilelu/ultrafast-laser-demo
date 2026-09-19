"""G-03 phase-resolution error audit.

This module does not guess CFRP fibre or AlSiC particle sizes.  It provides a
small, deterministic experiment for a caller that supplies a phase map and
phase response parameters.  The fine reference evaluates the event response
per fine cell; the coarse model uses one dominant phase and mean fluence per
coarse block.  The difference is reported as a numerical resolution error,
separate from any material-identification claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np


@dataclass(frozen=True)
class ResolutionAudit:
    fine_dx_m: float
    coarse_dx_m: float
    block_shape: tuple[int, int]
    fine_mean_increment: float
    coarse_mean_increment: float
    mean_abs_error: float
    max_abs_error: float
    relative_mean_error: float
    dominant_phase_fraction: float
    metadata: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "ufdemo.microstructure_resolution/1",
            "fine_dx_m": self.fine_dx_m,
            "coarse_dx_m": self.coarse_dx_m,
            "block_shape": list(self.block_shape),
            "fine_mean_increment": self.fine_mean_increment,
            "coarse_mean_increment": self.coarse_mean_increment,
            "mean_abs_error": self.mean_abs_error,
            "max_abs_error": self.max_abs_error,
            "relative_mean_error": self.relative_mean_error,
            "dominant_phase_fraction": self.dominant_phase_fraction,
            "metadata": dict(self.metadata),
        }


def _log_increment(fluence: np.ndarray, params: tuple[float, float]) -> np.ndarray:
    threshold, delta = (float(params[0]), float(params[1]))
    if not np.isfinite(threshold) or threshold <= 0.0 or not np.isfinite(delta) or delta <= 0.0:
        raise ValueError("phase response parameters must be finite and positive")
    out = np.zeros_like(fluence, dtype=float)
    mask = fluence > threshold
    out[mask] = delta * np.log(fluence[mask] / threshold)
    return out


def audit_phase_resolution(
    fluence: Any,
    phase_map: Any,
    *,
    fine_dx_m: float,
    coarse_dx_m: float,
    phase_parameters: Mapping[Any, tuple[float, float]],
) -> ResolutionAudit:
    """Compare fine per-phase response with a dominant-phase coarse model.

    ``coarse_dx_m`` must be an integer multiple of ``fine_dx_m`` and the
    arrays must be divisible by that block size.  No phase fraction or scale
    is inferred from a material name; all such information comes from the
    supplied ``phase_map``.
    """

    F = np.asarray(fluence, dtype=float)
    phases = np.asarray(phase_map)
    if F.ndim != 2 or phases.shape != F.shape or F.size == 0:
        raise ValueError("fluence and phase_map must be non-empty 2-D arrays with equal shape")
    if not np.all(np.isfinite(F)) or np.any(F < 0.0):
        raise ValueError("fluence must be finite and non-negative")
    fine_dx = float(fine_dx_m)
    coarse_dx = float(coarse_dx_m)
    if not np.isfinite(fine_dx) or not np.isfinite(coarse_dx) or fine_dx <= 0.0 or coarse_dx < fine_dx:
        raise ValueError("grid spacings must be finite and satisfy 0 < fine_dx <= coarse_dx")
    ratio = coarse_dx / fine_dx
    block = int(round(ratio))
    if block < 1 or not np.isclose(ratio, block, rtol=0.0, atol=1e-10):
        raise ValueError("coarse_dx_m must be an integer multiple of fine_dx_m")
    if F.shape[0] % block or F.shape[1] % block:
        raise ValueError("array shape must be divisible by coarse/fine resolution ratio")
    missing = sorted(set(np.unique(phases).tolist()) - set(phase_parameters.keys()), key=str)
    if missing:
        raise ValueError(f"phase_parameters missing phase ids: {missing}")

    fine = np.zeros_like(F, dtype=float)
    for phase, params in phase_parameters.items():
        fine += np.where(phases == phase, _log_increment(F, params), 0.0)

    coarse = np.zeros_like(F, dtype=float)
    dominant_cells = 0
    total_blocks = (F.shape[0] // block) * (F.shape[1] // block)
    for y0 in range(0, F.shape[0], block):
        for x0 in range(0, F.shape[1], block):
            ys, xs = slice(y0, y0 + block), slice(x0, x0 + block)
            block_phase = phases[ys, xs].ravel()
            values, counts = np.unique(block_phase, return_counts=True)
            dominant = values[int(np.argmax(counts))]
            dominant_cells += int(counts.max() == block * block)
            coarse_value = float(_log_increment(np.asarray([np.mean(F[ys, xs])]), phase_parameters[dominant])[0])
            coarse[ys, xs] = coarse_value

    diff = coarse - fine
    fine_mean = float(np.mean(fine))
    coarse_mean = float(np.mean(coarse))
    return ResolutionAudit(
        fine_dx_m=fine_dx,
        coarse_dx_m=coarse_dx,
        block_shape=(block, block),
        fine_mean_increment=fine_mean,
        coarse_mean_increment=coarse_mean,
        mean_abs_error=float(np.mean(np.abs(diff))),
        max_abs_error=float(np.max(np.abs(diff))),
        relative_mean_error=float(abs(coarse_mean - fine_mean) / max(abs(fine_mean), np.finfo(float).tiny)),
        dominant_phase_fraction=float(dominant_cells / max(total_blocks, 1)),
        metadata={
            "coarse_rule": "block_mean_fluence_plus_dominant_phase",
            "fine_rule": "per-cell_log_response",
            "phase_parameters_supplied": True,
            "material_microstructure_inferred": False,
        },
    )


__all__ = ["ResolutionAudit", "audit_phase_resolution"]
