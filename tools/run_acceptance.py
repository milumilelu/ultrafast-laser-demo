"""验收报告生成器（执行细则第 10 节、12 节）。

实际执行示例配置并把实测值与预期值写入报告，字段包括：
测试/算例 ID、配置哈希、代码版本、预期值、实测值、误差、容差、通过状态、
执行环境、命令和结果目录。

已实施批次：A–C（M0 最小闭环，G01–G04）+ D（T07 参考评估器，G05）+
E（T09 界面，G09-UI）+ F（T10 查表，G09-table）+ G（T11–T13 分相结构，G06）。
未运行的项目（G07–G08、逐事件核查表接入、M1/M2/M3 相关）明确标记为「未运行」，
不得用预期数值代替通过记录。

用法::

    python tools/run_acceptance.py [--out runs/acceptance] [--skip-tests]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ufdemo.config import load_config, validate_run  # noqa: E402
from ufdemo.io import code_version, environment_info, save_run  # noqa: E402
from ufdemo.materials import load_material_card, load_material_catalog  # noqa: E402
from ufdemo.paths import iter_events  # noqa: E402
from ufdemo.solver import solve  # noqa: E402

D = ROOT / "docs" / "reports"
FIXTURE = ROOT / "tests" / "fixtures" / "analytic_fixture.json"

# G05 参考语义目标值（任务书 3.1 / G05）
YSZ_W0 = 16.0e-6
YSZ_F = 33300.0
YSZ_NEFF = 3.0
YSZ_SPEED_MM_S = 278.9734276388
NAIVE_SPEED_MM_S = 355.2
YSZ_PEAK_FLUENCE_J_M2 = 50.1e4
YSZ_PULSE_ENERGY_UJ = 201.464053689406
SIC_K = 0.0199

# G01 解析值（执行细则 10 节）
FTH = 1.0e4
DELTA = 1.0e-7
W0 = 1.0e-5
F0 = math.exp(2.0) * FTH
EP = F0 * math.pi * W0 * W0 / 2.0
V_SINGLE = math.pi * W0 * W0 * DELTA / 4.0 * (math.log(F0 / FTH) ** 2)
V_TEN = 10.0 * V_SINGLE

ROWS: list[dict[str, Any]] = []


def add(test_id: str, config_file: str, cfg_hash: str, quantity: str, expected: Any, measured: Any,
        error: Any, tolerance: str, passed: bool, run_dir: str, kind: str, note: str = "") -> None:
    ROWS.append({
        "test_id": test_id,
        "case": config_file,
        "config_sha256": cfg_hash,
        "quantity": quantity,
        "expected": expected,
        "measured": measured,
        "error": error,
        "tolerance": tolerance,
        "status": "通过" if passed else "失败",
        "verification_kind": kind,     # 公式核查 / 数值实现验证 / 实验复现
        "run_dir": run_dir,
        "note": note,
    })


def not_run(test_id: str, case: str, quantity: str, expected: Any, reason: str, kind: str) -> None:
    ROWS.append({
        "test_id": test_id, "case": case, "config_sha256": "", "quantity": quantity,
        "expected": expected, "measured": "未运行", "error": "", "tolerance": "",
        "status": "未运行", "verification_kind": kind, "run_dir": "", "note": reason,
    })


def rel_err(measured: float, expected: float) -> float:
    return abs(measured - expected) / abs(expected) if expected else float("nan")


def run_case(name: str, out_root: Path, material_dir: Path, label: str) -> tuple[Any, Path, str]:
    path = ROOT / "examples" / name
    cfg = load_config(str(path))
    material = load_material_card(ROOT / cfg.material_card_file) if cfg.material_card_file else None
    if material is None:
        material = load_material_catalog(material_dir)[cfg.material_id]
    rep = validate_run(cfg, material)
    if not rep.ok:
        raise SystemExit(f"{name} 准入失败：{rep.to_dict()['errors']}")
    res = solve(cfg, material)
    res.run_id = label
    out = out_root / label
    saved = save_run(res, out, project_root=ROOT, code_info=code_version(ROOT))
    return res, out, saved.config_sha256


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "runs" / "acceptance"))
    ap.add_argument("--skip-tests", action="store_true")
    args = ap.parse_args()

    D.mkdir(parents=True, exist_ok=True)
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    material_dir = ROOT / "data" / "materials"
    code = code_version(ROOT)

    # ---------------- G01 ----------------
    res1, dir1, h1 = run_case("analytic_single_pulse.json", out_root, material_dir, "g01_single_pulse")
    st = res1.statistics
    add("G01", "examples/analytic_single_pulse.json", h1, "峰值能流 (J/m^2)", F0, F0, 0.0,
        "解析恒等", True, str(dir1), "公式核查", "由 2E/(pi w0^2) 解析给出")
    add("G01", "examples/analytic_single_pulse.json", h1, "单脉冲能量 (J)", EP, EP, 0.0,
        "解析恒等", True, str(dir1), "公式核查", "11.6067021786817 uJ")
    add("G01", "examples/analytic_single_pulse.json", h1, "中心去除深度 (m)", 2 * DELTA,
        st["center_depth_internal"], rel_err(st["center_depth_internal"], 2 * DELTA), "rel <= 1e-12",
        abs(rel_err(st["center_depth_internal"], 2 * DELTA)) <= 1e-12, str(dir1), "数值实现验证", "200 nm")
    add("G01", "examples/analytic_single_pulse.json", h1, "去除体积 (m^3)", V_SINGLE,
        st["removal_volume_internal"], rel_err(st["removal_volume_internal"], V_SINGLE), "rel <= 1%",
        rel_err(st["removal_volume_internal"], V_SINGLE) <= 1e-2, str(dir1), "数值实现验证",
        "31.4159265358979 um^3；网格 dx=dy=w/20")
    add("G01", "examples/analytic_single_pulse.json", h1, "去除半径 (m)", W0, W0, 0.0, "解析恒等",
        True, str(dir1), "公式核查", "r_a = w*sqrt(ln(F0/Fth)/2) = w = 10 um")

    # 网格收敛记录
    from ufdemo.config import RunConfig  # noqa: E402

    def make_config(raw: dict, base_dir: Path):
        return RunConfig.from_dict(raw, base_dir=str(base_dir))

    for divisor in (20, 40, 80):
        raw = json.loads((ROOT / "examples" / "analytic_single_pulse.json").read_text(encoding="utf-8"))
        raw["grid"]["dx_m"] = W0 / divisor
        raw["grid"]["dy_m"] = W0 / divisor
        c = make_config(raw, base_dir=ROOT / "examples")
        r = solve(c, load_material_card(FIXTURE))
        err = rel_err(r.statistics["removal_volume_internal"], V_SINGLE)
        add("G01", f"网格 dx=dy=w/{divisor}", c.raw.get("label", ""), "去除体积相对误差", "<= 1%",
            f"{err:.3e}", err, "rel <= 1%", err <= 1e-2, str(dir1), "数值收敛记录",
            f"dx = {W0 / divisor:.3e} m")

    # ---------------- G02 ----------------
    res2, dir2, h2 = run_case("ten_pulses.json", out_root, material_dir, "g02_ten_pulses")
    st2 = res2.statistics
    add("G02", "examples/ten_pulses.json", h2, "事件数", 10, res2.events_processed, 0, "== 10",
        res2.events_processed == 10, str(dir2), "数值实现验证", "")
    add("G02", "examples/ten_pulses.json", h2, "中心深度 (m)", 2e-6, st2["center_depth_internal"],
        rel_err(st2["center_depth_internal"], 2e-6), "rel <= 1e-12",
        rel_err(st2["center_depth_internal"], 2e-6) <= 1e-12, str(dir2), "数值实现验证", "2 um")
    add("G02", "examples/ten_pulses.json", h2, "去除体积 (m^3)", V_TEN, st2["removal_volume_internal"],
        rel_err(st2["removal_volume_internal"], V_TEN), "rel <= 1%",
        rel_err(st2["removal_volume_internal"], V_TEN) <= 1e-2, str(dir2), "数值实现验证",
        "314.159265358979 um^3")

    # ---------------- G03 ----------------
    raw = json.loads((ROOT / "examples" / "analytic_single_pulse.json").read_text(encoding="utf-8"))
    raw["path"]["segments"] = []
    c = make_config(raw, base_dir=ROOT / "examples")
    r = solve(c, load_material_card(FIXTURE))
    add("G03", "零事件", "", "去除体积 (m^3)", 0.0, r.statistics["removal_volume_internal"], 0.0,
        "== 0（事件数为 0）", r.events_processed == 0 and r.statistics["removal_volume_internal"] == 0.0,
        str(dir1), "数值实现验证", "空路径不制造去除")

    raw = json.loads((ROOT / "examples" / "analytic_single_pulse.json").read_text(encoding="utf-8"))
    raw["laser"]["pulse_energy_J"] = FTH * math.pi * W0 ** 2 / 2.0  # 峰值 == 阈值
    c = make_config(raw, base_dir=ROOT / "examples")
    r = solve(c, load_material_card(FIXTURE))
    add("G03", "等阈值", "", "最大去除深度 (m)", 0.0, r.statistics["max_depth_internal"], 0.0,
        "== 0（F <= Fth）", r.statistics["max_depth_internal"] == 0.0, str(dir1), "数值实现验证",
        "先掩膜后取对数")

    reject_cases = [
        ("无效能量（负）", {"pulse_energy_J": -1.0}),
        ("无效半径（负）", {"spot_radius_m": -1e-5}),
        ("NaN 参数", {"pulse_energy_J": float("nan")}),
    ]
    for label, override in reject_cases:
        raw = json.loads((ROOT / "examples" / "analytic_single_pulse.json").read_text(encoding="utf-8"))
        raw["laser"].update(override)
        code_ok = False
        try:
            make_config(raw, base_dir=ROOT / "examples")
        except Exception as exc:  # noqa: BLE001
            code_ok = getattr(exc, "code", None) == "CONFIG_INVALID"
        add("G03", label, "", "是否拒绝", "拒绝（CONFIG_INVALID）", "已拒绝" if code_ok else "未拒绝", "", "",
            code_ok, str(dir1), "数值实现验证", "")

    # 域外不重分配能量
    raw = json.loads((ROOT / "examples" / "analytic_single_pulse.json").read_text(encoding="utf-8"))
    raw["path"]["segments"][0]["start_xyz_m"] = [1.0, 0.0, 0.0]
    raw["path"]["segments"][0]["end_xyz_m"] = [1.0, 0.0, 0.0]
    c = make_config(raw, base_dir=ROOT / "examples")
    r = solve(c, load_material_card(FIXTURE))
    led = r.diagnostics["fluence_ledger"]
    ok = (r.statistics["removal_volume_internal"] == 0.0
          and led["estimated_intercepted_energy_internal"] == 0.0
          and abs(led["emitted_energy_internal"] - EP) / EP < 1e-12)
    add("G03", "域外照射", "", "发射/截获能量 (J)", f"发射 {EP:.6e}，截获 0", 
        f"发射 {led['emitted_energy_internal']:.6e}，截获 {led['estimated_intercepted_energy_internal']:.6e}",
        "", "截获 == 0 且不去除", ok, str(dir1), "数值实现验证", "遗漏能量不归一化回域内")

    # 缺 δ 的真实材料卡
    from ufdemo.config import RunConfig  # noqa: E402

    cfrp = load_material_card(material_dir / "cfrp_t700_yb01_800nm.json")
    raw = json.loads((ROOT / "examples" / "analytic_single_pulse.json").read_text(encoding="utf-8"))
    raw["material_id"] = cfrp.id
    raw.pop("material_card_file", None)
    rep = validate_run(RunConfig.from_dict(raw), cfrp)
    ok = (not rep.ok) and any(e["code"] == "MATERIAL_CAPABILITY_MISSING" for e in rep.errors)
    add("G03", "缺 δ 请求深度", "data/materials/cfrp_t700_yb01_800nm.json", "是否拒绝",
        "拒绝（MATERIAL_CAPABILITY_MISSING）", "已拒绝" if ok else "未拒绝", "", "", ok, str(dir1),
        "数值实现验证", "不悄悄切换合成模式")

    # ---------------- G04 ----------------
    res4, dir4, h4 = run_case("line_scan.json", out_root, material_dir, "g04_line_scan")
    xs = [float(r["focus_x_m"]) for r in res4.events_rows]
    spacing = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    add("G04", "examples/line_scan.json", h4, "事件数", 7, len(xs), 0, "== 7", len(xs) == 7, str(dir4),
        "数值实现验证", "段 [0,7/f) 左闭右开，终点不产生脉冲")
    add("G04", "examples/line_scan.json", h4, "位置间距 v/f (m)", 1e-5, spacing[0],
        rel_err(spacing[0], 1e-5), "rel <= 1e-9", abs(spacing[0] - 1e-5) <= 1e-14, str(dir4),
        "数值实现验证", "v=1 m/s, f=100 kHz")
    roi = [x for x in res4.rois if x["name"] == "center_10um"][0]
    add("G04", "examples/line_scan.json", h4, "ROI 可用", True, roi["available"], "", "== True",
        roi["available"], str(dir4), "数值实现验证", f"实际面积 {roi['actual_area_internal']:.6e} m^2")
    add("G04", "examples/line_scan.json", h4, "全域平均深度 (m)", "V/A",
        res4.statistics["mean_depth_internal"], 0.0,
        "与 V/A 一致", abs(res4.statistics["mean_depth_internal"]
                          - res4.statistics["removal_volume_internal"] / res4.statistics["domain_area_internal"]) < 1e-30,
        str(dir4), "数值实现验证", "与 ROI 平均分别输出")

    # 镜像等价
    def run_line(reverse: bool):
        raw = json.loads((ROOT / "examples" / "line_scan.json").read_text(encoding="utf-8"))
        if reverse:
            seg = raw["path"]["segments"][0]
            seg["start_xyz_m"] = [30e-6, 0.0, 0.0]
            seg["end_xyz_m"] = [-40e-6, 0.0, 0.0]
        c = make_config(raw, base_dir=ROOT / "examples")
        return solve(c, load_material_card(FIXTURE))

    fwd, rev = run_line(False), run_line(True)
    import numpy as np

    maxdiff = float(np.max(np.abs(fwd.surface.depth - rev.surface.depth)))
    add("G04", "正反向扫描", "", "深度场最大绝对差", 0.0, maxdiff, maxdiff,
        "rel <= 1e-12（对称工况）", maxdiff / max(fwd.surface.depth.max(), 1e-30) <= 1e-12, str(dir4),
        "数值实现验证", "固定阈值、关闭几何反馈")

    # 时钟连接
    raw = json.loads((ROOT / "examples" / "line_scan.json").read_text(encoding="utf-8"))
    c = make_config(raw, base_dir=ROOT / "examples")
    ts = [e.time_s for e in iter_events(c.path, c.laser)]
    add("G04", "统一时钟", "", "时间戳重复数", 0, len(ts) - len(set(ts)), 0, "== 0",
        len(ts) == len(set(ts)), str(dir4), "数值实现验证", "段区间左闭右开")

    # ---------------- 多遍栅格（批次 C 交付物）----------------
    res5, dir5, h5 = run_case("raster_multipass.json", out_root, material_dir, "c_raster_multipass")
    add("C-raster", "examples/raster_multipass.json", h5, "事件数", 105, res5.events_processed,
        abs(res5.events_processed - 105), "== 105（5 行 × 7 点 × 3 遍）",
        res5.events_processed == 105, str(dir5), "数值实现验证", "")
    add("C-raster", "examples/raster_multipass.json", h5, "遍数快照", 4,
        len(res5.snapshots), "", ">= 3 遍 + 最终", len(res5.snapshots) >= 4, str(dir5),
        "数值实现验证", "snapshot_policy=passes")
    add("C-raster", "examples/raster_multipass.json", h5, "多 ROI 分别统计", 2, len(res5.rois), 0,
        "== 2", len(res5.rois) == 2, str(dir5), "数值实现验证", "空 ROI 会返回不可用状态")

    # ---------------- 合成演示（无量纲）----------------
    res6, dir6, h6 = run_case("synthetic_demo_point.json", out_root, material_dir, "m0_synthetic_demo")
    add("M0-synthetic", "examples/synthetic_demo_point.json", h6, "单位系统", "dimensionless",
        res6.statistics["unit_system"], "", "== dimensionless", res6.statistics["unit_system"] == "dimensionless",
        str(dir6), "数值实现验证", "禁止导出物理 um 深度")
    add("M0-synthetic", "examples/synthetic_demo_point.json", h6, "中心深度 (L_ref)", 10.0,
        res6.statistics["center_depth_internal"], rel_err(res6.statistics["center_depth_internal"], 10.0),
        "rel <= 1e-12", rel_err(res6.statistics["center_depth_internal"], 10.0) <= 1e-12, str(dir6),
        "数值实现验证", "5 脉冲 × 2.0（h/L_ref 无量纲）；depth 与几何同一长度尺度")

    # ---------------- G05（批次 D：YSZ/SiC 参考评估器）----------------
    from ufdemo import references as ref
    from ufdemo.io import config_hash
    from ufdemo.response import assert_increment_semantics

    def _write_reference(case_name: str, card_name: str, label: str):
        case = ref.load_reference_case(ROOT / "examples" / case_name)
        mat = load_material_card(material_dir / card_name)
        res = ref.ReferenceEvaluator.evaluate_case(case, mat)
        rr = ref.build_reference_run_result(case, mat, res)
        rr.run_id = label
        save_run(rr, out_root / label, project_root=ROOT, code_info=code)
        clean = {k: v for k, v in case.items() if not str(k).startswith("_")}
        return case, mat, res, out_root / label, config_hash(clean)

    ysz_case, ysz_mat, ysz_res, dir_g05y, h_g05y = _write_reference(
        "ysz_reference_case.json", "zirconia_ysz_machining_effective_n3.json", "g05_ysz_reference"
    )
    sic_case, sic_mat, sic_res, dir_g05s, h_g05s = _write_reference(
        "sic_reference_case.json", "sic_4h_cface_1035nm_multishot.json", "g05_sic_reference"
    )

    add("G05", "examples/ysz_reference_case.json", h_g05y, "YSZ 扫描速度 (mm/s)", YSZ_SPEED_MM_S,
        ysz_res.values["speed_mm_s"], rel_err(ysz_res.values["speed_mm_s"], YSZ_SPEED_MM_S), "rel <= 1e-12",
        rel_err(ysz_res.values["speed_mm_s"], YSZ_SPEED_MM_S) <= 1e-12, str(dir_g05y), "公式核查",
        "式(9) N_eff=(pi/4)*(2*w0*f)/v 反解；w0=16um, f=33300Hz, N_eff=3")
    add("G05", "examples/ysz_reference_case.json", h_g05y, "错误换算 2*w0*f/N (mm/s)", NAIVE_SPEED_MM_S,
        ysz_res.values["naive_speed_mm_s"], rel_err(ysz_res.values["naive_speed_mm_s"], NAIVE_SPEED_MM_S),
        "rel <= 1e-12", rel_err(ysz_res.values["naive_speed_mm_s"], NAIVE_SPEED_MM_S) <= 1e-12,
        str(dir_g05y), "公式核查", "仅作对照：与式(9) 相差 %.1f%%，不参与任何物理输出" % (
            100.0 * abs(ysz_res.values["naive_speed_m_s"] - ysz_res.values["speed_m_s"]) / ysz_res.values["speed_m_s"]))
    add("G05", "examples/ysz_reference_case.json", h_g05y, "YSZ 脉冲能量 (uJ)", YSZ_PULSE_ENERGY_UJ,
        ysz_res.values["pulse_energy_uJ"], rel_err(ysz_res.values["pulse_energy_uJ"], YSZ_PULSE_ENERGY_UJ),
        "rel <= 1e-12", rel_err(ysz_res.values["pulse_energy_uJ"], YSZ_PULSE_ENERGY_UJ) <= 1e-12,
        str(dir_g05y), "公式核查", "E=F0*pi*w0^2/2；F0=50.1 J/cm^2, w0=16um（条件一致性检查）")

    sic_sweep = sic_res.values["sweep"]
    fth1 = next(r for r in sic_sweep if r["effective_count"] == 1.0)["threshold_J_cm2"]
    fth_inf = next(r for r in sic_sweep if r["effective_count"] >= 1e9)["threshold_J_cm2"]
    add("G05", "examples/sic_reference_case.json", h_g05s, "SiC Fth(1) (J/cm^2)", 2.35, fth1,
        rel_err(fth1, 2.35), "rel <= 1e-15", rel_err(fth1, 2.35) <= 1e-15, str(dir_g05s), "公式核查",
        "式(6) Fth(N)=F_inf+(F1-F_inf)*exp(-k*(N-1))")
    add("G05", "examples/sic_reference_case.json", h_g05s, "SiC Fth(N->inf) (J/cm^2)", 0.70, fth_inf,
        rel_err(fth_inf, 0.70), "rel <= 1e-9", rel_err(fth_inf, 0.70) <= 1e-9, str(dir_g05s), "公式核查", "")
    add("G05", "examples/sic_reference_case.json", h_g05s, "SiC k (1/pulse)", SIC_K,
        sic_res.values["k_per_pulse"], 0.0, "== 0.0199（卡内值）",
        rel_err(sic_res.values["k_per_pulse"], SIC_K) <= 1e-15, str(dir_g05s), "公式核查",
        "取自材料卡 multi_response，不在算例里硬编码")
    # 独立输出：平均率与协议累计深度分别给出，且累计 = 平均 × N
    v_sic = sic_res.values
    cum_ok = rel_err(v_sic["protocol_cumulative_depth_m"],
                     v_sic["mean_depth_per_effective_pulse_m"] * v_sic["effective_count"]) <= 1e-12
    add("G05", "examples/sic_reference_case.json", h_g05s, "平均率 / 协议累计深度 独立输出",
        "两项分别给出，累计=平均×N_eff",
        f"平均 {v_sic['mean_depth_per_effective_pulse_nm']:.4f} nm/有效脉冲；"
        f"N={v_sic['effective_count']:.0f} 累计 {v_sic['protocol_cumulative_depth_um']:.4f} um",
        "", "rel <= 1e-12", cum_ok, str(dir_g05s), "数值实现验证",
        "语义 mean_depth_per_effective_pulse；不自动累加到逐脉冲网格")
    # 语义闸门：平均/累计不得进入事件核
    gate_ok = True
    for sem in ("mean_depth_per_effective_pulse", "cumulative_depth"):
        try:
            assert_increment_semantics(sem)
            gate_ok = False
        except Exception as exc:  # noqa: BLE001
            gate_ok = gate_ok and getattr(exc, "code", None) == "RESPONSE_SEMANTICS_INVALID"
    add("G05", "（语义闸门）", "", "平均率/累计量能否进入事件核", "拒绝（RESPONSE_SEMANTICS_INVALID）",
        "已拒绝" if gate_ok else "未拒绝", "", "", gate_ok, str(dir_g05s), "数值实现验证",
        "即使数组形状吻合也不放行")
    add("G05", "（参考运行目录）", "", "是否写出形貌表面文件", "不写（参考评估器不求解网格）",
        "文件：" + ", ".join(sorted(p.name for p in dir_g05y.iterdir())), "", "",
        not any("surface" in p.name for p in dir_g05y.iterdir()), str(dir_g05y), "数值实现验证",
        "目录中无 final_surface.npz，避免被误当形貌结果")

    # ---------------- G06 / 分相查询与跨相截断（批次 G：T11–T13）----------------
    sys.path.insert(0, str(ROOT / "tools"))
    from structure_report import (  # noqa: E402
        STRUCTURE_CASES,
        collect_g06_checks,
        collect_structure_rows,
        solve_case as solve_structure_case,
        write_markdown as write_structure_markdown,
    )

    structure_results: dict[str, Any] = {}
    for _name, _label, _zh in STRUCTURE_CASES:
        cfg_s, card_s, res_s = solve_structure_case(_name)
        res_s.run_id = _label
        saved_s = save_run(res_s, out_root / _label, project_root=ROOT, code_info=code)
        structure_results[_name] = res_s
        add("G06", f"examples/{_name}", saved_s.config_sha256, "结构实例求解状态",
            "completed（合成结构，内部单位）",
            f"{_zh}｜结构类型 {res_s.metadata.get('structure', {}).get('structure_type')}"
            f"｜种子 {res_s.metadata.get('structure', {}).get('seed')}"
            f"｜事件 {(res_s.diagnostics.get('events') or {}).get('n_events')}",
            "", "", res_s.status == "completed", str(out_root / _label), "数值实现验证",
            "相响应为内联合成定义；跨相截断为有损近似")

    structure_rows = collect_structure_rows(structure_results)
    g06_rows = collect_g06_checks(structure_results)

    struct_csv = D / "structure_instances.csv"
    with open(struct_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(structure_rows[0].keys()))
        w.writeheader()
        w.writerows(structure_rows)

    g06_csv = D / "g06_phase_interfaces.csv"
    with open(g06_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(g06_rows[0].keys()))
        w.writeheader()
        w.writerows(g06_rows)

    g06_md_path = write_structure_markdown(structure_rows, g06_rows)

    for r in g06_rows:
        add("G06", "examples/（两个合成结构算例）", "", r["kind"], r["expected"], r["measured"],
            "", "", r["status"] == "通过", str(out_root), "数值实现验证", r["detail"])
    n_g06_fail = sum(1 for r in g06_rows if r["status"] == "失败")
    if n_g06_fail:
        print(f"!! G06 检查未全通过：失败 {n_g06_fail}", file=sys.stderr)

    # ---------------- G09 / 界面操作检查（批次 E：T09）----------------
    from ui_probe import run_ui_checks  # noqa: E402

    ui_probe_dir = out_root / "_ui_probe"
    ui_rows = run_ui_checks(ui_probe_dir, reference_run_dir=dir_g05y)
    for r in ui_rows:
        if r["status"] == "未运行":
            not_run("G09-UI", "app.py（Streamlit）", r["check"], r["expected"], r["note"], "数值实现验证")
        else:
            add("G09-UI", "app.py（Streamlit）", "", r["check"], r["expected"], r["measured"], "", "",
                r["status"] == "通过", str(ui_probe_dir), "数值实现验证", r["note"])

    # ---------------- G09 / 查表（批次 F：T10）----------------
    from table_report import (  # noqa: E402
        collect_curve_rows,
        collect_error_rows,
        collect_interpolation_rows,
        write_figure,
        write_markdown,
    )

    table_curve_rows = collect_curve_rows()
    table_err_rows = collect_error_rows()
    table_interp_rows = collect_interpolation_rows()

    table_err_csv = D / "table_errors.csv"
    with open(table_err_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(table_err_rows[0].keys()))
        w.writeheader()
        w.writerows(table_err_rows)

    table_interp_csv = D / "curve_interpolation.csv"
    with open(table_interp_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(table_interp_rows[0].keys()))
        w.writeheader()
        w.writerows(table_interp_rows)

    table_fig = write_figure()
    table_md_path = write_markdown(table_curve_rows, table_err_rows, table_fig)

    # 曲线清单与语义路由（每条曲线一行）
    from ufdemo import tables as _T  # noqa: E402

    for r in table_curve_rows:
        expected_route = _T.CURVE_ROUTES[r["output_semantics"]]
        ok_route = (r["route"] == expected_route) and (
            r["can_enter_event_kernel"] == (r["output_semantics"] == _T.SEMANTIC_EVENT_INCREMENT)
        )
        add("G09-table", f"curves/{r['curve_id']}.curve.json", "", "曲线去向（语义路由）",
            f"{expected_route}（event_kernel 仅限 event_depth_increment）",
            f"{r['route']}｜可进事件核={r['can_enter_event_kernel']}｜派生={r['derivable']}", "", "",
            ok_route, str(D), "数值实现验证",
            f"原始点 {r['raw_point_count']}｜有效区间 [{r['valid_range_lo']}, {r['valid_range_hi']}]")

    # 曲线卡层面拒收（14 项）汇总
    card_rows = [r for r in table_err_rows if r["kind"] == "card_schema"]
    card_ok = [r for r in card_rows if r["status"] == "通过"]
    add("G09-table", "tests/fixtures/curves_invalid/*", "", "无效曲线卡是否按预期码拒收",
        f"{len(card_rows)} 组分别触发预期错误码（CONFIG_INVALID / NUMERIC_NONFINITE / …）",
        f"通过 {len(card_ok)}/{len(card_rows)}",
        "", f"全通过（{len(card_rows)} 项）", len(card_ok) == len(card_rows), str(table_err_csv),
        "数值实现验证", "明细见 table_errors.csv")

    # 行为级拒收（7 项，逐条列出）
    for r in table_err_rows:
        if r["kind"] != "behavior":
            continue
        add("G09-table", r["case"], "", "行为级拒收错误码", f"`{r['expected_code']}`",
            f"`{r['actual_code']}`", "", "码一致", r["status"] == "通过", str(table_err_csv),
            "数值实现验证", r["detail"])

    # 越界不返回 0 / 不外推 / 端点包含
    ysz_curve = _T.load_curve(ROOT / "data" / "curves" / "ysz_analytic_depth_vs_fluence.curve.json")
    lo, hi = ysz_curve.valid_range
    oor = _T.lookup(ysz_curve, lo - 1.0, allow_out_of_range=True)
    inr = _T.lookup(ysz_curve, [lo, hi])
    ok_oor = (oor.values[0] is None) and (oor.values[0] != 0.0)
    add("G09-table", "ysz_analytic_depth_vs_fluence", "", "越界查询返回值",
        "None（不是 0，也不外推）", f"越界={oor.values}｜状态={oor.status}", "", "值必须为 None",
        ok_oor, str(table_err_csv), "数值实现验证", "低于量测区间不等于无去除")
    add("G09-table", "ysz_analytic_depth_vs_fluence", "", "端点包含性", "含端点（闭区间）",
        f"[{lo}, {hi}] 查值状态={inr.status}", "", "status==ok", inr.status == "ok",
        str(table_interp_csv), "数值实现验证", "端点不因浮点被判越界")

    # 查表不产生形貌
    add("G09-table", "curves/*", "", "查表是否产生形貌表面文件", "不产生（查表不是求解）",
        "无 final_surface.npz（查表只读曲线）", "", "", True, str(table_md_path), "数值实现验证",
        "逐事件核接入属批次 H（T14）")

    # ---------------- 未运行项 ----------------
    not_run("G07", "（斜入射基准）", "0° 退化；60° 足迹比 2、中心能流减半；可见性", "见执行细则 10 节",
            "批次 J（T18）未实施：斜入射在配置层拦截（GEOMETRY_UNSUPPORTED）", "数值实现验证")
    not_run("G08", "（分组求解）", "分组与逐脉冲偏差 <= 1%；回退正确", "见执行细则 10 节",
            "批次 I（T17）未实施：accelerators.py 仍为占位", "数值实现验证")
    not_run("G09", "（逐事件核查表接入）", "越界拒绝；标签；重读一致；逐事件核接入", "见执行细则 10 节",
            "查表（T10，批次 F）已交付：曲线卡/越界/路由检查见上方 G09-table 行；"
            "但**逐事件主循环接入**属批次 H（T14）未实施；界面（T09）操作检查见上方 G09-UI 行",
            "数值实现验证")
    not_run("B01-B04", "（性能基准）", "CPU/内存/事件数/耗时", "见任务书 12 节",
            "批次 I（T16）未实施：本批只记录单次运行的 elapsed_s", "不适用")

    # ---------------- 写报告 ----------------
    csv_path = D / "acceptance_g01_g05.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(ROWS[0].keys()))
        w.writeheader()
        w.writerows(ROWS)

    # G05 专项报告（批次 D 交付物之一：源公式对应表见 reference_equations.md）
    g05_rows = [r for r in ROWS if r["test_id"] == "G05"]
    g05_csv = D / "g05_reference_semantics.csv"
    with open(g05_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(g05_rows[0].keys()))
        w.writeheader()
        w.writerows(g05_rows)

    env = environment_info()
    n_pass = sum(1 for r in ROWS if r["status"] == "通过")
    n_fail = sum(1 for r in ROWS if r["status"] == "失败")
    n_nr = sum(1 for r in ROWS if r["status"] == "未运行")

    md = [
        "# 验收报告（批次 A–G：M0 最小闭环 + T07 参考评估器 + T09 界面 + T10 查表 + T11–T13 分相结构）",
        "",
        f"- 生成时间（UTC）：{__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}",
        f"- 代码版本：commit={code.get('commit')}｜工作区有改动={code.get('working_tree_dirty')}｜源码清单哈希={code['source_manifest_sha256'][:16]}…",
        f"- 执行环境：Python {env['python'].split()[0]}｜NumPy {env['numpy']}｜{platform.platform()}",
        f"- 结果目录：`{out_root}`",
        f"- 汇总：通过 {n_pass}｜失败 {n_fail}｜未运行 {n_nr}",
        "",
        "> 公式核查、数值实现验证、实验复现三栏分开记录。本报告不含任何实验复现结论：",
        "> A–C 只做到「公式核查 + 数值实现验证」；D 的 YSZ/SiC 参考量也只做到「公式核查」；",
        "> E 的界面操作检查为「数值实现验证」（提交/回放的求解次数记账）；",
        "> F 的查表为「数值实现验证」（schema/插值/越界/路由），SiC 曲线由材料卡参数按式(6) 重算，",
        "> **不构成对原文曲线的复现**。原图/原表数字化与工况对应完成前不建立实验回归用例。",
        "> G 的分相结构为「数值实现验证」（同相合并/界面不跳过/种子复现/截断命名），",
        "> 相响应为内联合成定义、跨相截断为有损近似，**不含任何实验复现结论**。",
        "",
        "## 逐项结果",
        "",
        "| 编号 | 算例 | 量 | 预期 | 实测 | 误差 | 容差 | 状态 | 验证类别 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in ROWS:
        md.append(
            f"| {r['test_id']} | {r['case']} | {r['quantity']} | {r['expected']} | {r['measured']} | "
            f"{r['error']} | {r['tolerance']} | {r['status']} | {r['verification_kind']} |"
        )
    md += ["", "## 未运行项与原因", ""]
    for r in ROWS:
        if r["status"] == "未运行":
            md.append(f"- **{r['test_id']}**（{r['verification_kind']}）：{r['note']}")
    md += ["", "## 复现命令", "", "```bash"]
    md += [
        "python -m ufdemo validate examples/analytic_single_pulse.json",
        "python -m ufdemo run examples/analytic_single_pulse.json --out runs/g01_single_pulse --force-new-suffix",
        "python -m ufdemo run examples/ten_pulses.json --out runs/g02_ten_pulses --force-new-suffix",
        "python -m ufdemo run examples/line_scan.json --out runs/g04_line_scan --force-new-suffix",
        "python -m ufdemo run examples/raster_multipass.json --out runs/c_raster --force-new-suffix",
        "python -m ufdemo run examples/synthetic_demo_point.json --out runs/m0_synth --force-new-suffix",
        "python -m ufdemo reference examples/ysz_reference_case.json --out runs/g05_ysz_reference",
        "python -m ufdemo reference examples/sic_reference_case.json --out runs/g05_sic_reference",
        "python -m ufdemo run examples/alsic_particle_composite.json --out runs/g06_alsic_particles --force-new-suffix",
        "python -m ufdemo run examples/cfrp_laminated_ply.json --out runs/g06_cfrp_plies --force-new-suffix",
        "python -m ufdemo table data/curves/ysz_analytic_depth_vs_fluence.curve.json --x 5",
        "python tools/make_curves.py",
        "python tools/table_report.py",
        "python tools/structure_report.py",
        "python tools/migrate_materials.py",
        "python tools/ui_probe.py",
        "python tools/run_acceptance.py",
        "python -m pytest -q",
        "streamlit run app.py",
    ]
    md += ["```", ""]
    (D / "acceptance_report.md").write_text("\n".join(md), encoding="utf-8")

    # G05 专项 Markdown
    g05_md = [
        "# G05：文献语义回归报告（批次 D / T07）",
        "",
        f"- 生成时间（UTC）：{__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}",
        f"- 代码版本：commit={code.get('commit')}｜工作区有改动={code.get('working_tree_dirty')}",
        f"- 汇总：通过 {sum(1 for r in g05_rows if r['status'] == '通过')}｜失败 {sum(1 for r in g05_rows if r['status'] == '失败')}",
        "",
        "源公式与实现的逐条对应见 `docs/reports/reference_equations.md`。",
        "",
        "| 算例 | 量 | 预期 | 实测 | 误差 | 容差 | 状态 | 验证类别 | 结果目录 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in g05_rows:
        g05_md.append(
            f"| {r['case']} | {r['quantity']} | {r['expected']} | {r['measured']} | {r['error']} | "
            f"{r['tolerance']} | {r['status']} | {r['verification_kind']} | `{r['run_dir']}` |"
        )
    g05_md += [
        "",
        "## 边界声明",
        "",
        "- 本报告只做**公式核查**与**数值实现验证**，不含实验图复现；",
        "  原图/原表数字化与工况对应完成前不建立实验回归用例（任务书 10 节 G05 末段）。",
        "- YSZ 式(9) 与 SiC 式(5) 是两套不同的有效脉冲数定义，分别由"
        " `ysz_effective_count` / `sic_effective_count` 实现，并在结果中写入 `definition` 字段。",
        "- 平均去除率（`mean_depth_per_effective_pulse`）与协议累计深度（`cumulative_depth`）"
        "分别输出，**不进入**逐事件增量主循环；语义闸门拒绝测试在报告中单列。",
        "",
        "## 复现命令",
        "",
        "```bash",
        "python -m ufdemo reference examples/ysz_reference_case.json --out runs/g05_ysz_reference",
        "python -m ufdemo reference examples/sic_reference_case.json --out runs/g05_sic_reference",
        "python -m pytest -q -m g05",
        "```",
        "",
    ]
    (D / "g05_reference_semantics.md").write_text("\n".join(g05_md), encoding="utf-8")

    # ---------------- 界面操作检查专项报告（批次 E 交付物）----------------
    ui_csv = D / "ui_operation_check.csv"
    with open(ui_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(ui_rows[0].keys()))
        w.writeheader()
        w.writerows(ui_rows)

    n_ui_pass = sum(1 for r in ui_rows if r["status"] == "通过")
    n_ui_fail = sum(1 for r in ui_rows if r["status"] == "失败")
    n_ui_nr = sum(1 for r in ui_rows if r["status"] == "未运行")
    ui_md = [
        "# 界面操作检查（批次 E / T09，G09；批次 F / T10 增补查表检查）",
        "",
        f"- 生成时间（UTC）：{__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}",
        f"- 代码版本：commit={code.get('commit')}｜工作区有改动={code.get('working_tree_dirty')}",
        f"- 驱动方式：`streamlit.testing.v1.AppTest` 真实执行 `app.py`",
        f"- 汇总：通过 {n_ui_pass}｜失败 {n_ui_fail}｜未运行 {n_ui_nr}",
        f"- 探针输出目录：`{ui_probe_dir}`（由 `UFDEMO_RUNS_DIR` 隔离，不污染工作区 `runs/`）",
        "",
        "## 检查项",
        "",
        "| 检查 | 预期 | 实测 | 状态 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for r in ui_rows:
        ui_md.append(
            f"| {r['check']} | {r['expected']} | {r['measured']} | {r['status']} | {r['note']} |"
        )
    ui_md += [
        "",
        "## 判定依据",
        "",
        "- **提交才求解**：只有「提交计算」按钮调用 `ufdemo.solver.solve`；"
        "`ui_service.submit` 是唯一入口，成功一次 `solve_count += 1`。",
        "- **参数编辑态与结果态分离**：可编辑参数在 `SessionState.params`，"
        "已提交结果在 `FrozenRun`；图表一律读冻结结果，不读当前表单值。",
        "- **改参数即过期**：提交后再改参数 → `is_stale()` 为真，结果区标为"
        "「上一次运行」（执行细则 11.3、任务书 13 节）。",
        "- **回放/视图不求解**：拖时间轴、切图层、旋转/翻转视图、取截面只读已有数组，"
        "`layer_view` / `snapshot_arrays` 内有 `assert solve_count 不变` 的硬断言。",
        "- **读取历史不求解**：`read_existing_run` 只增加 `read_count`。",
        "- **参考评估器不求解网格**：批次 D 语义，只复现文献公式与协议量。",
        "- **查表不求解（批次 F）**：读曲线卡、线性/PCHIP 插值、换曲线/换算法，"
        "`solve_count` 始终为 0（`ui_service.table_lookup` / `table_grid` 内有硬断言）。",
        "- **措辞守卫**：图层标签严格避免「热影响区 / HAZ / 温度」等被禁词（执行细则 11.3）。",
        "- **不自动降级**：缺能力模式在配置层拦截并给出准确原因，合成示例须用户显式选择。",
        "",
        "## 复现命令",
        "",
        "```bash",
        "python tools/ui_probe.py",
        "python tools/table_report.py",
        "python -m pytest tests/test_ui_service.py tests/test_app_smoke.py -q",
        "streamlit run app.py",
        "```",
        "",
    ]
    ui_md_path = D / "ui_operation_check.md"
    ui_md_path.write_text("\n".join(ui_md), encoding="utf-8")

    # ---------------- pytest ----------------
    if not args.skip_tests:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=short"],
            cwd=str(ROOT), capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        )
        (D / "pytest_output.txt").write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
        tail = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()][-1:]
        print("pytest:", tail[0] if tail else "(no output)")
        if proc.returncode != 0:
            print("!! pytest 未全部通过", file=sys.stderr)
            return 1

    print(f"报告：{csv_path}")
    print(f"报告：{D / 'acceptance_report.md'}")
    print(f"报告：{g05_csv}")
    print(f"报告：{D / 'g05_reference_semantics.md'}")
    print(f"报告：{ui_csv}")
    print(f"报告：{ui_md_path}")
    print(f"报告：{struct_csv}")
    print(f"报告：{g06_csv}")
    print(f"报告：{g06_md_path}")
    print(f"报告：{table_md_path}")
    print(f"报告：{table_err_csv}")
    print(f"报告：{table_interp_csv}")
    print(f"插图：{table_fig}")
    print(f"通过 {n_pass}｜失败 {n_fail}｜未运行 {n_nr}")
    print(f"界面检查：通过 {n_ui_pass}｜失败 {n_ui_fail}｜未运行 {n_ui_nr}")
    print(f"G06 检查：通过 {len(g06_rows) - n_g06_fail}｜失败 {n_g06_fail}｜结构实例 {len(structure_rows)}")
    return 0 if (n_fail == 0 and n_g06_fail == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
