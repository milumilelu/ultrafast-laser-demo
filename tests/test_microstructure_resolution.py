from __future__ import annotations

import numpy as np
import pytest

from ufdemo.microstructure_resolution import audit_phase_resolution


def test_g03_uniform_phase_has_zero_resolution_error() -> None:
    fluence = np.full((4, 4), 3.0)
    phases = np.zeros((4, 4), dtype=int)
    audit = audit_phase_resolution(
        fluence,
        phases,
        fine_dx_m=1e-6,
        coarse_dx_m=2e-6,
        phase_parameters={0: (1.0, 2e-6)},
    )
    assert audit.mean_abs_error == pytest.approx(0.0)
    assert audit.relative_mean_error == pytest.approx(0.0)
    assert audit.dominant_phase_fraction == 1.0


def test_g03_mixed_phase_reports_coarse_resolution_error_without_inference() -> None:
    fluence = np.array(
        [[1.5, 4.0, 1.5, 4.0], [4.0, 1.5, 4.0, 1.5], [1.5, 4.0, 1.5, 4.0], [4.0, 1.5, 4.0, 1.5]],
        dtype=float,
    )
    phases = np.array([[0, 1, 0, 1], [1, 0, 1, 0], [0, 1, 0, 1], [1, 0, 1, 0]], dtype=int)
    audit = audit_phase_resolution(
        fluence,
        phases,
        fine_dx_m=1e-6,
        coarse_dx_m=2e-6,
        phase_parameters={0: (1.0, 1e-6), 1: (2.0, 3e-6)},
    )
    assert audit.mean_abs_error > 0.0
    assert audit.metadata["material_microstructure_inferred"] is False
    assert audit.dominant_phase_fraction == 0.0


def test_g03_rejects_non_integral_resolution_ratio() -> None:
    with pytest.raises(ValueError, match="integer multiple"):
        audit_phase_resolution(
            np.ones((2, 2)),
            np.zeros((2, 2), dtype=int),
            fine_dx_m=1e-6,
            coarse_dx_m=1.5e-6,
            phase_parameters={0: (1.0, 1e-6)},
        )
