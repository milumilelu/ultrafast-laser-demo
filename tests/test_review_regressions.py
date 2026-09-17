"""审查发现的非零时钟、离焦、单位和持久化回归。"""
import ast
import builtins
import copy
import math

import numpy as np
import pytest

from conftest import load_example, make_config, ROOT
from ufdemo.beam import beam_patch, BeamOptions, peak_fluence
from ufdemo.config import estimate_events
from ufdemo.materials import load_material_card
from ufdemo.paths import iter_events
from ufdemo.solver import solve
from ufdemo.surface import SurfaceState


def test_nonzero_clock_origin():
    raw = load_example("ten_pulses.json")
    raw["path"]["t0_s"] = 2.0005
    for seg in raw["path"]["segments"]:
        seg["start_s"] += 2.0005
        seg["end_s"] += 2.0005
    cfg = make_config(raw)
    events = list(iter_events(cfg.path, cfg.laser))
    assert len(events) == 10
    assert events[0].time_s == pytest.approx(2.0005)
    assert estimate_events(cfg.path, cfg.laser) == len(events)


def test_fixed_geometry_ignores_current_height_with_finite_rayleigh_range():
    raw = load_example("analytic_single_pulse.json")
    raw["laser"]["rayleigh_range_m"] = 1e-5
    cfg = make_config(raw)
    surface = SurfaceState.initialize(cfg.grid, cfg.laser)
    event = next(iter_events(cfg.path, cfg.laser))
    before = beam_patch(event, surface, BeamOptions())
    surface.height[:] -= 1e-4
    after = beam_patch(event, surface, BeamOptions())
    np.testing.assert_array_equal(before.fluence, after.fluence)


def test_defocus_uses_local_heights_and_expanded_window():
    raw = load_example("analytic_single_pulse.json")
    raw["laser"]["rayleigh_range_m"] = 1e-5
    raw["grid"].update(nx=401, ny=401, dx_m=1e-6, dy_m=1e-6)
    cfg = make_config(raw)
    surface = SurfaceState.initialize(cfg.grid, cfg.laser)
    surface.height[:] = -3e-5
    surface.refresh_height_extrema()   # 绕过了 apply_increment ⇒ 极值须刷新
    event = next(iter_events(cfg.path, cfg.laser))
    patch = beam_patch(event, surface, BeamOptions(geometry_feedback="axial_defocus"))
    assert patch.estimated_intercepted_energy_J == pytest.approx(event.energy_J, rel=1e-6)
    assert patch.spot_radius == pytest.approx(cfg.laser.spot_radius_m * math.sqrt(10))
    surface.height[200, 200] = 0
    surface.refresh_height_extrema()
    patch = beam_patch(event, surface, BeamOptions(geometry_feedback="axial_defocus"))
    assert patch.fluence[200-patch.iy0, 200-patch.ix0] == pytest.approx(
        peak_fluence(event.energy_J, cfg.laser.spot_radius_m))


def test_synthetic_depth_and_roi_use_one_length_scale():
    raw = load_example("synthetic_demo_point.json")
    cfg = make_config(raw)
    card = load_material_card(ROOT / raw["material_card_file"])
    result = solve(cfg, card)
    assert result.ok, result.errors
    assert result.statistics["center_depth_internal"] == pytest.approx(10.0)
    assert result.statistics["depth_unit"] == "L_ref"
    assert result.rois[0]["n_cells"] > 1000


def test_pass_snapshots_are_end_of_pass_and_obey_limit(fixture_card):
    raw = load_example("ten_pulses.json")
    first = copy.deepcopy(raw["path"]["segments"][0])
    first.update(start_s=0, end_s=.003, pass_id=0)
    second = copy.deepcopy(first)
    second.update(start_s=.003, end_s=.006, pass_id=1)
    raw["path"]["segments"] = [first, second]
    raw["output"].update(snapshot_policy="passes", snapshot_every_n_passes=1, max_snapshots=2)
    result = solve(make_config(raw), fixture_card)
    # 逐遍策略在**每遍最后一个事件**记录：第 1 遍末 = 事件 2，第 2 遍末 = 事件 5；
    # 第 2 遍末与收尾快照同处一个事件，因此 index 列表尾部出现两次 5。
    assert [s["event_index"] for s in result.snapshots] == [2, 5, 5]
    center = result.surface.height.shape[0] // 2
    # 3 个脉冲 × 2e-7 = 6e-7：这是第 1 遍**结束后**的量，不是该遍首个脉冲后的 2e-7。
    assert result.snapshots[0]["depth"][center, center] == pytest.approx(6e-7)
    assert result.snapshots[-1]["final"] is True
    raw["output"].update(snapshot_policy="none", max_snapshots=0, max_snapshot_bytes=0)
    none_result = solve(make_config(raw), fixture_card)
    # none 只禁止中间快照；求解器始终保留一个「最终」快照（与 UI 侧语义一致）。
    assert len(none_result.snapshots) == 1
    assert none_result.snapshots[0]["final"] is True

    # max_snapshots 只约束逐遍快照，收尾快照始终追加且永远排在最后。
    # 3 个 pass、每 pass 都抓 → 事件 2/5/8 三个候选；上限为 1 时只保留第一个候选。
    third = copy.deepcopy(first)
    third.update(start_s=.006, end_s=.009, pass_id=2)
    raw["path"]["segments"] = [first, second, third]
    raw["output"].update(
        snapshot_policy="passes", snapshot_every_n_passes=1, max_snapshots=1, max_snapshot_bytes=134217728
    )
    capped = solve(make_config(raw), fixture_card)
    policy = [s for s in capped.snapshots if not s.get("final")]
    assert [s["event_index"] for s in policy] == [2]
    assert capped.snapshots[-1]["final"] is True
    assert capped.snapshots[-1]["event_index"] == 8

def test_local_visibility_uses_global_surface_for_upstream_occluders():
    from types import SimpleNamespace
    from ufdemo.geometry import first_intersection_visibility
    g = SimpleNamespace(nx=21, ny=5, dx_m=1.0, dy_m=1.0, center_x_m=0.0, center_y_m=0.0)
    h = np.zeros((5, 21)); h[:, 13:] = 10.0
    k = (math.sin(math.pi / 3), 0.0, math.cos(math.pi / 3))
    local = first_intersection_visibility(h[:, 9:12], g, k, section=(1, 4, 9, 12), global_height=h)
    full = first_intersection_visibility(h, g, k, section=(1, 4, 9, 12))
    assert np.array_equal(local, full)


# ---------------------------------------------------------------------------
# 错误码常量必须真的被导入
#
# 2026-09-17 审查发现：``webcontract.py`` 有 12 处、``config.py`` 有 1 处
# ``UFDemoError(<错误码>, ...)`` 用到的常量**从未 import**。这些都在*错误路径*上，
# 所以正常运行时全绿、测试也全绿；一旦真的触发，抛的却是 ``NameError`` 而不是
# 结构化错误 —— 于是「能力不足/参数不匹配」被伪装成「程序崩了」，
# 恰好违背本项目「宁可拒绝，不可静默降级」的红线。
# 本测试用静态扫描把这类问题挡在提交之前，不需要额外依赖。
# ---------------------------------------------------------------------------

_ERROR_FACTORY = "UFDemoError"


def _bound_names(tree):
    """模块内所有被绑定的名字（保守超集：import、赋值、def/class、形参、except as…）。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchAs) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchStar) and node.name:
            names.add(node.name)
    return names


def _unbound_error_codes(path):
    """返回 ``[(行号, 名字), …]``：``UFDemoError`` 首个实参是未绑定名字的位置。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bound = _bound_names(tree) | set(dir(builtins))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == _ERROR_FACTORY:
            a0 = node.args[0]
            if isinstance(a0, ast.Name) and a0.id not in bound:
                bad.append((node.lineno, a0.id))
    return bad


def test_error_code_constants_are_imported():
    offenders = {}
    for py in sorted((ROOT / "src" / "ufdemo").glob("*.py")):
        bad = _unbound_error_codes(py)
        if bad:
            offenders[py.name] = bad
    assert not offenders, (
        "以下 UFDemoError(...) 的第一个参数是未绑定名字，触发时会抛 NameError "
        "而不是结构化错误码：" + repr(offenders)
    )


def test_scanner_detects_missing_error_code_import(tmp_path):
    """自检：扫描器必须能识别「漏导入错误码」这种写法，避免测试退化成恒真。"""
    bad_src = (
        "from ufdemo.errors import UFDemoError\n"
        "raise UFDemoError(NOT_IMPORTED_CODE, 'boom')\n"
    )
    p = tmp_path / "bad.py"
    p.write_text(bad_src, encoding="utf-8")
    assert _unbound_error_codes(p) == [(2, "NOT_IMPORTED_CODE")]

    good_src = (
        "from ufdemo.errors import CONFIG_INVALID, UFDemoError\n"
        "raise UFDemoError(CONFIG_INVALID, 'boom')\n"
    )
    q = tmp_path / "good.py"
    q.write_text(good_src, encoding="utf-8")
    assert _unbound_error_codes(q) == []
