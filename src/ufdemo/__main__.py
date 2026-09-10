"""命令行入口（执行细则第 11 章、12 章）。

目标命令::

    python -m ufdemo validate  examples/analytic_single_pulse.json
    python -m ufdemo run       examples/analytic_single_pulse.json --out runs/analytic_001
    python -m ufdemo reference examples/sic_reference_case.json --out runs/sic_ref_001
    python -m ufdemo table     data/curves/ysz_analytic_depth_vs_fluence.curve.json --x 5
    python -m ufdemo materials
    python -m ufdemo inspect   runs/analytic_001

``reference`` 属批次 D（T07）：只复现文献定义的公式与协议量（有效 N、阈值、
平均率、协议累计深度），**不求解网格**，输出目录中不会出现形貌表面文件。

``table`` 属批次 F（T10）：曲线 schema + 分段线性/PCHIP + 越界处理。越界返回
``TABLE_OUT_OF_RANGE``（不填 0、不外推）；体积/平均/阈值曲线不得生成局部深度。

退出码：0 成功；1 已归类的执行失败；2 用法错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__, default_material_dir, default_runs_dir, project_root
from .config import load_config, validate_run
from .errors import UFDemoError
from .io import code_version, load_run, make_run_id, new_run_dir, save_run
from .materials import capability_table, load_material_card, load_material_catalog, resolve_material
from .solver import solve

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def _print_error(err: UFDemoError, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"error": err.to_dict()}, ensure_ascii=False, indent=2))
    else:
        print("错误：" + err.message)
        if err.field_path:
            print(f"  字段：{err.field_path}")
        if err.actual is not None:
            print(f"  实际值：{err.actual!r}")
        if err.requirement:
            print(f"  要求：{err.requirement}")
        if err.suggestion:
            print(f"  建议：{err.suggestion}")
        print(f"  错误码：{err.code}（{err.code_zh}）")


def cmd_validate(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    material = resolve_material(cfg, args.material_dir) if (cfg.material_card_file or cfg.material_id) else None
    if material is None:
        print("配置未指定材料，仅做结构校验。")
        return EXIT_OK
    report = validate_run(cfg, material)
    payload = report.to_dict()
    payload["material_watermark"] = material.watermark()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        state = "通过" if report.ok else "拒绝"
        print(f"准入校验：{state}")
        print(f"  运行模式：{report.run_mode}｜单位：{report.unit_system}｜材料：{report.material_id}")
        print(f"  证据状态：{report.evidence_status}｜允许模式：{list(report.allowed_run_modes)}")
        print(f"  预估事件数：{report.estimated_events}｜预估内存：{report.estimated_memory_bytes} B")
        for n in report.notes:
            print(f"  说明：{n}")
        for w in report.warnings:
            print(f"  警告：{w}")
        for e in report.errors:
            print(f"  错误：[{e['code']}] {e['message']}（{e['field_path']}）")
            if e.get("suggestion"):
                print(f"        建议：{e['suggestion']}")
    return EXIT_OK if report.ok else EXIT_FAILED


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    material = resolve_material(cfg, args.material_dir)

    base = Path(args.out_base).resolve() if args.out_base else default_runs_dir()
    if args.out:
        run_dir = Path(args.out).resolve()
        if run_dir.exists() and not args.force_new_suffix:
            # 细则 11.1：目录已存在则拒绝，不覆盖
            raise UFDemoError(
                "CONFIG_INVALID",
                "指定的输出目录已存在，拒绝覆盖",
                field_path="--out",
                actual=str(run_dir),
                requirement="输出目录必须不存在",
                suggestion="换路径，或加 --force-new-suffix 以自动追加时间+随机后缀。",
            )
        run_id = run_dir.name
    else:
        run_id = make_run_id(cfg.label or "run")
        run_dir = new_run_dir(base, run_id)

    result = solve(cfg, material, cancel_token=None, progress_callback=None)
    result.run_id = run_id

    code_info = code_version(project_root())
    saved = save_run(result, run_dir, project_root=project_root(), code_info=code_info)

    summary = result.summary()
    summary["run_dir"] = str(run_dir)
    summary["written_files"] = saved.written_files
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"状态：{result.status}")
        print(f"运行目录：{run_dir}")
        print(f"材料：{cfg.material_id}｜模式：{cfg.run_mode}｜单位：{cfg.unit.mode}")
        print(f"事件：{result.events_processed}/{result.events_total}")
        st = result.statistics or {}
        if st.get("removal_available"):
            print(f"  去除体积：{st.get('removal_volume_internal')} {cfg.unit.length_label}^3")
            print(f"  中心深度：{st.get('center_depth_internal')} {cfg.unit.depth_label}")
            print(f"  最大深度：{st.get('max_depth_internal')} {cfg.unit.depth_label}")
        else:
            print("  去除量：不提供（threshold_only）")
        for w in result.warnings:
            print(f"  警告：{w}")
        for e in result.errors:
            print(f"  错误：[{e['code']}] {e['message']}")
        print(f"  输出文件：{', '.join(saved.written_files)}")
    if result.status in ("failed", "cancelled"):
        return EXIT_FAILED
    return EXIT_OK


def cmd_reference(args: argparse.Namespace) -> int:
    from . import references as _ref

    case = _ref.load_reference_case(args.config)

    # 材料卡：优先算例中的 material_card_file，其次按 material_dir + material_id 解析
    material = None
    card_file = case.get("material_card_file")
    if card_file:
        cand = Path(card_file)
        if not cand.is_absolute():
            for base in (Path(args.config).resolve().parent, project_root()):
                if (base / cand).exists():
                    cand = (base / cand).resolve()
                    break
        material = load_material_card(cand)
    else:
        catalog = load_material_catalog(args.material_dir)
        mid = case.get("material_id")
        if mid not in catalog:
            raise UFDemoError(
                "MATERIAL_CAPABILITY_MISSING",
                f"材料目录中找不到 id={mid!r}",
                field_path="material_id",
                actual=mid,
                requirement=f"目录中可用：{sorted(catalog)}",
                suggestion="补材料卡或修正 material_id。",
            )
        material = catalog[mid]

    result = _ref.ReferenceEvaluator.evaluate_case(case, material)

    base = Path(args.out_base).resolve() if args.out_base else default_runs_dir()
    if args.out:
        run_dir = Path(args.out).resolve()
        if run_dir.exists():
            raise UFDemoError(
                "CONFIG_INVALID",
                "指定的输出目录已存在，拒绝覆盖",
                field_path="--out",
                actual=str(run_dir),
                requirement="输出目录必须不存在",
                suggestion="换路径；或不传 --out 让工具自动生成带时间+随机后缀的目录。",
            )
        run_id = run_dir.name
    else:
        run_id = make_run_id(case.get("label") or "reference")
        run_dir = new_run_dir(base, run_id)

    run_result = _ref.build_reference_run_result(case, material, result)
    run_result.run_id = run_id
    code_info = code_version(project_root())
    saved = save_run(run_result, run_dir, project_root=project_root(), code_info=code_info)

    payload = result.to_dict()
    payload["run_dir"] = str(run_dir)
    payload["written_files"] = saved.written_files
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK

    print(f"参考算例：{result.case_id}（{result.reference_kind}）")
    print(f"材料：{result.material_id}｜证据状态：{result.evidence_status}｜条件匹配：{result.condition_match}")
    print(f"输出语义：{result.output_semantics}（{payload['output_semantics_zh']}）")
    print(f"运行目录：{run_dir}")
    units = result.units
    for k, v in result.values.items():
        if k == "sweep":
            continue
        if isinstance(v, float):
            print(f"  {k} = {v!r} {units.get(k, '')}".rstrip())
        else:
            print(f"  {k} = {v} {units.get(k, '')}".rstrip())
    for n in result.notes:
        print(f"  说明：{n}")
    for w in result.warnings:
        print(f"  警告：{w}")
    print("  事件核：不允许（平均率/累计深度不得进入逐事件增量主循环）")
    print(f"  输出文件：{', '.join(saved.written_files)}")
    return EXIT_OK


def cmd_table(args: argparse.Namespace) -> int:
    """查表（批次 F / T10）：只做插值取值，**不求解网格、不产生形貌**。"""
    from . import tables as _tab

    if args.all_curves:
        curves = _tab.load_curves(Path(args.curve))
        x_vals = args.x if args.x is not None else None
    else:
        curve = _tab.load_curve(args.curve)
        curves = [curve]
        x_vals = args.x if args.x is not None else [curve.valid_range[0], curve.valid_range[1]]

    payloads = []
    for c in curves:
        xs = x_vals if x_vals is not None else [c.valid_range[0], c.valid_range[1]]
        res = _tab.lookup(c, xs, method=args.method, allow_out_of_range=args.allow_out_of_range)
        payloads.append(res)

    if args.json:
        dumped = [r.to_dict() for r in payloads]
        print(json.dumps(dumped if args.all_curves else dumped[0], ensure_ascii=False, indent=2, default=str))
        return EXIT_OK

    for c, res in zip(curves, payloads):
        print(f"曲线：{c.curve_id}｜材料：{c.material_id}｜证据：{c.evidence_status}")
        print(f"  语义：{c.output_semantics} → {c.route_zh}")
        print(f"  横轴：{c.x_quantity['name']} [{c.x_unit}]｜纵轴：{c.y_quantity['name']} [{c.y_unit}]")
        print(f"  有效区间：{c.valid_range[0]!r} ≤ x ≤ {c.valid_range[1]!r}")
        print(f"  原始点 {len(c.raw_points)} 条｜可用点 {len(c.points)} 条｜重复策略：{c.duplicate_report.policy}")
        print(f"  来源：{c.source_figure_or_table}")
        print(f"  插值方法：{res.method}")
        for xv, val, ok in zip(res.x, res.values, res.in_range):
            shown = "越界（不提供）" if val is None else f"{val!r} {c.y_unit}"
            print(f"    x = {xv!r}（{'区间内' if ok else '越界'}）→ {shown}")
        print(f"  状态：{res.status}｜{res.reason}")
        if not c.can_enter_event_kernel:
            print("  事件核：不允许（本曲线语义不产生逐事件局部深度）")
        for n in c.notes:
            print(f"  说明：{n}")
        for n in c.limitations:
            print(f"  限制：{n}")
    return EXIT_OK


def cmd_materials(args: argparse.Namespace) -> int:
    catalog = load_material_catalog(args.material_dir)
    if not catalog:
        print(f"材料目录为空或不存在：{args.material_dir}")
        return EXIT_FAILED
    rows = capability_table(catalog)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return EXIT_OK
    by_material: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_material.setdefault(r["material_id"], []).append(r)
    for mid, rs in by_material.items():
        head = rs[0]
        print(f"== {mid}｜{head['family']}｜{head['grade']}｜证据：{head['evidence_status']}")
        for r in rs:
            flag = "可用" if r["available"] else "不可用"
            cond = "（有条件）" if r["conditional"] else ""
            print(f"   [{flag}{cond}] {r['capability']}：{r['reason']}")
            if r["missing"]:
                print(f"            缺少：{r['missing']}")
    return EXIT_OK


def cmd_inspect(args: argparse.Namespace) -> int:
    loaded = load_run(args.run_dir)
    if args.json:
        print(json.dumps(loaded.to_dict(), ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    print(f"运行 ID：{loaded.run_id}")
    print(f"状态：{loaded.status}")
    print(f"事件：{len(loaded.events)}（events.csv 记录数）")
    print(f"快照：{len(loaded.snapshots)}")
    print(f"表面文件：{loaded.surface.get('_source_file')}")
    key_stats = {k: loaded.statistics.get(k) for k in ("removal_volume_internal", "center_depth_internal", "max_depth_internal", "mean_depth_internal")}
    print(f"统计：{key_stats}")
    for w in loaded.warnings:
        print(f"  警告：{w}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ufdemo", description="七种材料超快激光加工 Demo（M0：最小 CLI 闭环）")
    p.add_argument("--version", action="version", version=f"ufdemo {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_material_dir(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--material-dir", default=str(default_material_dir()), help="材料目录（data/materials）")

    v = sub.add_parser("validate", help="校验配置与材料卡准入")
    v.add_argument("config")
    v.add_argument("--json", action="store_true")
    add_material_dir(v)
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("run", help="执行一次求解并导出")
    r.add_argument("config")
    r.add_argument("--out", default=None, help="输出运行目录")
    r.add_argument("--out-base", default=None, help="输出基目录（未指定 --out 时使用）")
    r.add_argument("--force-new-suffix", action="store_true", help="输出目录已存在时自动追加时间+随机后缀")
    r.add_argument("--json", action="store_true")
    add_material_dir(r)
    r.set_defaults(func=cmd_run)

    ref = sub.add_parser("reference", help="文献协议参考评估器（有效 N、阈值、平均率；不求解网格）")
    ref.add_argument("config")
    ref.add_argument("--out", default=None, help="输出运行目录")
    ref.add_argument("--out-base", default=None, help="输出基目录（未指定 --out 时使用）")
    ref.add_argument("--json", action="store_true")
    add_material_dir(ref)
    ref.set_defaults(func=cmd_reference)

    m = sub.add_parser("materials", help="打印材料能力表")
    m.add_argument("--json", action="store_true")
    add_material_dir(m)
    m.set_defaults(func=cmd_materials)

    tb = sub.add_parser(
        "table",
        help="查表插值（批次 F：分段线性/PCHIP；越界返回状态，不返回 0）",
    )
    tb.add_argument("curve", help="曲线卡路径（*.curve.json）或曲线目录（配 --all-curves）")
    tb.add_argument("--x", type=float, nargs="+", default=None,
                    help="查询点（可多个）；省略时取有效区间两端")
    tb.add_argument("--method", choices=("linear", "pchip"), default="linear",
                    help="插值方法：linear（默认分段线性）或 pchip（需 SciPy，extrapolate=False）")
    tb.add_argument("--allow-out-of-range", action="store_true",
                    help="越界时不报错，改为返回状态（越界项为 null，仍不填 0）")
    tb.add_argument("--all-curves", action="store_true",
                    help="把第一个参数当目录，对目录下所有曲线卡查表")
    tb.add_argument("--json", action="store_true")
    tb.set_defaults(func=cmd_table)

    ins = sub.add_parser("inspect", help="重读运行结果")
    ins.add_argument("run_dir")
    ins.add_argument("--json", action="store_true")
    ins.set_defaults(func=cmd_inspect)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except UFDemoError as err:
        _print_error(err, as_json=getattr(args, "json", False))
        return EXIT_FAILED
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断。", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
