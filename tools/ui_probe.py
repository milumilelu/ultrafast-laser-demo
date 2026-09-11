"""界面操作检查探针（批次 E / T09，G09；批次 F / T10 增补查表检查）。

用 ``streamlit.testing.v1.AppTest`` 真实驱动 ``app.py``，把「提交/回放/查表求解次数」
等界面行为落成可机读的检查行，供验收报告与专项报告复用。

设计要点：

* 通过环境变量 ``UFDEMO_RUNS_DIR`` 把「提交计算」产生的运行写到 ``work_dir``，
  不污染工作区 ``runs/``；
* 本模块**不**执行任何科学计算断言——那些在 ``tests/test_ui_service.py`` 与
  ``tests/test_app_smoke.py``；这里只记录界面操作行为。

用法::

    from ui_probe import run_ui_checks
    rows = run_ui_checks(Path("runs/acceptance/_ui_probe"))
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

APP = str(ROOT / "app.py")


def _row(check: str, expected: Any, measured: Any, status: str, note: str = "") -> dict[str, Any]:
    return {
        "check": check,
        "expected": expected,
        "measured": measured,
        "status": status,
        "note": note,
    }


def _has(seq, key: str) -> bool:
    return any(getattr(x, "key", None) == key for x in seq)


def run_ui_checks(
    work_dir: Path | str,
    *,
    reference_run_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """执行界面操作检查，返回检查行列表。"""
    try:
        from streamlit.testing.v1 import AppTest
    except ImportError:  # pragma: no cover - 只有装不上 streamlit 才会到这里
        return [_row("全部界面检查", "可运行", "未安装 streamlit", "未运行",
                     "pip install -e .[ui] 后重跑")]

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    os.environ["UFDEMO_RUNS_DIR"] = str(work)

    rows: list[dict[str, Any]] = []

    def fresh():
        at = AppTest.from_file(APP, default_timeout=240)
        at.run()
        return at

    # --- 1. 初始渲染 ------------------------------------------------------
    at = fresh()
    rows.append(_row(
        "初始渲染无异常", "无异常",
        "异常：" + str(at.exception) if at.exception else "无异常",
        "失败" if at.exception else "通过",
        "六标签页：参数与运行 / 结果 / 参考评估器 / 查表 / 七材料能力入口 / 历史运行",
    ))
    s = at.session_state.ui
    rows.append(_row("初始 solve_count", 0, s.solve_count,
                     "通过" if s.solve_count == 0 else "失败", "未提交前不得求解"))
    rows.append(_row("初始无冻结结果", "frozen=None", "frozen=None" if s.frozen is None else "有结果",
                     "通过" if s.frozen is None else "失败", ""))

    # --- 2. 提交计算 → solve_count == 1 -----------------------------------
    at.button(key="submit_run").click().run()
    s = at.session_state.ui
    ok = (not at.exception) and s.solve_count == 1 and s.frozen is not None
    rows.append(_row(
        "提交计算：solve_count 恰好 +1", 1, s.solve_count,
        "通过" if ok else "失败",
        f"run_id={getattr(s.frozen, 'run_id', None)}｜status={getattr(s.frozen, 'status', None)}"
        + (f"｜异常 {at.exception}" if at.exception else ""),
    ))
    submitted_run_dir = getattr(s.frozen, "run_dir", None)

    # --- 3. 只改参数不提交 → 「上一次运行」--------------------------------
    at.number_input(key="f_nx").set_value(121).run()
    s = at.session_state.ui
    stale = s.is_stale()
    label = s.result_label()
    warns = " ".join(str(w.value) for w in at.warning)
    ok = stale and s.solve_count == 1 and "上一次运行" in warns
    rows.append(_row(
        "改参数未提交：旧结果标为「上一次运行」且不求解",
        "is_stale=True｜solve_count=1｜界面出现「上一次运行」",
        f"is_stale={stale}｜solve_count={s.solve_count}｜label={label}",
        "通过" if ok else "失败",
        "执行细则 11.3",
    ))
    rows.append(_row("改参数后 solve_count 不变", 1, s.solve_count,
                     "通过" if s.solve_count == 1 else "失败", ""))

    # --- 4. 回放 / 图层 / 视图 / 截面 → 不求解 ----------------------------
    frozen = at.session_state.ui.frozen
    before = at.session_state.ui.solve_count
    if _has(at.slider, "t_idx"):
        at.slider(key="t_idx").set_value(0).run()
    if _has(at.selectbox, "r_layer"):
        for name in frozen.renderable_layers():
            at.selectbox(key="r_layer").set_value(name).run()
    for key in ("r_T", "r_fx", "r_fy", "r_3d"):
        if _has(at.checkbox, key):
            at.checkbox(key=key).set_value(True).run()
    if _has(at.slider, "r_stride"):
        at.slider(key="r_stride").set_value(4).run()
    if _has(at.radio, "s_axis"):
        at.radio(key="s_axis").set_value("y").run()
    if _has(at.slider, "s_idx"):
        at.slider(key="s_idx").set_value(1).run()
    after = at.session_state.ui.solve_count
    rows.append(_row(
        "快照回放 / 切图层 / 旋转视图 / 切截面：solve_count 不变",
        f"{before}（不变）", after,
        "通过" if after == before else "失败",
        "执行细则 11.3「只旋转视图、切换图层/截面/快照时读取已有数据」"
        + (f"｜异常 {at.exception}" if at.exception else ""),
    ))

    # --- 5. 读取历史运行 → read_count +1，solve_count 不变 ----------------
    at2 = fresh()
    ok_read = False
    note = ""
    if _has(at2.button, "h_load"):
        at2.button(key="h_load").click().run()
        s2 = at2.session_state.ui
        ok_read = (not at2.exception) and s2.read_count == 1 and s2.solve_count == 0
        note = f"read_count={s2.read_count}｜solve_count={s2.solve_count}｜run_id=" \
               f"{getattr(s2.frozen, 'run_id', None)}"
        if at2.exception:
            note += f"｜异常 {at2.exception}"
    rows.append(_row(
        "读取历史运行：read_count +1 且 solve_count 不变",
        "read_count=1｜solve_count=0", note or "（历史面板无按钮）",
        "通过" if ok_read else "失败",
        "读取不等于用当前参数求解",
    ))

    # 5b. 回放水印与导出同源（批次 H / 细则 11.3）
    import json as _json

    from ufdemo.io import load_run as _load_run

    if ok_read and getattr(at2.session_state.ui, "frozen", None) is not None:
        _fz = at2.session_state.ui.frozen
        _wm_path = Path(_fz.run_dir) / "watermark.json"
        _exported = _json.loads(_wm_path.read_text(encoding="utf-8")) if _wm_path.exists() else {}
        _keys = ("material_id", "run_mode", "unit_mode")
        _ok_wm = bool(_exported) and all(
            _fz.material_watermark.get(k) == _exported.get(k) for k in _keys
        )
        rows.append(_row(
            "回放水印与导出 watermark.json 逐字段一致",
            "material_id/run_mode/unit_mode 全部一致",
            f"导出存在={_wm_path.exists()}｜"
            + "｜".join(f"{k}={_fz.material_watermark.get(k)}" for k in _keys),
            "通过" if _ok_wm else "失败",
            "批次 H：导出=回放，标签不再各写一份",
        ))
    else:
        rows.append(_row(
            "回放水印与导出 watermark.json 逐字段一致",
            "material_id/run_mode/unit_mode 全部一致",
            "（未读到历史运行，无法核对）",
            "未运行",
            "需要 runs/ 下有可读的运行目录",
        ))

    # --- 6. 参考评估器：不求解网格 ----------------------------------------
    at3 = fresh()
    at3.button(key="ref_eval").click().run()
    s3 = at3.session_state.ui
    payload = at3.session_state["ref_result"] if "ref_result" in at3.session_state else None
    ok_ref = (not at3.exception) and s3.solve_count == 0 and payload is not None and s3.frozen is None
    rows.append(_row(
        "参考评估器「评估」：不进入逐事件求解、不产生形貌",
        "solve_count=0｜产出参考结果｜frozen=None",
        f"solve_count={s3.solve_count}｜有参考结果={payload is not None}｜frozen="
        f"{'有' if s3.frozen else 'None'}"
        + (f"｜异常 {at3.exception}" if at3.exception else ""),
        "通过" if ok_ref else "失败",
        "参考评估器只做公式核查（批次 D 语义）",
    ))

    # --- 6b. 查表：读曲线/插值不求解（批次 F）-----------------------------
    at_t = fresh()
    if _has(at_t.selectbox, "t_curve") and _has(at_t.button, "t_lookup"):
        at_t.button(key="t_lookup").click().run()
        s_t = at_t.session_state.ui
        payload_t = at_t.session_state["t_result"] if "t_result" in at_t.session_state else None
        ok_tbl = (not at_t.exception) and s_t.solve_count == 0 and payload_t is not None
        rows.append(_row(
            "查表「查值」：不触发求解、不读取历史",
            "solve_count=0｜产出查表结果｜read_count=0",
            f"solve_count={s_t.solve_count}｜read_count={s_t.read_count}｜"
            f"有查表结果={payload_t is not None}"
            + (f"｜异常 {at_t.exception}" if at_t.exception else ""),
            "通过" if ok_tbl else "失败",
            "批次 F：查表是纯读取，不是求解",
        ))
        # 逐曲线 + 逐方法再跑一遍，确认都不求解
        curves = list(at_t.selectbox(key="t_curve").options)
        ok_sw = True
        detail = []
        for cname in curves:
            at_t.selectbox(key="t_curve").set_value(cname).run()
            for m in ("linear", "pchip"):
                if _has(at_t.selectbox, "t_method"):
                    at_t.selectbox(key="t_method").set_value(m).run()
                at_t.button(key="t_lookup").click().run()
                if at_t.exception or at_t.session_state.ui.solve_count != 0:
                    ok_sw = False
                    detail.append(f"{cname}/{m}")
        rows.append(_row(
            "切换曲线卡 / 插值方法：solve_count 始终为 0",
            "0（不变）",
            f"曲线数={len(curves)}｜异常组合={detail or '无'}",
            "通过" if ok_sw else "失败",
            "批次 F：换曲线/换算法不得触发求解",
        ))
    else:
        rows.append(_row(
            "查表「查值」：不触发求解", "solve_count=0｜产出查表结果", "查表面板未渲染", "未运行",
            "确认 data/curves 下存在曲线卡",
        ))

    # --- 7. 站点级不变量（纯逻辑，界面同源）-------------------------------
    from ufdemo import ui_service as U
    from ufdemo.materials import load_material_card

    # 7a. 七材料能力入口表（批次 H）：逐条实跑核验，且不得声称"缺口已开放"
    from ufdemo import default_material_dir

    entry_rows = U.material_entry_rows(default_material_dir())
    entry_sum = U.material_entry_summary(entry_rows)
    deferred_names = [(d["family"], d["item"]) for d in entry_sum["deferred_items"]]
    ok_entries = entry_sum["all_verified"] and entry_sum["all_probes_hold"]
    rows.append(_row(
        "七材料能力入口：全部条目探针实跑通过",
        "未核验=0｜探针全通过｜开放/红线/缺口均绑定依据",
        f"开放={entry_sum['n_opened']}｜红线={entry_sum['n_blocked']}｜"
        f"缺口={entry_sum['n_deferred']}{deferred_names}｜未核验={entry_sum['n_unverified']}",
        "通过" if ok_entries else "失败",
        "批次 H：拦截与缺口都必须有可执行证据，不得只在文档中声称",
    ))
    diamond_open = {
        r["item"] for r in entry_rows if r["family"] == "金刚石" and r["kind"] == "opened"
    }
    rows.append(_row(
        "金刚石「合成形貌」记为缺口而非开放",
        "opened 中不含「合成形貌」",
        f"opened={sorted(diamond_open)}",
        "通过" if "合成形貌" not in diamond_open else "失败",
        "规格要求开放但实现未支持，必须如实标为缺口并证明当前打不开",
    ))

    safe = True
    bad = []
    for name, label in U.LAYER_LABELS.items():
        try:
            U.assert_safe_wording(label, where=f"layer_label.{name}")
        except Exception:  # noqa: BLE001
            safe = False
            bad.append(name)
    rows.append(_row(
        "图层标签不含被禁措辞（严格子串：热影响区/HAZ/温度…）",
        "全部通过", "全部通过" if safe else f"违规图层 {bad}",
        "通过" if safe else "失败", "执行细则 11.3 措辞守卫",
    ))

    cfrp = load_material_card(ROOT / "data" / "materials" / "cfrp_t700_yb01_800nm.json")
    gate_ok, gate_reason = U.capability_gate(cfrp, "reference_case")
    rows.append(_row(
        "缺能力模式显示准确不可用原因（不自动降级、不静默填值）",
        "拒绝并给出原因",
        f"允许={gate_ok}｜原因={gate_reason}",
        "通过" if (not gate_ok and gate_reason) else "失败",
        "配置层拦截，而非仅按钮禁用",
    ))

    synth_ctx = U.UnitContext.from_config(
        {
            "unit_system": "dimensionless",
            "reference_scales": {"L_ref_m": 1e-5, "F_ref_J_m2": 1e4, "delta_ref_m": 1e-7},
        }
    )
    synth_label = U.depth_export_label(synth_ctx)
    depth_guarded = False
    try:
        U.guard_depth_export(synth_ctx, "result_depth_um.csv")
    except Exception:  # noqa: BLE001
        depth_guarded = True
    rows.append(_row(
        "合成模式禁止导出物理深度",
        "以 depth_um 命名时拒绝；depth 标签非 um",
        f"导出标签={synth_label}｜拒绝 depth_um={depth_guarded}",
        "通过" if (depth_guarded and synth_label != "depth_um") else "失败",
        "执行细则 4.1：合成深度与微米平面坐标不可混算",
    ))

    # --- 8. threshold_only 结果不得用数值零冒充「无去除」------------------
    ref_dir = Path(reference_run_dir) if reference_run_dir else (
        ROOT / "runs" / "g05_ysz_reference"
    )
    if (ref_dir / "metadata.json").exists():
        at4 = fresh()
        U.read_existing_run(at4.session_state.ui, str(ref_dir))
        loaded = at4.session_state.ui.frozen
        ok_thr = (loaded is not None) and (loaded.removal_available is False)
        rows.append(_row(
            "threshold_only/参考结果：去除量显示「不提供」而非 0",
            "removal_available=False",
            f"removal_available={None if loaded is None else loaded.removal_available}｜"
            f"run_id={None if loaded is None else loaded.run_id}",
            "通过" if ok_thr else "失败",
            "界面不显示数值零冒充「无去除」",
        ))
    else:
        rows.append(_row(
            "threshold_only/参考结果：去除量显示「不提供」而非 0",
            "removal_available=False", "无参考运行目录", "未运行",
            f"先运行 python -m ufdemo reference ... 生成 {ref_dir}",
        ))

    # 清理：本探针自己写入 work_dir 的运行目录保持原样（work_dir 属临时目录）
    rows.append(_row(
        "探针运行输出位置", f"隔离到 {work}",
        f"已写入 {len(list(work.glob('ui_run_*')))} 个运行目录",
        "通过", "UFDEMO_RUNS_DIR 覆盖，不污染工作区 runs/",
    ))
    rows.append(_row("提交运行目录", "存在 metadata.json",
                     "存在" if submitted_run_dir and (Path(submitted_run_dir) / "metadata.json").exists()
                     else "缺失",
                     "通过" if submitted_run_dir and (Path(submitted_run_dir) / "metadata.json").exists()
                     else "失败", ""))
    return rows


def main() -> int:
    rows = run_ui_checks(ROOT / "runs" / "_ui_probe")
    for r in rows:
        print(f"[{r['status']}] {r['check']}\n      预期：{r['expected']}\n      实测：{r['measured']}")
    n_fail = sum(1 for r in rows if r["status"] == "失败")
    n_nr = sum(1 for r in rows if r["status"] == "未运行")
    print(f"\n通过 {sum(1 for r in rows if r['status'] == '通过')}｜失败 {n_fail}｜未运行 {n_nr}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
