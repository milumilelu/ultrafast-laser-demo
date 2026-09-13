"""从**实测数据**生成曲线卡（U07）—— 让真实数据能驱动求解器。

与 `tools/make_curves.py` 的分工：

* `make_curves.py` 生成 **fixture**（公式/解析/合成），只服务数值测试；
* 本脚本生成 **measured** 卡，`entry_class="measured"`，**进默认入口**。

为什么需要它（这是本工程的一个真实断点）
----------------------------------------
`SOURCE_TYPES` 原先只有「解析定义 / 合成 / 迁移 / 用户表」四种，
**真实实验数据在曲线卡层无处安放** → `data/curves/` 里只能有 fixture →
`build_pulse_law` 永远拿不到实测曲线 → 求解器只能"合成演示"。
本脚本打通这条通路。

**必须显式声明的假设**（写进卡里，不藏在文档）
----------------------------------------------
1. 实测点报的是「**峰值中心**能流 → 中心坑深」。把它当作**局部**增量用，
   等于假设「局域去除量只由该点的局域能流决定」，且**未独立标定 Gaussian 径向分布**。
2. 4 个实测点**不含**完整阈值律与光斑尾部响应：最小实测能流**不是**阈值。
   下界以下按 `threshold_rule` 处理（默认 `reject`；本卡选 `zero_below` 并给出理由）。
3. 4 点**无重复测量** → `evidence_status` 只能是 `literature_reported`，
   **不得**升级为 `experiment_reproduced` / `independently_validated`。

用法::

    python tools/make_measured_curves.py
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MEASURED = ROOT / "data" / "measured"
CURVES = ROOT / "data" / "curves"


def _write_points(path: Path, rows: list[tuple[float, float]], note: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write("# ufdemo curve points — 单位见同目录 *.curve.json 的 x_quantity/y_quantity\n")
        fh.write(f"# {note}\n")
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["x", "y"])
        for x, y in rows:
            w.writerow([repr(float(x)), repr(float(y))])


def _dump(path: Path, obj: dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_sic4h_crater() -> dict[str, Any]:
    """由 `sic_single_pulse_measured.csv` 的 4 个实测点生成增量曲线卡。

    只转录**实验值**；原文旁边的模型预测值（11.2/14.3/17.0 μm）**不得**混入。
    """
    src = MEASURED / "sic_single_pulse_measured.csv"
    rows = list(csv.DictReader(src.read_text(encoding="utf-8-sig").splitlines()))
    pts = [(float(r["peak_fluence_J_cm2"]), float(r["depth_m"])) for r in rows]
    pts.sort()
    if len(pts) != 4:
        raise SystemExit(f"预期 4 个实测点，实际 {len(pts)}")

    cid = "sic4h_measured_single_pulse_crater"
    x_lo, x_hi = pts[0][0], pts[-1][0]
    _write_points(
        CURVES / f"{cid}.points.csv", pts,
        note=("Micromachines 15(5):573 实验值（Figures 3 & 7 正文明确数值）；"
              "1030 nm / 300 fs / 单脉冲；未混入同文模型预测值"),
    )

    card = {
        "schema_version": "1.0",
        "curve_id": cid,
        "entry_class": "measured",          # **进默认入口**
        "material_id": "sic_4h_cface_1035nm_multishot",
        "material_identity": {
            "family": "SiC",
            "grade": "monocrystalline 4H-SiC",
            "material_identity_confirmed_by_user": False,
        },
        "x_quantity": {
            "name": "peak_fluence",
            "unit": "J/cm^2",
            "label": "峰值能流（入射表面峰值）",
            "kind": "fluence",
        },
        "y_quantity": {
            "name": "removal_depth_per_pulse",
            "unit": "m",
            "label": "单脉冲去除深度（坑深）",
            "kind": "depth",
        },
        "output_semantics": "event_depth_increment",   # 唯一可进主循环的语义
        "depth_direction": "surface_normal",
        "fluence_basis": "incident_surface_peak",
        "fixed_conditions": {
            # 原文 1030 nm；材料卡标称 1035 nm —— 留 1% 容差并在 limitations 里说明。
            "wavelength_m": {"value": 1030e-9, "rel_tol": 0.01,
                             "note": "原文 1030 nm；材料卡标称 1035 nm，差 0.5% 在容差内"},
            "pulse_duration_s": {"value": 300e-15, "rel_tol": 0.10,
                                 "note": "原文 300 fs"},
        },
        "protocol": {
            "protocol_id": "measured_single_pulse_crater_depth",
            "required_history": {"mode": "none"},
            "note": "单脉冲、无历史耦合；每个事件独立查表。",
        },
        "source_figure_or_table": "Micromachines 2024, 15(5):573 — 正文实验值（Figures 3 & 7）",
        "valid_range": {
            "x": [x_lo, x_hi],
            "note": (f"4 个实测峰值能流 {x_lo:g}–{x_hi:g} J/cm²。"
                     "**最小实测能流不是阈值** —— 真实阈值更低且未测。"),
        },
        "points_file": f"{cid}.points.csv",
        "duplicate_policy": "reject",
        "evidence_status": "literature_reported",   # 4 点无重复测量，不得再升级
        "source_type": "published_measurement",      # U07 新增的实测来源类型
        # 曲线自带的截断规则：下界以下**显式**按无去除处理，并给出理由。
        # 这是**工程截断假设**，不是实测结论 —— 它会让低能流区**系统性低估**去除，
        # 方向是保守的（宁可少算，不多算）。
        "threshold_rule": {
            "mode": "zero_below",
            "reason": ("实测下界以下未采样；按「无去除」处理是**保守截断**"
                       "（真实阈值 < 6.92 J/cm²，故本卡低估低能流区去除）。"
                       "该假设已列入 limitations，不得当作实测阈值。"),
        },
        "source": {
            "source_ids": ["D03"],
            "doi": "10.3390/mi15050573",
            "url": "https://www.mdpi.com/2072-666X/15/5/573",
            "locator": "Results 正文实验值（Figures 3 & 7）",
            "license": "CC BY 4.0",
        },
        "notes": [
            "本卡由 `tools/make_measured_curves.py` 从实测 CSV 生成，**不是**解析 fixture。",
            "4 个点**全部**来自论文实验值；同文的模型预测值已排除。",
            "单脉冲、无重复测量 → 证据状态只能停在 `literature_reported`。",
        ],
        "limitations": [
            "**峰值中心关系近似局部响应**：实测报的是中心坑深，"
            "当作局域增量用等于假设「局域去除只由局域能流决定」；"
            "Gaussian 径向分布**未独立标定**。",
            "**不含完整阈值律与光斑尾部响应**：最小实测能流 6.92 J/cm² 不是阈值。",
            "**低能流区系统性低估**：下界以下按 0 计（保守截断），真实去除可能大于 0。",
            "**4 点无重复测量**：不构成实验复现，也不得升级证据状态。",
            "**不适用**于扫描/多遍的累计深度（那是另一语义，见 U06/U09 的数据）。",
        ],
    }
    _dump(CURVES / f"{cid}.curve.json", card)
    return {"curve_id": cid, "points": pts, "card": card}


def main() -> int:
    CURVES.mkdir(parents=True, exist_ok=True)
    info = make_sic4h_crater()
    print(f"  已生成实测曲线卡：{info['curve_id']}")
    print(f"    点数 {len(info['points'])}："
          + "、".join(f"{x:g} J/cm²→{y*1e6:.2f} μm" for x, y in info["points"]))
    print("    entry_class = measured（进默认入口）")
    print("    output_semantics = event_depth_increment（可进逐事件主循环）")
    print("    evidence_status = literature_reported（4 点无重复测量，不得升级）")
    print(f"  产物：{CURVES / (info['curve_id'] + '.curve.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
