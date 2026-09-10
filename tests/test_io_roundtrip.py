"""导出 / 重读 / 运行状态回归（执行细则 11.1 节；批次 C 的 T08 交付）。

覆盖：稳定 JSON 与配置哈希、run_id 唯一不覆盖、NPZ 关闭 pickle、
取消与失败只保留部分结果、CSV 与界面统计一致、种子可复现。
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from conftest import ROOT, load_example, make_config
from ufdemo.errors import CONFIG_INVALID, UFDemoError
from ufdemo.io import config_hash, is_completed, load_run, save_run, stable_json
from ufdemo.materials import load_material_card
from ufdemo.solver import CancelToken, solve


def _run_single(**overrides):
    raw = load_example("analytic_single_pulse.json")
    for k, v in overrides.items():
        raw[k] = v
    cfg = make_config(raw)
    return cfg, solve(cfg, load_material_card(raw["material_card_file"]))


def test_stable_json_and_config_hash_are_order_independent():
    a = {"b": 1, "a": [2, 3]}
    b = {"a": [2, 3], "b": 1}
    assert stable_json(a) == stable_json(b)
    assert config_hash(a) == config_hash(b)


def test_stable_json_rejects_nan():
    with pytest.raises(ValueError):
        stable_json({"x": float("nan")})


def test_save_and_load_roundtrip(tmp_path):
    cfg, res = _run_single()
    res.run_id = "unit_roundtrip"
    out = tmp_path / "run1"
    saved = save_run(res, out)
    assert saved.status == "completed"
    assert is_completed(out)

    for f in ("config.json", "material_snapshot.json", "metadata.json", "statistics.csv",
              "profiles.csv", "diagnostics.json", "events.csv", "final_surface.npz",
              "snapshots/index.json"):
        assert (out / f).exists(), f

    loaded = load_run(out)
    assert loaded.status == "completed"
    assert loaded.config["material_id"] == cfg.material_id
    # CSV 与内存统计一致
    assert loaded.statistics["removal_volume_internal"] == pytest.approx(
        res.statistics["removal_volume_internal"], rel=1e-12)
    assert loaded.statistics["center_depth_internal"] == pytest.approx(
        res.statistics["center_depth_internal"], rel=1e-12)
    # 表面场重读一致
    assert loaded.surface["_source_file"] == "final_surface.npz"
    assert np.allclose(loaded.surface["height"], res.surface.height, rtol=0, atol=0)
    # 事件与快照
    assert len(loaded.events) == res.events_processed
    assert len(loaded.snapshots) == len(res.snapshots)
    # 元数据完整性
    md = loaded.metadata
    assert md["config_sha256"] == saved.config_sha256
    assert md["seed"] == cfg.seed
    assert md["code"]["source_manifest_sha256"]
    assert md["environment"]["numpy"]
    assert md["material_card_sha256"]


def test_run_dir_not_overwritten(tmp_path):
    from ufdemo.io import new_run_dir

    d = new_run_dir(tmp_path, "fixed_id")
    assert d.exists()
    with pytest.raises(UFDemoError) as ei:
        new_run_dir(tmp_path, "fixed_id")
    assert ei.value.code == CONFIG_INVALID


def test_cancelled_run_keeps_partial_surface_only(tmp_path):
    raw = load_example("ten_pulses.json")
    raw["solver"]["cancel_check_interval"] = 1
    cfg = make_config(raw)
    material = load_material_card(raw["material_card_file"])
    token = CancelToken()
    state = {"n": 0}

    def cb(info):
        state["n"] += 1
        if info.get("events_done", 0) >= 3:
            token.cancel()

    res = solve(cfg, material, cancel_token=token, progress_callback=cb)
    assert res.status == "cancelled"
    assert res.events_processed < 10

    res.run_id = "unit_cancelled"
    out = tmp_path / "cancelled"
    save_run(res, out)
    assert not (out / "final_surface.npz").exists()
    assert (out / "partial_surface.npz").exists()
    assert not is_completed(out)
    loaded = load_run(out)
    assert loaded.status == "cancelled"
    assert any("未完成" in w or "上一次运行" in w for w in loaded.warnings)
    md = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert md["events_processed"] == res.events_processed


def test_failed_run_records_errors(tmp_path):
    raw = load_example("analytic_single_pulse.json")
    raw["run_mode"] = "calibrated_case"  # 无独立验证关联 → 拒绝
    cfg = make_config(raw)
    material = load_material_card(raw["material_card_file"])
    res = solve(cfg, material)
    assert res.status == "failed"
    assert res.errors
    res.run_id = "unit_failed"
    out = tmp_path / "failed"
    save_run(res, out)
    assert not is_completed(out)
    assert not (out / "final_surface.npz").exists()


def test_npz_is_loaded_without_pickle(tmp_path):
    """重读接口必须关闭 pickle；含 object 数组的 npz 不能读入。"""
    cfg, res = _run_single()
    res.run_id = "unit_npz"
    out = tmp_path / "npz"
    save_run(res, out)
    arr = np.load(out / "final_surface.npz", allow_pickle=False)
    assert "height" in arr.files
    arr.close()

    # 仅含数值/字符串数组（不依赖 pickle）
    with np.load(out / "final_surface.npz", allow_pickle=False) as npz:
        assert all(npz[k].dtype != np.dtype(object) for k in npz.files)

    bad = tmp_path / "bad.npz"
    np.savez(bad, obj=np.array([{"a": 1}], dtype=object))
    with pytest.raises(ValueError):
        with np.load(bad, allow_pickle=False) as z:
            _ = z["obj"][0]


def test_reproducibility_with_same_seed(tmp_path):
    raw = load_example("line_scan.json")
    cfg1 = make_config(raw)
    cfg2 = make_config(raw)
    m = load_material_card(raw["material_card_file"])
    r1 = solve(cfg1, m)
    r2 = solve(cfg2, m)
    assert config_hash(r1.config_snapshot) == config_hash(r2.config_snapshot)
    assert np.array_equal(r1.surface.height, r2.surface.height)


def test_synthetic_demo_forbids_physical_depth_export():
    """合成模式禁止导出物理 μm 深度；列名使用无量纲名称。

    深度与平面几何必须用**同一个**无量纲长度尺度（任务书「合成模式的单位
    规则」）。深度是从 ``h/L_ref`` 场减得的高度差，所以标签是 ``L_ref``——
    写成 ``delta_ref`` 会与网格、ROI 差 ``L_ref/delta_ref`` 倍。
    """
    raw = load_example("synthetic_demo_point.json")
    cfg = make_config(raw, base_dir=ROOT)
    material = load_material_card(ROOT / raw["material_card_file"])
    res = solve(cfg, material)
    assert res.status == "completed", res.errors
    assert cfg.unit.mode == "dimensionless"
    assert cfg.unit.allows_physical_depth_export is False
    assert res.statistics["depth_unit"] == "L_ref"
    assert res.statistics["length_unit"] == "L_ref"
    # 无量纲量应与 SI 版解析算例同量级（同归一化参数），但不带物理单位
    assert res.statistics["center_depth_internal"] == pytest.approx(10.0, rel=1e-12)
    for p in res.profiles:
        assert p["depth_units_label"].startswith("L_ref")
