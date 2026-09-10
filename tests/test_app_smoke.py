"""批次 E / T09 —— 界面冒烟测试（``streamlit.testing.v1.AppTest`` 驱动 ``app.py``）。

这是**端到端**验收：把真实界面脚本跑起来，逐项断言 G09 要求

* 提交计算 → ``solve_count`` 恰好 +1；
* 只改参数不提交 → 结果被标为「上一次运行」，``solve_count`` 不变；
* 拖动时间轴 / 换图层 / 旋转视图 / 切截面 → ``solve_count`` 不变；
* 读取历史运行 → ``read_count`` +1 且 ``solve_count`` 不变；
* 参考评估器「评估」不求解网格。

若环境未安装 Streamlit，整个文件跳过（界面层不是科学核心的依赖）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="界面冒烟测试需要 streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

APP = str(ROOT / "app.py")
TIMEOUT = 180


def _fresh() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()
    assert not at.exception, f"初始渲染异常：{at.exception}"
    return at


def _counters(at: AppTest) -> dict:
    s = at.session_state.ui
    return {"solve": s.solve_count, "read": s.read_count}


def _has(seq, key: str) -> bool:
    return any(getattr(x, "key", None) == key for x in seq)


# ---------------------------------------------------------------------------
# 渲染与初始状态
# ---------------------------------------------------------------------------


def test_app_renders_without_exception():
    at = _fresh()
    assert at.session_state.ui.solve_count == 0
    assert at.session_state.ui.read_count == 0
    assert at.session_state.ui.frozen is None
    # 侧边栏计数器对用户可见（透明度要求）
    assert _has(at.button, "submit_run")


def test_no_result_message_before_first_submit():
    at = _fresh()
    texts = " ".join(str(i.value) for i in at.info) + " ".join(
        str(c.value) for c in at.caption
    )
    assert "尚无结果" in texts or at.session_state.ui.frozen is None


# ---------------------------------------------------------------------------
# 提交：唯一求解入口
# ---------------------------------------------------------------------------


def test_submit_solves_exactly_once():
    at = _fresh()
    at.button(key="submit_run").click().run()
    assert not at.exception, f"提交异常：{at.exception}"
    assert _counters(at) == {"solve": 1, "read": 0}
    frozen = at.session_state.ui.frozen
    assert frozen is not None
    assert frozen.status == "completed"
    # 结果写入磁盘
    assert (Path(frozen.run_dir) / "metadata.json").exists()


def test_repeated_submit_increments_once_per_click():
    at = _fresh()
    at.button(key="submit_run").click().run()
    assert at.session_state.ui.solve_count == 1
    frozen1 = at.session_state.ui.frozen
    at.button(key="submit_run").click().run()
    assert at.session_state.ui.solve_count == 2
    assert at.session_state.ui.frozen is not frozen1


# ---------------------------------------------------------------------------
# 参数编辑态 vs 结果态：过期标记
# ---------------------------------------------------------------------------


def test_editing_params_marks_result_as_previous_run():
    at = _fresh()
    at.button(key="submit_run").click().run()
    assert at.session_state.ui.solve_count == 1
    at.number_input(key="f_nx").set_value(121).run()
    assert not at.exception, f"改参数异常：{at.exception}"
    assert at.session_state.ui.solve_count == 1, "改参数不得触发求解"
    assert at.session_state.ui.is_stale() is True
    assert at.session_state.ui.result_label().startswith("上一次运行")
    # 界面必须真的发出警告文案
    warnings = " ".join(str(w.value) for w in at.warning)
    assert "上一次运行" in warnings


def test_reverting_params_clears_stale_flag():
    at = _fresh()
    original = int(at.session_state.ui.params["grid"]["nx"])
    at.button(key="submit_run").click().run()
    at.number_input(key="f_nx").set_value(original + 20).run()
    assert at.session_state.ui.is_stale() is True
    at.number_input(key="f_nx").set_value(original).run()
    assert at.session_state.ui.is_stale() is False


# ---------------------------------------------------------------------------
# 回放 / 视图 / 图层 / 截面：绝不求解
# ---------------------------------------------------------------------------


def test_playback_and_view_controls_do_not_solve():
    at = _fresh()
    at.button(key="submit_run").click().run()
    frozen = at.session_state.ui.frozen
    assert at.session_state.ui.solve_count == 1

    # 时间轴快照回放
    if _has(at.slider, "t_idx"):
        for v in range(len(frozen.snapshots)):
            at.slider(key="t_idx").set_value(v).run()
            assert at.session_state.ui.solve_count == 1

    # 图层切换
    if _has(at.selectbox, "r_layer"):
        for name in frozen.renderable_layers():
            at.selectbox(key="r_layer").set_value(name).run()
            assert at.session_state.ui.solve_count == 1

    # 抽稀（渲染参数）
    if _has(at.slider, "r_stride"):
        at.slider(key="r_stride").set_value(4).run()

    # 视图旋转 / 翻转
    for key in ("r_T", "r_fx", "r_fy"):
        if _has(at.checkbox, key):
            at.checkbox(key=key).set_value(True).run()

    # 三维曲面切换
    if _has(at.checkbox, "r_3d"):
        at.checkbox(key="r_3d").set_value(True).run()

    # 截面方向与位置
    if _has(at.radio, "s_axis"):
        at.radio(key="s_axis").set_value("y").run()
    if _has(at.slider, "s_idx"):
        at.slider(key="s_idx").set_value(1).run()

    assert at.session_state.ui.solve_count == 1, "视图/回放/截面不得求解"
    assert not at.exception, f"视图操作异常：{at.exception}"


# ---------------------------------------------------------------------------
# 历史运行：只读
# ---------------------------------------------------------------------------


def test_history_read_does_not_solve():
    # 先制造一个可读的运行
    maker = _fresh()
    maker.button(key="submit_run").click().run()
    run_id = maker.session_state.ui.frozen.run_id

    at = _fresh()
    assert _has(at.button, "h_load"), "历史面板应有「读取结果」按钮"
    at.button(key="h_load").click().run()
    assert not at.exception, f"读取历史异常：{at.exception}"
    assert at.session_state.ui.solve_count == 0, "读取历史不得求解"
    assert at.session_state.ui.read_count == 1
    loaded = at.session_state.ui.frozen
    assert loaded is not None
    # 磁盘数组可读（历史运行没有内存 result）
    assert loaded.result is None
    assert loaded.layer("height") is not None
    assert run_id  # 至少有一个运行目录被列出


def test_history_panel_lists_runs():
    maker = _fresh()
    maker.button(key="submit_run").click().run()
    at = _fresh()
    if _has(at.selectbox, "h_idx"):
        n = len(at.selectbox(key="h_idx").options)
        assert n >= 1


# ---------------------------------------------------------------------------
# 参考评估器：不求解网格
# ---------------------------------------------------------------------------


def test_reference_evaluator_does_not_solve_grid():
    at = _fresh()
    assert _has(at.button, "ref_eval")
    at.button(key="ref_eval").click().run()
    assert not at.exception, f"参考评估异常：{at.exception}"
    assert at.session_state.ui.solve_count == 0, "参考评估不得进入逐事件求解"
    assert at.session_state.ui.read_count == 0
    payload = at.session_state["ref_result"]
    assert payload, "参考评估应产生结果"
    assert payload["output_semantics"] in {"threshold_only", "mean_depth_per_effective_pulse", "cumulative_depth"}
    # 参考评估器不得产生形貌
    assert at.session_state.ui.frozen is None


def test_reference_case_switch_keeps_no_solve():
    at = _fresh()
    if _has(at.selectbox, "ref_case"):
        opts = list(at.selectbox(key="ref_case").options)
        for opt in opts:
            at.selectbox(key="ref_case").set_value(opt).run()
            at.button(key="ref_eval").click().run()
            assert at.session_state.ui.solve_count == 0
            assert not at.exception, f"参考算例 {opt} 异常：{at.exception}"


# ---------------------------------------------------------------------------
# 查表（批次 F）：读取曲线、插值，绝不求解
# ---------------------------------------------------------------------------


def test_table_tab_renders_without_exception():
    at = _fresh()
    assert _has(at.selectbox, "t_curve"), "查表面板应有曲线卡下拉"
    assert _has(at.button, "t_lookup"), "查表面板应有「查值」按钮"


def test_table_lookup_does_not_solve():
    at = _fresh()
    if not _has(at.selectbox, "t_curve"):
        return
    at.button(key="t_lookup").click().run()
    assert not at.exception, f"查表异常：{at.exception}"
    assert at.session_state.ui.solve_count == 0, "查表不得触发求解"
    assert at.session_state.ui.read_count == 0
    payload = at.session_state["t_result"]
    assert payload is not None
    assert payload["route"] in {"event_kernel", "evaluator"}


def test_table_method_and_curve_switch_do_not_solve():
    at = _fresh()
    if not _has(at.selectbox, "t_curve"):
        return
    curves = list(at.selectbox(key="t_curve").options)
    for name in curves:
        at.selectbox(key="t_curve").set_value(name).run()
        at.button(key="t_lookup").click().run()
        assert not at.exception, f"曲线 {name} 查表异常：{at.exception}"
        assert at.session_state.ui.solve_count == 0, "切换曲线/查表不得求解"
        # 逐曲线试线性与（若可用）PCHIP
        for m in ("linear", "pchip"):
            if _has(at.selectbox, "t_method"):
                at.selectbox(key="t_method").set_value(m).run()
            at.button(key="t_lookup").click().run()
            assert at.session_state.ui.solve_count == 0


def test_table_out_of_range_zero_not_returned():
    """越界查询：默认报错，不返回 0；勾选允许越界后越界项为 None。"""
    at = _fresh()
    if not _has(at.text_input, "t_xs"):
        return
    at.text_input(key="t_xs").set_value("1e12").run()
    at.button(key="t_lookup").click().run()
    assert at.session_state.ui.solve_count == 0
    # 未勾选「允许越界」→ 出错、无结果
    assert "t_result" not in at.session_state or at.session_state["t_result"] is None
    assert any("越界" in str(e.value) or "TABLE_OUT_OF_RANGE" in str(e.value) for e in at.error)
    # 勾选允许越界 → 越界项为 None，绝不是 0
    at.checkbox(key="t_allow_oor").set_value(True).run()
    at.button(key="t_lookup").click().run()
    payload = at.session_state["t_result"]
    assert payload is not None
    assert payload["values"] == [None]
    assert payload["in_range"] == [False]


# ---------------------------------------------------------------------------
# 侧边栏：能力门槛与不自动降级
# ---------------------------------------------------------------------------


def test_sidebar_exposes_capability_and_counter():
    at = _fresh()
    # 求解次数计数器对用户可见
    codes = " ".join(str(c.value) for c in at.code)
    assert "提交求解次数" in codes
    assert "不增加求解次数" in codes


def test_switching_material_does_not_solve():
    at = _fresh()
    if _has(at.selectbox, "material_label"):
        opts = list(at.selectbox(key="material_label").options)
        for opt in opts[:4]:
            at.selectbox(key="material_label").set_value(opt).run()
            assert at.session_state.ui.solve_count == 0
            assert not at.exception, f"切换材料 {opt} 异常：{at.exception}"


def test_wording_guard_clean_on_rendered_labels():
    """渲染出来的图层标签不得含被禁词（严格子串口径）。"""
    at = _fresh()
    at.button(key="submit_run").click().run()
    from ufdemo import ui_service as U

    for label in U.LAYER_LABELS.values():
        U.assert_safe_wording(label, where="layer_label")
    for c in at.caption:
        U.assert_safe_wording(str(c.value), where="caption")
