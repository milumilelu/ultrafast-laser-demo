from __future__ import annotations

import json

import numpy as np
import pytest

from ufdemo.observations import HeightFieldError, derive_observation, load_height_field


def test_load_height_field_preserves_nan_and_uses_physical_pixel_centres(tmp_path):
    path = tmp_path / "sample.npz"
    z = np.asarray([[0.0, -2.0, np.nan], [1.0, -1.0, -3.0]], dtype=np.float32)
    np.savez(path, z=z)
    (tmp_path / "_index.json").write_text(
        json.dumps([{"file": path.name, "shape": [2, 3], "px_um": 0.5, "material": "test", "pass_count": 2}]),
        encoding="utf-8",
    )

    field = load_height_field(path)
    assert field.z_um.shape == (2, 3)
    assert np.isnan(field.z_um[0, 2])
    assert field.meta.dx_um == pytest.approx(0.5)
    assert field.meta.dy_um == pytest.approx(0.5)
    assert field.x_um.tolist() == pytest.approx([0.25, 0.75, 1.25])
    assert field.y_um.tolist() == pytest.approx([0.25, 0.75])
    assert field.meta.pass_count == 2


def test_derive_observation_makes_sign_and_threshold_explicit(tmp_path):
    path = tmp_path / "sample.npz"
    np.savez(path, z=np.asarray([[0.0, -2.0], [1.0, -3.0]], dtype=np.float32))
    field = load_height_field(path, entry={"shape": [2, 2], "dx_um": 1.0, "dy_um": 2.0})
    summary = derive_observation(field, reference="zero", sign="depth", threshold_um=2.0)
    # Depth is -z; only the -2 and -3 pixels pass the explicit threshold.
    assert summary.n_valid == 2
    assert summary.mean_depth_um == pytest.approx(2.5)
    assert summary.area_um2 == pytest.approx(4.0)
    assert summary.volume_um3 == pytest.approx(10.0)
    assert summary.to_dict()["sign"] == "depth"


def test_unprocessed_reference_and_detrend_require_explicit_mask(tmp_path):
    path = tmp_path / "sample.npz"
    np.savez(path, z=np.asarray([[0.0, 1.0], [0.0, 1.0]], dtype=np.float32))
    field = load_height_field(path, entry={"shape": [2, 2], "dx_um": 1.0})
    with pytest.raises(HeightFieldError):
        derive_observation(field, reference="unprocessed_mask")
    mask = np.asarray([[True, False], [True, False]])
    summary = derive_observation(field, reference="unprocessed_mask", reference_mask=mask, sign="raw")
    assert summary.mean_depth_um == pytest.approx(0.5)


def test_shape_mismatch_is_rejected(tmp_path):
    path = tmp_path / "sample.npz"
    np.savez(path, z=np.zeros((2, 3), dtype=np.float32))
    with pytest.raises(HeightFieldError, match="shape"):
        load_height_field(path, entry={"shape": [3, 2], "dx_um": 1.0})
    with pytest.raises(HeightFieldError, match="单位"):
        load_height_field(path, entry={"shape": [2, 3], "dx_um": 1.0, "units": "m"})
