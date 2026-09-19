"""U08：文献算例回放（陶瓷/玻璃）回归。

守三件事：

1. **回放 ≠ 预测** —— 4 组算例的实测值/条件/来源逐条可见，且不经过模型；
2. **等效脉冲数不得被表述成真实脉冲数**（措辞守卫，严格子串口径）；
3. **不得由端点造假三维形貌** —— 无 3D 图层；若有重建必须带强制标签；
   且**不存在**「累计深度 ÷ N 当作脉冲核」的路径。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from ufdemo.cases import (
    EQUIVALENT_PULSE_LABEL,
    RECONSTRUCTION_LABEL,
    CaseReplay,
    assert_cases_are_replay_only,
    assert_no_unlabeled_reconstruction,
    assert_pulse_count_wording,
    load_ceramics_cases,
)
from ufdemo.errors import CONFIG_INVALID, UFDemoError

ROOT = Path(__file__).resolve().parents[1]
MEASURED = ROOT / "data" / "measured"
CASES_CSV = MEASURED / "ceramics_dot_line_measured.csv"

pytestmark = pytest.mark.u08


@pytest.fixture(scope="module")
def replay() -> CaseReplay:
    if not CASES_CSV.exists():
        pytest.skip("陶瓷数据未导入")
    return CaseReplay.from_dir(MEASURED)


# ---------------------------------------------------------------------------
# 1. 数据：4 组，条件与来源齐备
# ---------------------------------------------------------------------------


def test_four_cases_loaded(replay):
    """**行数仍为 4** —— 不得为了「更完整」而在端点数据里塞 3D 行。"""
    assert len(replay.cases) == 4, [c.case_id for c in replay.cases]


def test_cases_have_conditions_and_locator(replay):
    """实测值、条件、来源在数据里**逐条可见**（含 Table 1 / Table 2 定位）。"""
    for c in replay.cases:
        assert c.source_doi == "10.3390/ma15103614"
        assert c.source_locator.startswith("Table "), c.source_locator
        assert c.peak_fluence_J_cm2 > 0
        assert c.depth_um > 0
        assert c.conditions_text()
        assert "J/cm²" in c.conditions_text()


def test_expected_values_present(replay):
    """抽查关键实测值（与论文表格一致）。"""
    by_id = {c.case_id: c for c in replay.cases}
    assert by_id["D02-glass_dot"].depth_um == pytest.approx(6.3)
    assert by_id["D02-glass_dot"].peak_fluence_J_cm2 == pytest.approx(8.0)
    assert by_id["D02-glass_dot"].pulse_count == pytest.approx(10)
    assert by_id["D02-zirconia_dot"].depth_um == pytest.approx(7.4)
    assert by_id["D02-glass_cross"].depth_um == pytest.approx(8.8)
    assert by_id["D02-zirconia_cross"].depth_um == pytest.approx(7.1)


# ---------------------------------------------------------------------------
# 2. 措辞守卫：等效脉冲数不是脉冲数
# ---------------------------------------------------------------------------


def test_equivalent_pulse_label_is_forced(replay):
    """交叉线算例的计数标签必须是带「等效」与「非真实事件序列」的强制标签。"""
    cross = [c for c in replay.cases if c.equivalent_pulse_count is not None]
    assert cross, "应有交叉线算例带等效脉冲数"
    for c in cross:
        assert c.pulse_count_label == EQUIVALENT_PULSE_LABEL
        assert "等效" in c.pulse_count_label
        assert "非真实事件序列" in c.pulse_count_label
        # 展示文本里也必须带「等效」
        assert "等效" in c.conditions_text()


def test_wording_guard_rejects_plain_pulse_count():
    """**把等效值写成真实脉冲数必须被拒** —— 这是本项最容易犯的错。"""
    for bad in ("真实脉冲数", "实际脉冲数", "脉冲数（实测）"):
        with pytest.raises(UFDemoError) as ei:
            assert_pulse_count_wording(bad)
        assert ei.value.code == CONFIG_INVALID


def test_wording_guard_requires_explicit_equivalence():
    """缺「等效」或缺「非真实事件序列」都要拒 —— 只写「脉冲数 20」不够。"""
    with pytest.raises(UFDemoError):
        assert_pulse_count_wording("脉冲数 20")
    with pytest.raises(UFDemoError):
        assert_pulse_count_wording("等效脉冲数 20")  # 缺「非真实事件序列」
    assert_pulse_count_wording(EQUIVALENT_PULSE_LABEL)  # 合规写法通过


# ---------------------------------------------------------------------------
# 3. 三维形貌红线
# ---------------------------------------------------------------------------


def test_replay_has_no_3d_heightfield(replay):
    """回放**不含**三维形貌：只有端点数据。"""
    d = replay.to_dict()
    assert d["has3dHeightfield"] is False
    for banned in ("height", "heightfield", "surface", "depthMap", "profile"):
        assert banned not in d, f"回放载荷里出现了形貌字段 {banned!r}"


def test_reconstruction_label_is_enforced_when_present():
    """若存在重建图层，标签**必须**含「假设截面形状重建」。"""
    assert_no_unlabeled_reconstruction(None)          # 无重建 → 不抛
    assert_no_unlabeled_reconstruction(RECONSTRUCTION_LABEL)  # 带标签 → 不抛
    with pytest.raises(UFDemoError) as ei:
        assert_no_unlabeled_reconstruction("重建形貌")
    assert ei.value.code == CONFIG_INVALID


def test_replay_cases_have_no_increment_access(replay):
    """回放算例一律**无**增量权限（端点几何不是逐事件增量）。"""
    assert_cases_are_replay_only(replay.cases)  # 不抛
    assert all(c.increment_access is False for c in replay.cases)
    assert all(c.observation_access is True for c in replay.cases)


def test_no_cumulative_over_n_path_in_source():
    """**不得**存在「累计深度 ÷ N 当作脉冲核」的代码路径。

    这里用源码级检查：``cases.py`` 里不得出现除以脉冲计数的写法。
    它比运行断言更能防住「以后有人加回来」。
    """
    src = (ROOT / "src" / "ufdemo" / "cases.py").read_text(encoding="utf-8")
    for bad in ("depth_um / ", "depth / ", "/ equivalent_pulse_count", "/ pulse_count"):
        assert bad not in src, f"cases.py 出现了疑似「累计 ÷ N」的写法：{bad!r}"


# ---------------------------------------------------------------------------
# 4. 与权限注册表一致（U05）
# ---------------------------------------------------------------------------


def test_cases_consistent_with_permission_registry(replay):
    """这 4 条在 U05 注册表里也必须是「可观测、无增量权限」。"""
    reg_path = MEASURED / "registry.json"
    if not reg_path.exists():
        pytest.skip("注册表未生成")
    import json

    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    by_id = {d["case_id"]: d for d in reg["datasets"]}
    for c in replay.cases:
        entry = by_id.get(c.case_id)
        assert entry is not None, f"{c.case_id} 不在注册表里"
        assert entry["observation_access"] is True
        assert entry["increment_access"] is False


# ---------------------------------------------------------------------------
# 5. 报告
# ---------------------------------------------------------------------------


def test_report_exists_and_states_boundaries():
    md = ROOT / "docs" / "reports" / "ceramics_cases.md"
    if not md.exists():
        pytest.skip("报告尚未生成（先跑 tools/ceramics_cases_report.py）")
    text = md.read_text(encoding="utf-8")
    assert "已知条件回放" in text or "不经过模型" in text
    assert "等效脉冲数" in text and "不是真实脉冲数" in text
    assert RECONSTRUCTION_LABEL in text
    assert "increment_access" in text


# 第 6 节「探针超时路径」的两条检查已随 U03 探针删除（2026-09-19，唯一界面收敛，ADR-0023）。
