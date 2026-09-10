"""超快激光加工 Demo —— Streamlit 界面（批次 E / T09）。

启动::

    streamlit run app.py

设计约束（执行细则第 3、11.3 节，任务书第 13 节）：

* **本文件可以导入 Streamlit；科学核心不导入**。所有求解/读取逻辑都在
  ``ufdemo.ui_service`` 与 ``ufdemo.solver``，界面只做渲染与事件转发。
* **参数编辑态与结果态分离**：可编辑参数在 ``st.session_state.ui.params``，
  已提交结果在 ``st.session_state.ui.frozen``；图表一律读 ``frozen``，
  不读当前表单值。
* **提交才求解**：只有「提交计算」按钮会调用求解器；切换模板、拖动时间轴、
  旋转/翻转视图、切换图层、切截面都只读已有数组（界面显示求解次数计数器）。
* **缺参数就显示不可用原因**，并可**显式**选择合成示例；不自动降级、不静默填值。
* 每个结果持续显示材料身份／模式／单位／证据状态；剂量不写“温度”，
  阈值标记不写“热影响区”。
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import plotly.graph_objects as go  # noqa: E402

from ufdemo import __version__, default_curves_dir, default_material_dir, default_runs_dir, project_root  # noqa: E402
from ufdemo import ui_service as U  # noqa: E402
from ufdemo import references as REF  # noqa: E402
from ufdemo import tables as T  # noqa: E402
from ufdemo.errors import UFDemoError  # noqa: E402
from ufdemo.io import code_version  # noqa: E402
from ufdemo.materials import load_material_card, load_material_catalog  # noqa: E402

EXAMPLES = ROOT / "examples"
DEFAULT_TEMPLATE = "analytic_single_pulse.json"
REFERENCE_CASES = ("ysz_reference_case.json", "sic_reference_case.json")


# ---------------------------------------------------------------------------
# 会话状态
# ---------------------------------------------------------------------------


def _state() -> U.SessionState:
    if "ui" not in st.session_state:
        st.session_state.ui = U.new_session(U.load_template(EXAMPLES, DEFAULT_TEMPLATE))
    if "active_material" not in st.session_state:
        st.session_state.active_material = "analytic_fixture_not_a_material"
    return st.session_state.ui


@st.cache_data(show_spinner=False)
def _example_names() -> list[str]:
    return U.list_examples(EXAMPLES)


@st.cache_data(show_spinner=False)
def _catalog_rows() -> list[dict]:
    cat = load_material_catalog(default_material_dir())
    rows = []
    for mid, spec in sorted(cat.items()):
        rows.append(
            {
                "material_id": mid,
                "family": spec.identity.get("family"),
                "grade": spec.identity.get("grade"),
                "evidence_status": spec.evidence_status,
                "allowed_run_modes": ",".join(spec.allowed_run_modes),
                "physical": spec.is_physical(),
            }
        )
    return rows


def _material_choices() -> dict[str, str]:
    """label → material_id。含人工 fixture（明确标注为解析测试卡）。"""
    out: dict[str, str] = {}
    for r in _catalog_rows():
        label = f"{r['material_id']}｜{r['family']}／{r['grade']}｜证据 {r['evidence_status']}"
        out[label] = r["material_id"]
    fx = ROOT / "tests" / "fixtures" / "analytic_fixture.json"
    if fx.exists():
        spec = load_material_card(fx)
        label = f"{spec.id}｜{spec.identity.get('family')}｜人工解析 fixture（非材料）"
        out[label] = spec.id
    return out


def _resolve_material(material_id: str):
    if material_id == "analytic_fixture_not_a_material":
        return load_material_card(ROOT / "tests" / "fixtures" / "analytic_fixture.json")
    cat = load_material_catalog(default_material_dir())
    if material_id not in cat:
        raise UFDemoError(
            "MATERIAL_CAPABILITY_MISSING",
            f"材料目录中找不到 id={material_id!r}",
            field_path="material_id",
            actual=material_id,
            requirement=f"可用：{sorted(cat)}",
        )
    return cat[material_id]


def _watermark_block(wm: dict, *, where: str) -> None:
    U.assert_safe_wording(U.format_watermark(wm), where=where)
    st.caption("｜".join(str(v) for v in (
        wm.get("material_id"),
        f"{wm.get('family')}／{wm.get('grade')}",
        f"模式 {wm.get('run_mode')}",
        f"单位 {wm.get('unit_system')}",
        f"证据 {wm.get('evidence_status')}",
        "物理预测" if wm.get("physical_prediction_allowed") else "非物理预测",
    )))
    for w in wm.get("warnings") or []:
        st.caption(f"⚠ {w}")


# ---------------------------------------------------------------------------
# 侧边栏：材料、模式、能力
# ---------------------------------------------------------------------------


def _sidebar(state: U.SessionState):
    st.sidebar.title("超快激光加工 Demo")
    st.sidebar.caption(f"ufdemo {__version__}（批次 A–E）")

    choices = _material_choices()
    labels = list(choices.keys())
    default_idx = next(
        (i for i, l in enumerate(labels) if choices[l] == st.session_state.active_material), 0
    )
    chosen = st.sidebar.selectbox("材料卡", labels, index=default_idx, key="material_label")
    material_id = choices[chosen]
    if material_id != st.session_state.active_material:
        st.session_state.active_material = material_id
        st.session_state.pop("ref_result", None)

    material = _resolve_material(material_id)

    st.sidebar.markdown("**能力与限制**")
    cap_rows = [
        {
            "能力": name,
            "可用": "是" if cap.available else "否",
            "说明": cap.reason + (f"（缺 {', '.join(cap.missing)}）" if cap.missing else ""),
        }
        for name, cap in sorted(material.capabilities.items())
    ]
    st.sidebar.dataframe(cap_rows, hide_index=True, height=210)

    allowed = list(material.allowed_run_modes) or ["threshold_only"]
    run_mode = st.sidebar.selectbox("运行模式", allowed, key="run_mode")

    ok, reason = U.capability_gate(material, run_mode)
    if ok and material.is_physical():
        st.sidebar.success(reason)
    elif ok:
        st.sidebar.warning(f"{reason}（证据状态：{material.evidence_status}）")
    else:
        st.sidebar.error(f"不可用：{reason}")
        offer = U.suggest_synthetic(material, run_mode)
        if offer:
            st.sidebar.warning(offer["warning"])
            if st.sidebar.button("改用合成示例（需显式选择）", key="offer_synthetic"):
                for c in U.synthetic_choices():
                    if c["id"] == "synthetic_demo_point":
                        state.params = U.load_template(EXAMPLES, "synthetic_demo_point.json")
                        st.session_state.active_material = "synthetic_demo_isotropic"
                        st.rerun()

    st.sidebar.divider()
    st.sidebar.markdown("**求解次数（透明度）**")
    c = state.counters()
    st.sidebar.code(
        "\n".join(
            [
                f"提交求解次数 : {c['solve_count']}",
                f"读取历史次数 : {c['read_existing_count']}",
                "视图/图层/时间轴/截面：不增加求解次数",
            ]
        ),
        language="text",
    )
    st.sidebar.caption("只有「提交计算」会调用求解器；其余操作读取已有数组。")
    return material, run_mode


# ---------------------------------------------------------------------------
# 参数区
# ---------------------------------------------------------------------------

# 参数控件的 key：重置模板时一并清除，让控件重新读取模板默认值。
PARAM_WIDGET_KEYS = (
    "f_nx", "f_ny", "f_dx", "f_dy",
    "f_E", "f_w0", "f_f",
    "f_v", "f_x0", "f_x1",
    "f_roi", "f_snap",
)


def _reset_param_widgets(template_name: str) -> None:
    st.session_state.ui.params = U.load_template(EXAMPLES, template_name)
    for k in PARAM_WIDGET_KEYS:
        st.session_state.pop(k, None)


def _param_panel(state: U.SessionState, material, run_mode: str):
    st.subheader("参数与运行（只有「提交计算」会求解）")

    names = _example_names()
    t_idx = names.index(DEFAULT_TEMPLATE) if DEFAULT_TEMPLATE in names else 0
    tmpl = st.selectbox("配置模板（切换模板会重置参数）", names, index=t_idx, key="template")
    if st.button("载入/重置为该模板", key="reset_template"):
        _reset_param_widgets(tmpl)
        st.rerun()

    p = state.params
    G = U.get_path

    st.markdown("**网格**")
    c1, c2, c3, c4 = st.columns(4)
    nx = c1.number_input("nx", 3, 801, int(G(p, "grid.nx", 161)), key="f_nx")
    ny = c2.number_input("ny", 3, 801, int(G(p, "grid.ny", 161)), key="f_ny")
    dx_um = c3.number_input("dx (µm)", 0.01, 100.0, float(G(p, "grid.dx_m", 5e-7)) * 1e6, key="f_dx")
    dy_um = c4.number_input("dy (µm)", 0.01, 100.0, float(G(p, "grid.dy_m", 5e-7)) * 1e6, key="f_dy")

    st.markdown("**光束**")
    c1, c2, c3 = st.columns(3)
    e_uJ = c1.number_input("单脉冲能量 (µJ)", 1e-6, 1e6, float(G(p, "laser.pulse_energy_J", 1e-5)) * 1e6, key="f_E")
    w0_um = c2.number_input("光斑半径 w0 (µm)", 0.01, 500.0, float(G(p, "laser.spot_radius_m", 1e-5)) * 1e6, key="f_w0")
    freq = c3.number_input("重复频率 (Hz)", 1.0, 1e7, float(G(p, "laser.repetition_rate_Hz", 1000.0)), key="f_f")

    st.markdown("**路径（首段）**")
    c1, c2, c3 = st.columns(3)
    speed = c1.number_input("速度 (m/s)", 0.0, 100.0, float(G(p, "path.segments.0.speed_m_s", 0.0)), key="f_v")
    start = c2.number_input("起点 x (µm)", -1e4, 1e4, float(G(p, "path.segments.0.start_xyz_m.0", 0.0)) * 1e6, key="f_x0")
    end = c3.number_input("终点 x (µm)", -1e4, 1e4, float(G(p, "path.segments.0.end_xyz_m.0", 0.0)) * 1e6, key="f_x1")

    st.markdown("**输出**")
    c1, c2 = st.columns(2)
    roi_um = c1.number_input("ROI 半径 (µm)", 0.0, 1e4, float(G(p, "output.roi.0.radius_m", 1e-5)) * 1e6, key="f_roi")
    snap_opts = ("events", "passes", "none")
    cur_snap = str(G(p, "output.snapshot_policy", "events"))
    snap_policy = c2.selectbox("快照策略", snap_opts, index=snap_opts.index(cur_snap) if cur_snap in snap_opts else 0, key="f_snap")

    overrides = {
        "grid.nx": int(nx),
        "grid.ny": int(ny),
        "grid.dx_m": dx_um * 1e-6,
        "grid.dy_m": dy_um * 1e-6,
        "laser.pulse_energy_J": e_uJ * 1e-6,
        "laser.spot_radius_m": w0_um * 1e-6,
        "laser.repetition_rate_Hz": float(freq),
        "path.segments.0.speed_m_s": float(speed),
        "path.segments.0.start_xyz_m": [start * 1e-6, 0.0, 0.0],
        "path.segments.0.end_xyz_m": [end * 1e-6, 0.0, 0.0],
        "output.snapshot_policy": snap_policy,
        "output.roi.0.radius_m": roi_um * 1e-6,
    }
    base = U.load_template(EXAMPLES, tmpl)
    base["run_mode"] = run_mode
    base["material_id"] = material.id
    # 表单当前值 → 编辑态（不求解）。这样「改了但没提交」也能被识别出来。
    U.set_pending_params(state, U.apply_overrides(base, overrides))

    submitted = st.button("提交计算", type="primary", key="submit_run")
    if not submitted:
        if state.frozen is not None and state.is_stale():
            st.warning(
                "参数已修改但**尚未提交**：结果区显示的是"
                f"「上一次运行」（{state.submitted_at}），不是这些新参数的结果。"
            )
        return

    try:
        with st.spinner("正在求解…"):
            frozen = U.submit(
                state,
                material,
                out_base=default_runs_dir(),
                project_root=project_root(),
                label="ui_run",
                code_info=code_version(project_root()),
            )
        st.success(f"已提交并完成：{frozen.run_id}（求解次数 {state.solve_count}）")
    except UFDemoError as err:
        st.error(err.format_human())
    st.rerun()


# ---------------------------------------------------------------------------
# 结果区：形貌 / 截面 / 时间轴
# ---------------------------------------------------------------------------


def _result_panel(state: U.SessionState):
    frozen = state.frozen
    if frozen is None:
        st.info("尚无结果。请在「参数与运行」里提交计算，或在「历史运行」里读取已有结果。")
        return

    if state.is_stale():
        st.warning(
            f"当前参数已修改，下方显示的是 **{state.result_label()}**；"
            "要按新参数出结果请重新「提交计算」。"
        )
    else:
        st.success(f"结果状态：{state.result_label()}")

    _watermark_block(frozen.material_watermark, where="result_header")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("状态", U.STATUS_ZH.get(frozen.status, frozen.status))
    c2.metric("事件", f"{frozen.events_processed}/{frozen.events_total}")
    st_ = frozen.statistics or {}
    if frozen.removal_available:
        c3.metric("去除体积", f"{st_.get('removal_volume_internal'):.6g}" if st_.get("removal_volume_internal") is not None else "不提供")
        c4.metric("中心深度", f"{st_.get('center_depth_internal'):.6g}" if st_.get("center_depth_internal") is not None else "不提供")
    else:
        c3.metric("去除量", "不提供（threshold_only）")
        c4.metric("深度", "不提供")
    st.caption(f"单位：长度 {frozen.material_watermark.get('length_label')}｜"
               f"深度 {frozen.material_watermark.get('depth_label')}｜"
               f"能流 {frozen.material_watermark.get('fluence_label')}"
               f"（CSV 与界面统计同源）")

    n_snap = len(frozen.snapshots)
    tab3d, tabsec, tabtl = st.tabs(["形貌", "截面", "时间轴"])

    avail = frozen.available_layers()
    layer_names = [k for k, v in avail.items() if v is None]
    blocked = {k: v for k, v in avail.items() if v is not None}
    if not layer_names:
        tabs_hint = "该来源没有可渲染的数组（可能未保存表面，或结果为 threshold_only）。"
        with tab3d:
            st.info(tabs_hint)
        with tabsec:
            st.info(tabs_hint)
        with tabtl:
            st.info(tabs_hint)
        _result_footers(frozen)
        return

    with tab3d:
        c1, c2, c3 = st.columns([2, 1, 1])
        cur_layer = st.session_state.get("r_layer")
        idx_layer = layer_names.index(cur_layer) if cur_layer in layer_names else 0
        chosen_layer = c1.selectbox("图层", layer_names, index=idx_layer, key="r_layer")
        show3d = c2.checkbox("三维曲面", value=False, key="r_3d")
        stride = c3.slider("抽稀步长", 1, 8, 2, key="r_stride")
        for name, why in blocked.items():
            st.caption(f"图层「{U.LAYER_LABELS[name]}」不可用：{why}")

        arr = U.layer_view(state, chosen_layer, None)
        if arr is None:
            st.info("该图层当前不可用。")
        else:
            arr = U.orient_view(
                arr,
                transpose=st.checkbox("转置（视图旋转）", key="r_T"),
                flip_x=st.checkbox("水平翻转", key="r_fx"),
                flip_y=st.checkbox("垂直翻转", key="r_fy"),
            )
            arr = arr[::stride, ::stride]
            label = U.LAYER_LABELS[chosen_layer]
            U.assert_safe_wording(label, where="layer_label")
            fig = go.Figure()
            if show3d:
                xs = frozen.arrays_from(None).get("x")
                ys = frozen.arrays_from(None).get("y")
                fig.add_trace(
                    go.Surface(
                        z=arr,
                        x=None if xs is None else xs[::stride],
                        y=None if ys is None else ys[::stride],
                    )
                )
                fig.update_layout(scene=dict(xaxis_title="x", yaxis_title="y", zaxis_title=label))
            else:
                fig.add_trace(go.Heatmap(z=arr, colorscale="Viridis", colorbar=dict(title=label)))
            fig.update_layout(
                title=f"{label}｜{U.format_watermark(frozen.material_watermark)}",
                height=460,
                margin=dict(l=10, r=10, t=60, b=10),
            )
            st.plotly_chart(fig, width="stretch", key="r_fig_main")

    with tabsec:
        depth = frozen.depth_of()
        if depth is None:
            st.info("该结果不提供深度（threshold_only 或未保存）——界面不显示数值零冒充“无去除”。")
        else:
            ny_, nx_ = depth.shape
            axis = st.radio("截面方向", ("x", "y"), horizontal=True, key="s_axis")
            idx = st.slider("位置下标", 0, (nx_ if axis == "x" else ny_) - 1, (nx_ if axis == "x" else ny_) // 2, key="s_idx")
            xs = frozen.arrays_from(None).get("x")
            coords = None if xs is None else (xs if axis == "x" else frozen.arrays_from(None).get("y"))
            sec = U.cross_section(depth, axis=axis, index=int(idx), coords=coords)
            fig = go.Figure(go.Scatter(x=sec["coord"], y=sec["value"], mode="lines", name="深度"))
            fig.update_layout(
                title=f"截面（{axis}，下标 {idx}）｜{U.format_watermark(frozen.material_watermark)}",
                xaxis_title="坐标", yaxis_title=frozen.material_watermark.get("depth_label"),
                height=380, margin=dict(l=10, r=10, t=60, b=10),
            )
            st.plotly_chart(fig, width="stretch", key="r_fig_sec")
            st.caption("截面从已求解场读取，不重新求解。")

    with tabtl:
        if n_snap == 0:
            st.info("本次运行没有保存快照（snapshot_policy=none 或未达快照节点）。")
        else:
            i = st.slider("快照（回放）", 0, n_snap - 1, n_snap - 1, key="t_idx")
            meta = {k: v for k, v in frozen.snapshots[i].items() if not hasattr(v, "shape")}
            st.caption(f"快照 {i + 1}/{n_snap}｜事件 {meta.get('event_index')}｜t = {meta.get('time_s')} s｜"
                       f"遍 {meta.get('pass_id')}｜{'最终' if meta.get('final') else '过程'}")
            arr = U.layer_view(state, chosen_layer if chosen_layer in layer_names else "height", int(i))
            if arr is not None:
                fig = go.Figure(go.Heatmap(z=arr, colorscale="Viridis"))
                fig.update_layout(title=f"快照回放｜{U.LAYER_LABELS.get(chosen_layer, chosen_layer)}", height=400,
                                  margin=dict(l=10, r=10, t=50, b=10))
                st.plotly_chart(fig, width="stretch", key="r_fig_tl")
            st.caption("拖动时间轴只读取已保存快照，求解次数不变。")

    _result_footers(frozen)


def _result_footers(frozen: U.FrozenRun):
    """结果区页脚：警告、错误与（批次 G）相结构诊断。无图可渲染时也会显示。"""
    if frozen.warnings:
        with st.expander(f"警告（{len(frozen.warnings)}）", expanded=False):
            for w in frozen.warnings:
                st.write(f"- {w}")
    if frozen.errors:
        with st.expander(f"错误（{len(frozen.errors)}）", expanded=True):
            for e in frozen.errors:
                st.write(f"- [{e.get('code')}] {e.get('message')}（{e.get('field_path')}）")
    _structure_diagnostics_block(frozen)


def _structure_diagnostics_block(frozen: U.FrozenRun):
    """批次 G：相结构诊断（分相统计与跨相截断）。

    均质运行没有分相结构，本块不显示。跨相截断项名称固定为
    「未应用候选去除体积」——它不是剩余热量，也不是界面能量传输结果。
    """
    d = frozen.structure_diagnostics
    if not d:
        return
    vf = d.get("volume_fraction") or {}
    target = d.get("target_volume_fraction")
    actual = vf.get("actual_volume_fraction")
    with st.expander(
        f"相结构诊断｜{d.get('structure_type')}｜算法 {d.get('algorithm_version')}｜"
        f"种子 {d.get('seed')}｜相数 {len(d.get('phases') or [])}",
        expanded=False,
    ):
        st.caption(
            f"目标体积分数：{'未声明' if target is None else f'{target:.4g}'}｜"
            f"实际体积分数（{vf.get('volume_fraction_method') or '未估计'}）："
            f"{actual if actual is not None else '未估计'}。"
            "目标比例不代表有限样本必然等于该值，两者分别报告。"
        )
        if d.get("same_phase_merge"):
            st.caption("同相相邻铺层已预先合并；未在人为层边界处截断。")
        rows = frozen.structure_summary_rows()
        if rows:
            st.caption("各相阈值/去除尺度为**内联合成定义**（内部单位：F_ref / L_ref），不从材料卡借用。")
            st.dataframe(rows, hide_index=True, width="stretch")
        if d.get("clipped_events"):
            st.caption("跨相截断是有损近似：实际去除取「候选」与「到下一不同相界面距离」的较小值。")
        st.dataframe(frozen.truncation_rows(), hide_index=True, width="stretch")
        if d.get("truncation_note"):
            st.caption(d["truncation_note"])


# ---------------------------------------------------------------------------
# 参考评估器（批次 D）
# ---------------------------------------------------------------------------


def _reference_panel():
    st.subheader("文献参考评估器（批次 D）")
    st.caption("只复现文献定义的公式与协议量：**不求解网格**，不产生形貌；"
               "平均率与累计深度不得进入逐事件主循环。")

    case_name = st.selectbox("参考算例", REFERENCE_CASES, key="ref_case")
    if st.button("评估", key="ref_eval"):
        try:
            case = REF.load_reference_case(EXAMPLES / case_name)
            card_file = case.get("material_card_file")
            material = load_material_card(ROOT / card_file) if card_file else None
            res = REF.ReferenceEvaluator.evaluate_case(case, material)
            st.session_state["ref_result"] = res.to_dict()
        except UFDemoError as err:
            st.session_state.pop("ref_result", None)
            st.error(err.format_human())

    payload = st.session_state.get("ref_result")
    if not payload:
        return

    st.info(f"输出语义：{payload['output_semantics']}（{payload['output_semantics_zh']}）｜"
            f"事件核允许：{'是' if payload['event_kernel_allowed'] else '否'}｜"
            f"实验复现：{'是' if payload['verified_by_experiment'] else '否（只做公式核查）'}")
    st.caption(f"材料 {payload['material_id']}｜证据 {payload['evidence_status']}｜条件匹配 {payload['condition_match']}")

    rows = []
    for k, v in payload["values"].items():
        if k == "sweep":
            continue
        # 「值」列既有数值又有定义字符串（如 effective_count_definition），
        # 统一转成字符串展示，避免 Arrow 混合类型转换告警。
        rows.append({"量": k, "值": v if isinstance(v, str) else repr(v),
                     "单位": payload["units"].get(k, "")})
    st.dataframe(rows, hide_index=True, height=260, width="stretch")

    if payload["values"].get("sweep"):
        st.markdown("**有效 N 扫描**")
        st.dataframe(payload["values"]["sweep"], hide_index=True, width="stretch")
        st.caption("N=1e9 仅用于核对 Fth(N)→F_∞ 极限，其累计深度无物理意义。")

    for n in payload["notes"]:
        st.caption(f"- {n}")
    for w in payload["warnings"]:
        st.warning(w)
    if payload.get("engineering_extension"):
        st.caption(f"工程外推说明：{payload['engineering_extension']}")


# ---------------------------------------------------------------------------
# 历史运行
# ---------------------------------------------------------------------------


def _history_panel(state: U.SessionState):
    st.subheader("历史运行（读取已有结果，不重新求解）")
    runs = U.list_runs(default_runs_dir())
    if not runs:
        st.info("runs/ 下暂无可读取的运行目录。")
        return
    labels = [
        f"{r['run_id']}｜{r.get('status_zh')}｜{r.get('run_mode')}｜{r.get('written_at_utc')}"
        for r in runs
    ]
    i = st.selectbox("选择运行目录", range(len(labels)), format_func=lambda k: labels[k], key="h_idx")
    if st.button("读取结果", key="h_load"):
        try:
            frozen = U.read_existing_run(state, runs[i]["run_dir"])
            if frozen.status != "completed":
                st.warning("该运行未完成（失败/取消），只代表上一次运行的部分结果，不得当作成功输出。")
            st.success(f"已读取：{frozen.run_id}（读取次数 {state.read_count}，求解次数不变 {state.solve_count}）")
        except UFDemoError as err:
            st.error(err.format_human())


# ---------------------------------------------------------------------------
# 查表（批次 F）：读取曲线卡，插值，不求解
# ---------------------------------------------------------------------------


def _table_panel(state: U.SessionState):
    st.subheader("查表（曲线插值，不触发求解）")
    st.caption(
        "读取固定材料/波长/脉宽/历史协议下的一维响应曲线：默认**分段线性**，"
        "需要平滑时用**保形 PCHIP**（`extrapolate=False`）。"
        "越界返回状态与原因，**低于量测区间不返回 0，也不外推或钳到端点**。"
    )

    cards = U.list_curve_cards(default_curves_dir())
    if not cards:
        st.info(
            f"`{default_curves_dir()}` 下暂无曲线卡。可用 `python -m ufdemo.make_curves` 生成示例。"
        )
        return

    names = [c["name"] for c in cards]
    cur = st.session_state.get("t_curve")
    idx = names.index(cur) if cur in names else 0
    name = st.selectbox("曲线卡（*.curve.json）", names, index=idx, key="t_curve")

    try:
        curve = U.load_curve_card(default_curves_dir(), name)
    except UFDemoError as err:
        st.error(err.format_human())
        return

    # 去向与能力（如实展示）
    st.markdown("**曲线身份与去向**")
    U.assert_safe_wording(curve.y_quantity.get("name", ""), where="curve_y")
    st.dataframe(U.curve_capability_rows(curve), hide_index=True, width="stretch")
    st.caption(
        f"x：{curve.x_quantity.get('name')}（{curve.x_unit}）｜"
        f"y：{curve.y_quantity.get('name')}（{curve.y_unit}）｜"
        f"来源：{curve.source_figure_or_table}｜来源类型 {curve.source_type}"
    )
    if curve.notes:
        with st.expander("说明 / 限制", expanded=False):
            for n in curve.notes:
                st.write(f"- {n}")
            for lim in curve.limitations:
                st.write(f"- ⚠ {lim}")
    if not curve.can_enter_event_kernel:
        st.warning(
            "该曲线**不得进入逐事件核**，也不得用于生成局部形貌；只能进评估器"
            "（体积/平均率/累计曲线在无额外形状假设时不能唯一反推局部深度）。"
        )

    # 原始点（保留全部，含重复 x）
    st.markdown("**原始数据点（CSV 唯一来源）**")
    raw_rows = [{"x": p[0], "y": p[1]} for p in curve.raw_points]
    st.dataframe(raw_rows, hide_index=True, height=180, width="stretch")
    dr = curve.duplicate_report
    if dr.applied:
        st.caption(
            f"重复 x 处理：policy={dr.policy}，已合并 {dr.merged_count} 个，"
            f"规则：{dr.rule_note}；原始点 {dr.raw_point_count} 条全部保留。"
        )

    # 查询
    st.markdown("**查询**")
    c1, c2, c3 = st.columns([2, 1, 1])
    lo, hi = curve.valid_range
    default_xs = f"{lo!r}, {0.5 * (lo + hi)!r}, {hi!r}"
    xs_text = c1.text_input("查询 x（逗号分隔，有效区间内）", value=default_xs, key="t_xs")
    method = c2.selectbox("插值方法", list(T.INTERPOLATION_METHODS), index=0, key="t_method")
    allow_oor = c3.checkbox("允许越界（越界项返回 None）", value=False, key="t_allow_oor")

    if st.button("查值", key="t_lookup"):
        try:
            parsed = [float(s.strip()) for s in xs_text.split(",") if s.strip()]
        except ValueError:
            st.error("查询 x 必须是逗号分隔的数值。")
            return
        try:
            res = U.table_lookup(
                state, curve, parsed, method=method, allow_out_of_range=allow_oor
            )
            st.session_state["t_result"] = res.to_dict()
        except UFDemoError as err:
            st.session_state.pop("t_result", None)
            st.error(err.format_human())
            return

    payload = st.session_state.get("t_result")
    if payload and payload.get("curve_id") == curve.curve_id:
        rows = [
            {
                "x": x,
                "y（None=越界，非 0）": ("None（越界）" if v is None else repr(v)),
                "在区间内": "是" if ok else "否",
            }
            for x, v, ok in zip(payload["x"], payload["values"], payload["in_range"])
        ]
        st.dataframe(rows, hide_index=True, width="stretch")
        if payload["status"] == "ok":
            st.success(f"状态：{payload['status']}｜{payload['reason']}")
        else:
            st.warning(f"状态：{payload['status']}｜{payload['reason']}")
        for note in payload["notes"]:
            st.caption(f"- {note}")

    # 原始点与插值图
    st.markdown("**原始点与插值图**")
    fig = go.Figure()
    grid_m = U.table_grid(state, curve, n=200, method="linear")
    fig.add_trace(
        go.Scatter(x=grid_m["x"], y=grid_m["y"], mode="lines", name="线性插值")
    )
    if T.PCHIP_AVAILABLE:
        grid_p = U.table_grid(state, curve, n=200, method="pchip")
        fig.add_trace(
            go.Scatter(
                x=grid_p["x"], y=grid_p["y"], mode="lines", name="PCHIP 插值", line=dict(dash="dot")
            )
        )
    else:
        st.caption("当前环境未安装 SciPy：不显示 PCHIP（不会自动退化为线性）。")
    fig.add_trace(
        go.Scatter(
            x=grid_m["raw_x"],
            y=grid_m["raw_y"],
            mode="markers",
            name="原始点",
            marker=dict(size=8, symbol="circle-open"),
        )
    )
    fig.update_layout(
        title=f"{curve.curve_id}｜{curve.y_quantity.get('name')} vs {curve.x_quantity.get('name')}",
        xaxis_title=f"{curve.x_quantity.get('name')}（{curve.x_unit}）",
        yaxis_title=f"{curve.y_quantity.get('name')}（{curve.y_unit}）",
        height=420,
        margin=dict(l=10, r=10, t=60, b=10),
    )
    st.plotly_chart(fig, width="stretch", key="t_fig")
    st.caption("插值图与查值均不调用求解器；侧边栏求解次数保持不变。")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="超快激光加工 Demo", layout="wide")
    state = _state()
    material, run_mode = _sidebar(state)

    st.title("七种材料超快激光加工 Demo")
    st.caption("M0 最小闭环 + 批次 D 参考评估器 + 批次 E 界面 + 批次 F 查表。"
               "旧资料与 `微观仿真/` 未被修改。")

    t1, t2, t3, t4, t5 = st.tabs(
        ["参数与运行", "结果（形貌/截面/时间轴）", "参考评估器", "查表", "历史运行"]
    )
    with t1:
        _param_panel(state, material, run_mode)
    with t2:
        _result_panel(state)
    with t3:
        _reference_panel()
    with t4:
        _table_panel(state)
    with t5:
        _history_panel(state)


main()
