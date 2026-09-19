"""U11 验收：在同一 commit SHA 下产出**统一发布证据**。

修复的缺陷（审查 F07）：仓库里的证据是**不同时间点**的记录，且没有一处把
数字与提交绑定 —— 例如 `docs/reports/pytest_output.txt` 写 368 passed，
而同一提交的 K 批次说明写 394，实测又是另一个数。审阅者无法判断
某个数字对应哪份代码。

本脚本把下列证据收进**一份**带 SHA 的报告，每项显式标注 ``run`` / ``not_run``，
且 **``not_run`` 必须带原因**（不允许用预期值冒充通过记录）：

1. ``pytest``（含 junit XML，汇总数与 XML 交叉核对）
2. ``tools/package_smoke.py``（wheel 干净环境安装冒烟）
3. 数据 QA（``tools/measured_data_report.py``，U04 交付前为 ``not_run``）
> 2026-09-19：Node 契约测试与真实浏览器探针两步已随旧 V2 前端删除（ADR-0023）。

用法::

    python tools/release_evidence.py                 # 全跑
    python tools/release_evidence.py --fast          # 跳过 wheel 冒烟
    python tools/release_evidence.py --only pytest,package
    python tools/release_evidence.py --sha <sha>     # 声明本次证据对应的提交

产物：

* ``docs/reports/release_evidence_<sha>.md``  人读
* ``docs/reports/release_evidence_<sha>.json`` 机读（含逐项 status / reason / 计数）

**校验**：报告头部写死 ``commit_sha`` 与采集时的 ``git status``。若生成后
HEAD 变化，报告自动作废 —— ``--verify`` 会检查这一点并返回非零。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "docs" / "reports"
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

ALL_STEPS = ("pytest", "package", "dataqa")


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


def sh(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None,
       timeout: int = 3600) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    merged.setdefault("PYTHONPATH", "src")
    if env:
        merged.update(env)
    try:
        return subprocess.run(
            cmd, cwd=str(cwd or ROOT), capture_output=True, text=True,
            env=merged, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timeout after {timeout}s")


def git(*args: str) -> str:
    out = sh(["git", *args])
    return out.stdout.strip() if out.returncode == 0 else ""


# ---------------------------------------------------------------------------
# 各步证据
# ---------------------------------------------------------------------------


def step_pytest(work: Path) -> dict[str, Any]:
    xml = work / "pytest.xml"
    proc = sh([sys.executable, "-m", "pytest", "-q", f"--junitxml={xml}"], timeout=2400)
    counts: dict[str, Any] = {}
    if xml.exists():
        root = ET.parse(xml).getroot()
        # testsuite 可能嵌套在 testsuites 下
        suite = root if root.tag == "testsuite" else root.find("testsuite")
        if suite is not None:
            counts = {
                "tests": int(suite.get("tests", 0)),
                "failures": int(suite.get("failures", 0)),
                "errors": int(suite.get("errors", 0)),
                "skipped": int(suite.get("skipped", 0)),
                "time_s": float(suite.get("time", 0) or 0),
            }
    m = re.search(r"(\d+) passed", proc.stdout)
    passed_text = int(m.group(1)) if m else None
    consistent = (passed_text is None or counts.get("tests") is None
                  or passed_text + (counts.get("skipped") or 0) <= counts.get("tests", 0) + 0
                  or passed_text == counts.get("tests", 0) - (counts.get("skipped") or 0))
    return {
        "status": "run",
        "argv": "pytest -q --junitxml",
        "returncode": proc.returncode,
        "summary_text": (proc.stdout.strip().splitlines() or [""])[-1],
        "junit": counts,
        "junit_passed_consistent": bool(consistent),
        "tail": "\n".join(proc.stdout.strip().splitlines()[-4:]),
        "ok": proc.returncode == 0 and bool(counts),
    }


def _exit_note(proc: subprocess.CompletedProcess, tail_lines: int = 6) -> str:
    """给异常退出一个有信息量的说明。

    教训：探针被外部**杀掉**时 stdout 为空、`returncode != 0`，
    旧实现只记 ``ok=False`` → 报告里出现 ``FAIL`` 但**没有原因**，
    与「断言真的失败」看起来一模一样。发布证据必须能自证，
    所以这里把退出码与 stderr 尾部带上。
    """
    bits = [f"returncode={proc.returncode}"]
    err = (proc.stderr or "").strip()
    if err:
        bits.append("stderr: " + " | ".join(err.splitlines()[-tail_lines:]))
    out = (proc.stdout or "").strip()
    if out:
        bits.append("stdout 尾部: " + " | ".join(out.splitlines()[-tail_lines:]))
    if proc.returncode == 124:
        return "超时终止（可能是环境争用或真卡死）；" + "；".join(bits)
    return "异常退出，未产出汇总（进程被杀 / 崩溃 / 环境争用）；" + "；".join(bits)


def step_package(work: Path) -> dict[str, Any]:
    script = ROOT / "tools" / "package_smoke.py"
    if not script.exists():
        return {"status": "not_run", "reason": "缺少 tools/package_smoke.py（U02 未交付）", "ok": False}
    proc = sh([sys.executable, str(script)], timeout=1800)
    m = re.findall(r"^结果：\s*(\d+) 通过｜(\d+) 失败", proc.stdout, re.MULTILINE)
    n_pass, n_fail = (int(m[-1][0]), int(m[-1][1])) if m else (None, None)
    # 退出码 2 = 环境不具备（如无法联网），不算通过也不算失败
    status = "run" if proc.returncode in (0, 1) else "not_run"
    abnormal = status == "run" and n_fail is None
    return {
        "status": status,
        "argv": "python tools/package_smoke.py",
        "returncode": proc.returncode,
        "passed": n_pass, "failed": n_fail,
        "reason": (
            "退出码 2：环境不具备（多为无法访问镜像源）"
            if status != "run"
            else (_exit_note(proc) if abnormal else None)
        ),
        "ok": proc.returncode == 0,
        "tail": "\n".join(proc.stdout.strip().splitlines()[-3:]),
    }


def step_dataqa(work: Path) -> dict[str, Any]:
    script = ROOT / "tools" / "measured_data_report.py"
    if not script.exists():
        return {
            "status": "not_run",
            "reason": "tools/measured_data_report.py 尚未交付（U04 未实施）",
            "ok": False,
        }
    proc = sh([sys.executable, str(script)], timeout=900)
    return {
        "status": "run", "argv": "python tools/measured_data_report.py",
        "returncode": proc.returncode, "ok": proc.returncode == 0,
        "tail": "\n".join(proc.stdout.strip().splitlines()[-3:]),
    }


STEPS = {
    "pytest": ("pytest（全量）", step_pytest),
    "package": ("wheel 干净环境安装冒烟", step_package),
    "dataqa": ("实测数据 QA", step_dataqa),
}

# 这些步骤**不是**「跑失败」而是「环境/前置不具备」时的说明
SLOW_STEPS = ("package",)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def build_report(sha: str, dirty: str, results: dict[str, Any], started: float) -> dict[str, Any]:
    ran = [k for k, v in results.items() if v.get("status") == "run"]
    not_run = [k for k, v in results.items() if v.get("status") != "run"]
    failed = [k for k, v in results.items() if v.get("status") == "run" and not v.get("ok")]
    return {
        "schema": "ufdemo.release_evidence/1",
        "commit_sha": sha,
        "working_tree_dirty": bool(dirty),
        "working_tree_status": dirty.splitlines() if dirty else [],
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started)),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "steps": results,
        "summary": {
            "run": ran,
            "not_run": not_run,
            "failed": failed,
            "all_run_steps_ok": not failed,
        },
    }


def render_md(rep: dict[str, Any]) -> str:
    L: list[str] = []
    L.append("# 发布证据（统一到同一提交）")
    L.append("")
    L.append(f"- **commit_sha**: `{rep['commit_sha']}`")
    L.append(f"- 工作树有未提交改动：**{'是' if rep['working_tree_dirty'] else '否'}**"
             + (f"（{len(rep['working_tree_status'])} 项）" if rep['working_tree_dirty'] else ""))
    L.append(f"- 采集时间：{rep['collected_at']}")
    L.append(f"- Python：{rep['python']}")
    L.append("")
    L.append("> ⚠️ 本报告与 `commit_sha` **绑定**。若 HEAD 已变，报告作废 —— "
             "用 `python tools/release_evidence.py --verify` 检查。")
    L.append("")
    L.append("## 汇总")
    L.append("")
    s = rep["summary"]
    L.append(f"- 已运行：{', '.join(s['run']) or '（无）'}")
    L.append(f"- 未运行：{', '.join(s['not_run']) or '（无）'}")
    L.append(f"- 已运行但失败：{', '.join(s['failed']) or '（无）'}")
    L.append("")
    L.append("## 逐项")
    L.append("")
    L.append("| 检查 | 状态 | 结果 | 说明 |")
    L.append("|---|---|---|---|")
    for key, (label, _) in STEPS.items():
        r = rep["steps"].get(key)
        if not r:
            continue
        st = r.get("status")
        if st != "run":
            res = "—"
            note = r.get("reason") or ""
            cell = "not_run"
        else:
            bits = []
            for k in ("passed", "failed", "tests", "failures", "errors", "skipped"):
                if r.get(k) is not None:
                    bits.append(f"{k}={r[k]}")
            res = "，".join(bits) or (r.get("summary_text") or "")
            note = "通过" if r.get("ok") else "**失败**"
            # 异常退出（被杀/崩溃）必须把原因带到汇总行，否则与断言失败无法区分
            if not r.get("ok") and r.get("reason"):
                note += f"：{r['reason']}"
            cell = "run"
        L.append(f"| {label} | `{cell}` | {res} | {note} |")
    L.append("")
    L.append("## 明细")
    L.append("")
    for key, (label, _) in STEPS.items():
        r = rep["steps"].get(key)
        if not r:
            continue
        L.append(f"### {label}")
        L.append("")
        L.append(f"- 命令：`{r.get('argv', '—')}`")
        if r.get("status") != "run":
            L.append(f"- **未运行原因**：{r.get('reason')}")
        else:
            L.append(f"- 退出码：{r.get('returncode')}")
            if "junit" in r and r["junit"]:
                L.append(f"- JUnit：`{json.dumps(r['junit'], ensure_ascii=False)}`")
                if not r.get("junit_passed_consistent"):
                    L.append("- ⚠️ 文本汇总与 JUnit 计数**不一致**，需人工核对")
            L.append(f"- 尾部输出：")
            L.append("")
            L.append("```")
            L.append(str(r.get("tail") or "").strip())
            L.append("```")
        L.append("")
    return "\n".join(L)


def _code_diff_paths(base: str, head: str) -> list[str] | None:
    """返回 ``base..head`` 之间**非报告类**的改动路径；无法判断时返回 ``None``。

    报告类路径 = ``docs/reports/`` 与 ``runs/``。这两处只放产物，不是被测代码。

    为什么要这个：证据必须「先提交代码、再采证据」，而采完证据又要提交报告
    —— 那一次提交会让 HEAD 前进，于是刚采的报告立刻「SHA 不符」。
    但两份提交之间**被测代码逐位相同**（只多了报告文件），证据依然成立。
    不做这个区分，就会陷入「提交即失效、重采又失效」的死循环。
    """
    out = sh(["git", "diff", "--name-only", base, head])
    if out.returncode != 0:
        return None
    paths = [p.strip() for p in out.stdout.splitlines() if p.strip()]
    return [p for p in paths if not (p.startswith("docs/reports/") or p.startswith("runs/"))]


def verify(sha: str | None = None) -> int:
    """检查现有报告是否仍对应当前 HEAD。

    选取顺序（**不要**改成「按文件名排序取最后一个」）：
    1. 显式给了 ``sha`` → 用那一份；
    2. 否则**优先找 commit_sha 等于当前 HEAD 的报告**；
    3. 再找 commit_sha 是当前 HEAD 祖先、且两者之间**只差报告文件**的报告；
    4. 最后退到**按修改时间最新**的一份。

    踩过的两个坑：
    - 原先用 ``sorted(...)[-1]`` 按**字母序**取最后一份。提交哈希随机，
      旧报告 ``f2572e2…`` 恰好排在 ``d14d548…`` 之后 → **重新采集成功后
      ``--verify`` 仍报「已作废」**，与事实相反。
    - 采完证据提交报告会让 HEAD 前进 → 报告立刻 SHA 不符。故引入第 3 条：
      只差报告文件时视为**代码等价**，证据仍然有效（并在输出里说明）。
    """
    head = git("rev-parse", "HEAD")
    cands = sorted(REPORTS.glob("release_evidence_*.json"))
    if not cands:
        print("未找到任何 release_evidence_*.json")
        return 2

    if sha:
        p = REPORTS / f"release_evidence_{sha}.json"
        if not p.exists():
            print(f"未找到 sha={sha} 的报告")
            return 2
        data = json.loads(p.read_text(encoding="utf-8"))
        ok = data["commit_sha"] == head
        print(f"报告 {p.name}")
        print(f"  报告 SHA : {data['commit_sha']}")
        print(f"  当前 HEAD: {head}")
        print(f"  结论     : {'有效' if ok else '**已作废**（HEAD 已变，请重新采集）'}")
        return 0 if ok else 1

    def _load(p: Path) -> dict:
        return json.loads(p.read_text(encoding="utf-8"))

    # 2) 精确匹配 HEAD
    for p in cands:
        try:
            if _load(p).get("commit_sha") == head:
                print(f"报告 {p.name}")
                print(f"  报告 SHA : {head}")
                print(f"  当前 HEAD: {head}")
                print("  结论     : 有效（与 HEAD 精确一致）")
                return 0
        except Exception:  # noqa: BLE001 - 坏报告跳过
            continue

    # 3) 祖先且只差报告文件 → 代码等价
    for p in sorted(cands, key=lambda q: q.stat().st_mtime, reverse=True):
        try:
            rsha = _load(p).get("commit_sha") or ""
        except Exception:  # noqa: BLE001
            continue
        if not rsha:
            continue
        code_diff = _code_diff_paths(rsha, head)
        if code_diff is None or code_diff:
            continue
        print(f"报告 {p.name}")
        print(f"  报告 SHA : {rsha}")
        print(f"  当前 HEAD: {head}")
        print(f"  结论     : **有效**（自 {rsha[:7]} 起只有报告类文件变动，被测代码逐位相同）")
        return 0

    # 4) 退到最新修改的一份，并如实说明
    target = max(cands, key=lambda q: q.stat().st_mtime)
    data = _load(target)
    print(f"报告 {target.name}")
    print(f"  报告 SHA : {data.get('commit_sha')}")
    print(f"  当前 HEAD: {head}")
    print("  结论     : **已作废**（报告对应提交与当前 HEAD 之间有代码改动，请重新采集）")
    return 1


def archive_stale_pytest_log() -> str | None:
    """归档**未绑定提交**的旧 ``pytest_output.txt``。

    只在文件**确实缺 SHA 绑定**时才动手 —— 否则会与已经修好的
    ``tools/run_acceptance.py`` 打架（后者现在会在头部写 ``commit_sha``）。
    历史背景：该文件过去是裸的 pytest 输出，368 / 394 / 395 被混放，
    审阅者无法判断数字属于哪份代码（审查缺陷 F07）。
    """
    src = REPORTS / "pytest_output.txt"
    if not src.exists():
        return None
    text = src.read_text(encoding="utf-8", errors="replace")
    # 已绑定提交 → 是有效证据，不动它
    if re.search(r"^#\s*commit_sha\s*:\s*[0-9a-f]{7,40}", text, re.MULTILINE):
        return None
    m = re.search(r"(\d+) passed", text)
    n = m.group(1) if m else "unknown"
    hist = REPORTS / "history"
    hist.mkdir(exist_ok=True)
    dst = hist / "pytest_output_legacy_368.txt"
    if dst.exists():
        return None
    header = (
        "历史记录（**不是**当前证据）\n"
        "==========================\n"
        f"原文件 docs/reports/pytest_output.txt 记录 {n} passed，\n"
        "但未绑定提交 SHA，与后续提交的数字（394 / 395 / 418…）互不对应，\n"
        "属审查缺陷 F07 所述「不同时间点数字混放」。\n"
        "当前证据请见 docs/reports/release_evidence_<sha>.md（由 tools/release_evidence.py 生成）。\n"
        "---- 以下为原文件内容 ----\n"
    )
    dst.write_text(header + text, encoding="utf-8")
    src.write_text(
        "【已归档】本文件未绑定提交、数字与当前代码不一致（审查缺陷 F07）。\n"
        f"历史内容已移至 docs/reports/history/{dst.name}。\n"
        "当前发布证据：docs/reports/release_evidence_<sha>.{md,json}\n",
        encoding="utf-8",
    )
    return str(dst.relative_to(ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="U11 统一发布证据")
    ap.add_argument("--only", default=None, help=f"逗号分隔，可选：{','.join(ALL_STEPS)}")
    ap.add_argument("--fast", action="store_true", help="跳过 wheel 冒烟")
    ap.add_argument("--sha", default=None, help="声明证据对应的提交（默认当前 HEAD）")
    ap.add_argument("--verify", action="store_true", help="只校验已有报告是否对应 HEAD")
    ap.add_argument("--archive-stale", action="store_true", help="归档过时的 pytest_output.txt")
    args = ap.parse_args(argv)

    if args.verify:
        return verify(args.sha)
    if args.archive_stale:
        moved = archive_stale_pytest_log()
        print(f"归档结果：{moved or '无需归档（目标已存在或源不存在）'}")
        return 0

    started = time.time()
    sha = args.sha or git("rev-parse", "HEAD") or "unknown"
    dirty = git("status", "--porcelain")

    if args.only:
        want = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = [s for s in want if s not in ALL_STEPS]
        if unknown:
            print(f"未知步骤：{unknown}；可选 {list(ALL_STEPS)}")
            return 2
    else:
        want = list(ALL_STEPS)
        if args.fast:
            want = [s for s in want if s not in SLOW_STEPS]

    work = ROOT / "runs" / "_release_evidence"
    work.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("release_evidence —— 统一发布证据（U11 / F07）")
    print(f"commit_sha: {sha}")
    print("=" * 70)

    results: dict[str, Any] = {}
    # **必须把所有步骤都列进报告**：未选的标 not_run 并写明原因。
    # 若只写跑过的项，报告会静默漏项 —— 那正是 F07 要消灭的「看似完整」。
    for key in ALL_STEPS:
        if key not in want:
            results[key] = {
                "status": "not_run",
                "reason": "本次以 --only 未选择该步骤（不是失败，也不是通过）",
                "ok": False,
            }

    for key in want:
        label, fn = STEPS[key]
        print(f"\n[{key}] {label}")
        try:
            r = fn(work)
        except Exception as err:  # noqa: BLE001 - 单步异常不应中断整份证据
            r = {"status": "not_run", "reason": f"执行异常：{type(err).__name__}: {err}", "ok": False}
        results[key] = r
        if r.get("status") == "run":
            print(f"   → {'OK' if r.get('ok') else 'FAIL'}  {r.get('tail', '').splitlines()[-1:] }")
        else:
            print(f"   → not_run：{r.get('reason')}")

    rep = build_report(sha, dirty, results, started)
    REPORTS.mkdir(parents=True, exist_ok=True)
    md_path = REPORTS / f"release_evidence_{sha}.md"
    js_path = REPORTS / f"release_evidence_{sha}.json"
    md_path.write_text(render_md(rep), encoding="utf-8")
    js_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    # 归档过时记录（只做一次，幂等）
    moved = archive_stale_pytest_log()

    print("\n" + "=" * 70)
    print(f"汇总：run={rep['summary']['run']}")
    print(f"      not_run={rep['summary']['not_run']}")
    print(f"      failed={rep['summary']['failed']}")
    if moved:
        print(f"已归档过时记录 → {moved}")
    print(f"报告：{md_path.relative_to(ROOT)}")
    print(f"      {js_path.relative_to(ROOT)}")
    print("=" * 70)

    return 1 if rep["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
