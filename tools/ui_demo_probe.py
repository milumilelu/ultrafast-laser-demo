"""端到端「演示可用性」探针（批次 G 增补）。

回答一个问题：**前端（`app.py` / Streamlit）与后端（求解引擎）是否真的对接、
能否完整演示一遍，而不只是各自单测通过。**

现有 ``tools/ui_probe.py`` 验证的是「界面记账不变量」（提交才求解、回放不重算）。
本探针补上它没有覆盖的一层：**分相结构在界面上的完整链路**——从选材料/选模板，
到点「提交计算」，到结果区真的渲染出「相结构诊断」面板，再到历史读取同源。

覆盖 7 组检查：

* A 示例发现：两个合成结构示例被界面自动列出（`list_examples` 扫目录）；
* B 逻辑层提交：两个结构示例走 `ui_service.submit`（唯一求解入口）能出结果；
* C 只读不重算：渲染图层/结构摘要/截断诊断/快照后 `solve_count` 不变；
* D 措辞守卫：全部图层标签与不可用原因不含被禁词；
* E 界面全链路：`AppTest` 选 CFRP + `synthetic_demo` + 铺层模板 → 点提交 → 无异常；
* F 面板渲染：结果区真的出现「相结构诊断」expander；
* G 历史同源：`read_existing_run` 读回同一运行，带出同样诊断且不求解。

用法::

    python tools/ui_demo_probe.py [--out runs/ui_demo_probe]

输出 ``docs/reports/ui_demo_probe.csv`` 与 ``.md``。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

EXAMPLES = ROOT / "examples"
D = ROOT / "docs" / "reports"

CFRP_EXAMPLE = "cfrp_laminated_ply.json"
CFRP_MATERIAL = "cfrp_t700_yb01_800nm"
ALSIC_EXAMPLE = "alsic_particle_composite.json"
ALSIC_MATERIAL = "alsic_sicp_aa2024_1030nm"

STRUCTURE_CASES = ((CFRP_EXAMPLE, CFRP_MATERIAL), (ALSIC_EXAMPLE, ALSIC_MATERIAL))


def _row(check: str, expected: Any, measured: Any, status: str, note: str = "") -> dict[str, Any]:
    return {"check": check, "expected": expected, "measured": measured, "status": status, "note": note}


def _ok(check: str, expected: Any, measured: Any, note: str = "") -> dict[str, Any]:
    return _row(check, expected, measured, "通过", note)


def _fail(check: str, expected: Any, measured: Any, note: str = "") -> dict[str, Any]:
    return _row(check, expected, measured, "失败", note)


def run_demo_checks(out_dir: Path, *, skip_apptest: bool = False) -> list[dict[str, Any]]:
    """返回逐项检查结果。``out_dir`` 用于隔离运行输出（不污染工作区 ``runs/``）。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from ufdemo import default_material_dir
    from ufdemo import ui_service as U
    from ufdemo.materials import load_material_catalog

    rows: list[dict[str, Any]] = []

    # ---------------- A. 示例发现 ----------------
    try:
        names = U.list_examples(EXAMPLES)
    except Exception as exc:  # noqa: BLE001
        return [_fail("示例发现", "可列出", f"异常 {exc}", str(exc))]

    for exp, _mat in STRUCTURE_CASES:
        if exp in names:
            rows.append(_ok("示例发现", f"{exp} 出现在界面示例列表", "在列表中",
                            "示例由 list_examples 扫目录自动纳入界面下拉"))
        else:
            rows.append(_fail("示例发现", f"{exp} 出现在界面示例列表", "不在列表",
                              f"界面下拉漏了该示例（实际 {len(names)} 个）"))

    # ---------------- B. 逻辑层提交 ----------------
    cat = load_material_catalog(default_material_dir())
    frozen_map: dict[str, Any] = {}
    state_map: dict[str, Any] = {}
    for exp, mat_id in STRUCTURE_CASES:
        try:
            params = U.load_template(EXAMPLES, exp)
            material = cat[mat_id]
            state = U.new_session(params)
            frozen = U.submit(
                state, material,
                out_base=out_dir / "runs",
                project_root=ROOT,
                label="demo_probe",
            )
            frozen_map[exp] = frozen
            state_map[exp] = state
            rows.append(_ok(
                "逻辑层提交", f"{exp} 提交后 status=completed",
                f"status={frozen.status}｜solve_count={state.solve_count}｜快照={len(frozen.snapshots)}",
                "走 ui_service.submit（界面唯一求解入口）",
            ))
        except Exception as exc:  # noqa: BLE001
            rows.append(_fail("逻辑层提交", f"{exp} 可提交求解", f"异常 {type(exc).__name__}: {exc}",
                              "提交失败"))

    # ---------------- C. 只读不重算 ----------------
    for exp, frozen in frozen_map.items():
        state = state_map[exp]
        before = state.solve_count
        frozen.renderable_layers()
        frozen.available_layers()
        frozen.structure_summary_rows()
        frozen.truncation_rows()
        for lname in frozen.renderable_layers():
            U.layer_view(state, lname, None)
        if frozen.snapshots:
            U.snapshot_arrays(state, 0)
        after = state.solve_count
        rows.append(
            _ok("只读不重算", "渲染/切图层/取快照后 solve_count 不变",
                f"{before} -> {after}", "图层与诊断均为纯读取")
            if before == after
            else _fail("只读不重算", "solve_count 不变", f"{before} -> {after}", "发生了隐式重算")
        )

    # ---------------- D. 措辞守卫 ----------------
    from ufdemo.ui_service import FORBIDDEN_TERMS, LAYER_LABELS

    bad_labels: list[str] = []
    for k, v in LAYER_LABELS.items():
        try:
            U.assert_safe_wording(v)
        except Exception:  # noqa: BLE001
            bad_labels.append(f"{k}={v}")
    rows.append(
        _ok("措辞守卫", f"全部图层标签不含被禁词（{len(LAYER_LABELS)} 项）",
            f"被禁词表 {FORBIDDEN_TERMS}；命中 {len(bad_labels)} 项",
            "标签只描述该图实际是什么量")
        if not bad_labels
        else _fail("措辞守卫", "无被禁词", f"命中 {bad_labels}", "措辞不合规")
    )

    bad_reasons: list[str] = []
    for exp, frozen in frozen_map.items():
        for k, v in frozen.available_layers().items():
            if not v:
                continue
            try:
                U.assert_safe_wording(str(v))
            except Exception:  # noqa: BLE001
                bad_reasons.append(f"{exp}:{k}")
    rows.append(
        _ok("措辞守卫（不可用原因）", "不可用原因文案不含被禁词", f"命中 {len(bad_reasons)} 项",
            "如 threshold_mask 如实报不可用，不用无关量凑数")
        if not bad_reasons
        else _fail("措辞守卫（不可用原因）", "无被禁词", f"命中 {bad_reasons}", "措辞不合规")
    )

    # ---------------- E/F/G. 界面全链路（AppTest）----------------
    if skip_apptest:
        rows.append(_row("界面全链路（AppTest）", "真实驱动 app.py", "已跳过（--skip-apptest）", "未运行",
                         "未执行，不得视为通过"))
        return rows

    try:
        from streamlit.testing.v1 import AppTest
    except Exception as exc:  # noqa: BLE001
        rows.append(_row("界面全链路（AppTest）", "可真实驱动 app.py",
                         f"未安装 streamlit.testing（{exc}）", "未运行", "缺依赖"))
        return rows

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300)
    at.run()
    rows.append(
        _ok("界面全链路（AppTest）", "首屏无异常", f"异常={len(at.exception)} 错误={len(at.error)}",
            "AppTest 真实执行 app.py")
        if not at.exception and not at.error
        else _fail("界面全链路（AppTest）", "首屏无异常",
                   f"异常={[str(e.value)[:120] for e in at.exception]}", "首屏报错")
    )

    # 选材料 → 运行模式 → 模板 → 提交
    try:
        labels = [str(o) for o in at.sidebar.selectbox(key="material_label").options]
        pick = next((o for o in labels if CFRP_MATERIAL in o), None)
        if pick is None:
            raise AssertionError(f"材料下拉找不到 {CFRP_MATERIAL}")
        at.sidebar.selectbox(key="material_label").set_value(pick).run()

        rm = [str(o) for o in at.sidebar.selectbox(key="run_mode").options]
        if "synthetic_demo" not in rm:
            raise AssertionError(f"CFRP 可选模式不含 synthetic_demo：{rm}")
        at.sidebar.selectbox(key="run_mode").set_value("synthetic_demo").run()

        t_opts = [str(o) for o in at.selectbox(key="template").options]
        if CFRP_EXAMPLE not in t_opts:
            raise AssertionError(f"模板下拉找不到 {CFRP_EXAMPLE}")
        at.selectbox(key="template").set_value(CFRP_EXAMPLE).run()
        at.button(key="reset_template").click().run()

        at.button(key="submit_run").click().run()
        state = None
        try:
            state = at.session_state["ui"]
        except Exception:  # noqa: BLE001
            pass
        n_exc, n_err = len(at.exception), len(at.error)
        got = state.frozen if state is not None else None
        detail = (
            f"异常={n_exc} 错误={n_err}"
            + (f"｜solve_count={state.solve_count}" if state is not None else "")
            + (f"｜status={got.status}｜快照={len(got.snapshots)}" if got is not None else "｜无结果")
        )
        rows.append(
            _ok("界面全链路（AppTest）", "点「提交计算」后无异常且出结果", detail,
                "选 CFRP + synthetic_demo + 铺层模板 → 提交")
            if (n_exc == 0 and n_err == 0 and got is not None and state.solve_count == 1)
            else _fail("界面全链路（AppTest）", "提交后无异常且 solve_count=1", detail,
                       "界面链路未走通")
        )
    except Exception as exc:  # noqa: BLE001
        rows.append(_fail("界面全链路（AppTest）", "界面可完成一次演示提交",
                          f"异常 {type(exc).__name__}: {exc}", "界面链路中断"))
        return rows

    at.run()
    rows.append(
        _ok("界面结果区渲染", "结果页重跑无异常", f"异常={len(at.exception)} 错误={len(at.error)}",
            "结果区在提交后重新渲染")
        if not at.exception and not at.error
        else _fail("界面结果区渲染", "结果页无异常",
                   f"异常={[str(e.value)[:120] for e in at.exception]}", "结果区报错")
    )

    # F. 相结构诊断面板
    exp_labels: list[str] = []
    try:
        exp_labels = [str(getattr(e, "label", "")) for e in at.expander]
    except Exception:  # noqa: BLE001
        pass
    hit = [lab for lab in exp_labels if "相结构诊断" in lab]
    rows.append(
        _ok("分相诊断面板", "结果区出现「相结构诊断」面板", hit[0] if hit else f"未出现（expander={exp_labels}）",
            "面板含目标/实际体积分数、同相合并说明、各相摘要与截断诊断")
        if hit
        else _fail("分相诊断面板", "出现「相结构诊断」面板", f"expander={exp_labels}", "面板未渲染")
    )

    # G. 历史读取同源
    try:
        run_dir = state.frozen.run_dir
        st2 = U.new_session(U.load_template(EXAMPLES, CFRP_EXAMPLE))
        fr = U.read_existing_run(st2, run_dir)
        same = bool(fr.structure_diagnostics) and (
            (fr.structure_diagnostics or {}).get("structure_type")
            == (state.frozen.structure_diagnostics or {}).get("structure_type")
        )
        ph = fr.layer("phase_id")
        detail = (
            f"solve_count={st2.solve_count} read_count={st2.read_count}"
            f"｜结构类型={(fr.structure_diagnostics or {}).get('structure_type')}"
            f"｜截断诊断行={len(fr.truncation_rows())}｜phase_id={'有' if ph is not None else '无'}"
        )
        rows.append(
            _ok("历史读取同源", "读回历史带出同源结构诊断且不求解", detail,
                "read_existing_run 与实时提交共用 build_structure_diagnostics")
            if (same and st2.solve_count == 0 and st2.read_count == 1)
            else _fail("历史读取同源", "同源诊断且 solve_count=0", detail, "历史读取与实时不一致")
        )
    except Exception as exc:  # noqa: BLE001
        rows.append(_fail("历史读取同源", "读回历史带出结构诊断",
                          f"异常 {type(exc).__name__}: {exc}", "历史读取失败"))

    return rows


def write_reports(rows: list[dict[str, Any]], out_dir: Path) -> tuple[Path, Path]:
    D.mkdir(parents=True, exist_ok=True)
    csv_path = D / "ui_demo_probe.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    n_pass = sum(1 for r in rows if r["status"] == "通过")
    n_fail = sum(1 for r in rows if r["status"] == "失败")
    n_nr = sum(1 for r in rows if r["status"] == "未运行")
    md = [
        "# 端到端演示可用性检查（前后端对接）",
        "",
        f"- 汇总：通过 {n_pass}｜失败 {n_fail}｜未运行 {n_nr}",
        f"- 探针输出目录：`{out_dir}`（隔离，不污染工作区 `runs/`）",
        "",
        "> 本检查回答：**前端（Streamlit）与后端（求解引擎）是否真的对接、能否完整演示一遍**。",
        "> 与 `ui_operation_check.md`（界面记账不变量）互补：那份查「有没有偷偷重算」，",
        "> 这份查「选材料→选模板→提交→出结果→面板渲染→历史读回」整条链路。",
        "",
        "## 检查项",
        "",
        "| 检查 | 预期 | 实测 | 状态 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(f"| {r['check']} | {r['expected']} | {r['measured']} | {r['status']} | {r['note']} |")
    md += [
        "",
        "## 判定依据",
        "",
        "- **提交是唯一求解入口**：界面「提交计算」→ `ui_service.submit` → `solver.solve`，",
        "  成功一次 `solve_count += 1`；其余操作（切图层/时间轴/截面/结构诊断面板/读历史）只读数组。",
        "- **分相图层与诊断同源**：实时提交与 `read_existing_run` 共用 `build_structure_diagnostics`，",
        "  因此历史读回的结构诊断与实时一致（`solve_count` 保持 0，只增 `read_count`）。",
        "- **跨相截断如实披露**：截断量命名为「未应用候选去除体积」，界面同时给出",
        "  「被截断的事件数 / 单元次数 / 相标签切换次数 / 未应用占比」，并提示属于有损近似。",
        "- **不提供的图层如实报不可用**：`threshold_mask` 显示原因而非数值 0；",
        "  均质运行不显示分相诊断面板，`phase_id` 图层如实报不可用（不返回全 0 假数组）。",
        "",
        "## 复现命令",
        "",
        "```bash",
        "python tools/ui_demo_probe.py",
        "python tools/ui_probe.py",
        "streamlit run app.py",
        "```",
        "",
    ]
    md_path = D / "ui_demo_probe.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    return csv_path, md_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="运行输出目录（默认临时目录，避免污染 runs/）")
    ap.add_argument("--skip-apptest", action="store_true", help="跳过 AppTest 段（只跑逻辑层）")
    args = ap.parse_args()

    if args.out:
        out_dir = Path(args.out)
    else:
        out_dir = Path(tempfile.mkdtemp(prefix="ufdemo_ui_demo_"))
    # 界面/求解都写到这里，避免污染工作区 runs/
    os.environ["UFDEMO_RUNS_DIR"] = str(out_dir / "runs")

    rows = run_demo_checks(out_dir, skip_apptest=args.skip_apptest)
    csv_path, md_path = write_reports(rows, out_dir)

    n_fail = sum(1 for r in rows if r["status"] == "失败")
    n_nr = sum(1 for r in rows if r["status"] == "未运行")
    print(f"报告：{csv_path}")
    print(f"报告：{md_path}")
    print(f"检查 {len(rows)} 项｜失败 {n_fail}｜未运行 {n_nr}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
