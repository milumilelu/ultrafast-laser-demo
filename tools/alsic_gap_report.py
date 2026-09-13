"""AlSiC 缺口审计（U10）。

**本文件是「未取得即未完成」的证据**：它不生成数据，只做两件事——

1. 把缺口**如实、可审计**地记录下来（含本轮实际检索证据与排除项）；
2. **断言这个缺口没有被偷偷填上**：数据包里不得出现纳秒体制冒充飞秒，
   也不得用相近材料（纯 AA2024、烧结 SiC）冒充 SiCp/AA2024。

为什么要写成代码而不是写段说明：缺口最容易被「为了让报告好看」而悄悄补掉
（用纳秒数据、或用相近材料的数据顶替）。把它变成会失败的断言，
才能让「这份数据包里到底有没有 AlSiC 飞秒数值」有一个**可复核**的答案。

输出：``docs/reports/alsic_gap.md``
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MEASURED = ROOT / "data" / "measured"
REPORTS = ROOT / "docs" / "reports"

#: 材料卡里 AlSiC 的 family 写法（中英文都算）
ALSIC_FAMILY_MARKERS = ("alsic", "铝基碳化硅", "sicp", "aluminum silicon carbide")

#: 纳秒体制的标记词（出现即视为纳秒数据混入风险）
NANOSECOND_MARKERS = ("nanosecond", "ns_regime", "纳秒", "_ns_", "ns激光")

#: 相近材料（**可以作基体参考，但不得冒充 AlSiC 实验**）
PROXY_MATERIAL_MARKERS = ("aa2024", "aluminum alloy", "纯铝", "sintered sic", "烧结sic")


def scan_dataset_files() -> dict[str, Any]:
    """扫描 `data/measured/`，统计各族的记录数与体制标记。"""
    out: dict[str, Any] = {"files": [], "alsic_rows": [], "nanosecond_rows": [],
                           "proxy_only_rows": []}
    for p in sorted(MEASURED.glob("*.csv")):
        rows = list(csv.DictReader(p.read_text(encoding="utf-8-sig").splitlines()))
        fams: set[str] = set()
        for r in rows:
            fam = " ".join(str(r.get(k, "") or "") for k in
                           ("material_family", "material_name", "recipe_label")).lower()
            fams.add(fam)
            is_alsic = any(m in fam for m in ALSIC_FAMILY_MARKERS)
            is_ns = any(m in fam for m in NANOSECOND_MARKERS) or \
                "nanosecond" in str(r.get("pulse_regime", "") or "").lower()
            if is_alsic:
                out["alsic_rows"].append({"file": p.name, **r})
            if is_ns:
                out["nanosecond_rows"].append({"file": p.name, **r})
            # 只含代理材料、却声称是复合材料
            if not is_alsic and any(m in fam for m in PROXY_MATERIAL_MARKERS) \
                    and "composite" in fam:
                out["proxy_only_rows"].append({"file": p.name, **r})
        out["files"].append({"file": p.name, "rows": len(rows),
                             "families": sorted(fams)[:4]})
    return out


def manifest_entries() -> list[dict[str, Any]]:
    return json.loads((MEASURED / "external_assets_manifest.json").read_text(encoding="utf-8"))


def main() -> int:
    scan = scan_dataset_files()
    entries = manifest_entries()
    alsic_asset = next((e for e in entries if e["id"] == "A02"), None)
    if alsic_asset is None:
        print("external_assets_manifest.json 里缺少 A02（AlSiC）条目")
        return 2

    n_alsic = len(scan["alsic_rows"])
    n_ns = len(scan["nanosecond_rows"])
    n_proxy = len(scan["proxy_only_rows"])

    L = [
        "# AlSiC 数据缺口审计（U10）",
        "",
        "## 结论",
        "",
        f"- 数据包内 AlSiC 记录：**{n_alsic} 条**",
        f"- manifest A02 状态：`{alsic_asset['status']}`（`is_gap = {alsic_asset.get('is_gap')}`）",
        "",
        "> **本轮未取得 AlSiC 的可用飞秒数值，U10 记为「未完成」。**",
        "> 这是**如实报缺**，而不是「因为牌号不匹配而拒绝数据」——",
        "> 缺的是**数值本身**（全文与图表在订阅墙后），不是牌号一致性。",
        "",
        "## 缺口原因",
        "",
        alsic_asset.get("gap_reason", "（未记录原因）"),
        "",
        "## 目标条件与排除项",
        "",
        f"- 目标论文：`{alsic_asset['doi']}`（{alsic_asset.get('pulse_regime_required', '')}）",
        "",
    ]
    for k, v in (alsic_asset.get("explicitly_excluded") or {}).items():
        L.append(f"- **{k}**：{v}")
    L += [
        "",
        "## 本轮检索证据",
        "",
    ]
    for ev in alsic_asset.get("search_evidence", []):
        L.append(f"- {ev}")

    L += [
        "",
        "## 数据包现状（自动扫描）",
        "",
        "| 文件 | 行数 | 族（前几个） |",
        "|---|---:|---|",
    ]
    for f in scan["files"]:
        L.append(f"| `{f['file']}` | {f['rows']} | {'；'.join(f['families'])} |")

    L += [
        "",
        "### 核对结果（三条「没有被偷偷填上」的断言）",
        "",
        f"- **纳秒体制混入飞秒包的记录数：{n_ns}**（必须为 0）",
        f"- **以相近材料冒充复合材料的记录数：{n_proxy}**（必须为 0）",
        f"- **AlSiC 记录数：{n_alsic}**（当前为 0，故本项未完成）",
        "",
        "## 下一步",
        "",
        alsic_asset.get("next_action", ""),
        "",
        alsic_asset.get("does_not_block_other_families", ""),
        "",
        "## 复现",
        "",
        "```bash",
        "python tools/alsic_gap_report.py",
        "```",
        "",
        "该脚本**只审计、不造数据**；有失败项（如发现纳秒混入）时退出码非 0。",
    ]
    REPORTS.mkdir(parents=True, exist_ok=True)
    md = REPORTS / "alsic_gap.md"
    md.write_text("\n".join(L) + "\n", encoding="utf-8")

    print(f"  AlSiC 记录数       = {n_alsic}")
    print(f"  纳秒混入记录数     = {n_ns}   （必须 0）")
    print(f"  相近材料冒充记录数 = {n_proxy}（必须 0）")
    print(f"  manifest A02 状态  = {alsic_asset['status']}")
    print(f"报告：{md}")

    # 只有「纳秒混入」或「代理冒充」才算硬失败；
    # AlSiC 记录为 0 是**已知缺口**（本项未完成），不算脚本失败，
    # 但会在输出与报告里如实标注。
    if n_ns or n_proxy:
        print("\n  ✗ 发现红线问题（纳秒混入或相近材料冒充）")
        return 1
    print("\n  ✓ 无纳秒混入、无相近材料冒充（缺口如实保留）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
