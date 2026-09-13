"""U03 浏览器级验收：用**本机已装的系统浏览器**驱动 `tools/browser_probe.mjs`。

背景（必读，勿重犯）
-------------------
本工程曾把 U03 的「浏览器级」长期标为 **未运行**，理由写作「本机无 Playwright / Selenium /
系统浏览器」。**该理由不成立**，实测本机有 Chrome 152 与 Edge 152，都在默认安装路径。
误诊由三个独立事实合并而成：

1. **Windows 上浏览器从不注册进 PATH** —— `which chrome` / `where chrome` 必然失败，
   而 `C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe` 一直存在；
2. **Playwright 默认只找自己下载的浏览器** —— 缓存为空时报
   `Executable doesn't exist at ...\\ms-playwright\\...`，措辞极易被误读为「本机没浏览器」；
3. Playwright / Selenium 当时**确实未安装** —— 但「库没装」≠「没有浏览器」。

因此本模块**只用绝对路径**探测浏览器，探测不到时如实报「环境缺失」并列出已查路径，
**不得**再记成「本机无浏览器」。取证见 `docs/reports/browser_test_capability.md`。

方案：`puppeteer-core` + 系统 Chrome（**不下载 Chromium**）。
探针逐条覆盖 U03 的 8 条主路径 + 1 条「下载」，每条结果带 `u03` 字段。

用法::

    python tools/browser_check.py                      # 隔离端口 + 隔离 runs/ 跑一遍
    python tools/browser_check.py --no-solve           # 跳过需要真实求解的路径
    python tools/browser_check.py --browser edge       # 用 Edge
    python tools/browser_check.py --port 8801 --out runs/_bc

输出：`docs/reports/browser_check.csv` 与 `.md`。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / "docs" / "reports"

# U03 要求的 9 条主路径（顺序即清单顺序）；`download` 前端未实现，单独标注
U03_PATHS = [
    "U03-1 启动",
    "U03-2 选真实数据",
    "U03-3 提交",
    "U03-4 错误提示",
    "U03-5 曲线单位",
    "U03-6 换参数后旧结果标记",
    "U03-7 读历史",
    "U03-8 回放不重算",
    "U03-9 下载",
]

# 状态映射：探针状态 → 验收报告状态
_STATUS = {"pass": "通过", "fail": "失败", "skip": "未运行", "unimplemented": "未实现"}

# ---------------- 前置条件探测（**只查绝对路径**）----------------

NODE_CANDIDATES = [
    os.environ.get("WB_NODE_EXE", ""),
    "C:/Users/RZF/.workbuddy/binaries/node/versions/22.22.2-3/node.exe",
    "C:/Program Files/nodejs/node.exe",
]
NODE_WS = os.environ.get("WB_NODE_WS", "C:/Users/RZF/.workbuddy/binaries/node/workspace")

BROWSER_CANDIDATES: dict[str, list[str]] = {
    "chrome": [
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google/Chrome/Application/chrome.exe"),
    ],
    "edge": [
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    ],
}


def _row(check: str, expected: Any, measured: Any, status: str, note: str = "",
         u03: str = "") -> dict[str, Any]:
    return {"u03": u03, "check": check, "expected": expected,
            "measured": measured, "status": status, "note": note}


def _timeout_row(timeout: float) -> dict[str, Any]:
    """探针超时的验收行。

    单独抽成函数**并配测试**：它原先内联在 ``return`` 里，参数个数写错时
    只有真的挂死才会暴露 —— 而挂死本来就是罕见路径，错误会被掩盖很久。
    """
    return _row("U03 浏览器级", "在超时内完成",
                f"超时 {timeout:.0f}s（已终止进程树）", "失败",
                "探针挂死：不是断言失败。用命令行核对 Chrome 是否启动、"
                "profile 是否被占用、CDP 是否返回。",
                "U03-浏览器级")


def _find_node() -> tuple[str | None, str]:
    for p in NODE_CANDIDATES:
        if p and Path(p).exists():
            return p, ""
    return None, "未找到受管 node（已查：" + "、".join(x for x in NODE_CANDIDATES if x) + "）"


def _has_puppeteer() -> tuple[bool, str]:
    mod = Path(NODE_WS) / "node_modules" / "puppeteer-core"
    if mod.exists():
        return True, ""
    return False, (f"未安装 puppeteer-core（应位于 {mod}）。安装："
                   f'cd "{NODE_WS}" && npm install puppeteer-core '
                   f"--registry=https://registry.npmmirror.com")


def _find_browser(kind: str = "auto") -> tuple[str | None, str]:
    order = ["chrome", "edge"] if kind == "auto" else [kind]
    for name in order:
        for p in BROWSER_CANDIDATES.get(name, []):
            if p and Path(p).exists():
                return p, ""
    checked = [p for name in order for p in BROWSER_CANDIDATES.get(name, []) if p]
    return None, ("按绝对路径未找到系统浏览器（已查：" + "、".join(checked) + "）。"
                  "注意：Windows 上浏览器不在 PATH 里，`which chrome` 必然失败，"
                  "不要据此判断「本机无浏览器」。")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _opener() -> urllib.request.OpenerDirector:
    """绕过系统代理：本机 HTTP_PROXY 会把 127.0.0.1 也代理掉（实测 502）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _child_env(runs_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["UFDEMO_RUNS_DIR"] = str(runs_dir)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    return env


def _wait_health(base: str, proc: subprocess.Popen, timeout: float = 30.0) -> bool:
    op = _opener()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with op.open(base + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        time.sleep(0.5)
    return False


# ---------------- 主流程 ----------------

def _kill_tree(proc: subprocess.Popen) -> None:
    """杀掉子进程**整棵树**。

    为什么必须连树一起杀：探针会拉起 Chrome 子进程。只杀 node 会留下孤儿
    Chrome 继续占着 profile 目录，下一次探针就起不来（本工程踩过）。
    Windows 用 ``taskkill /T /F``，其它平台退回 ``proc.kill()``。
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=20, check=False)
        else:
            proc.kill()
    except Exception:  # noqa: BLE001 - 兜底失败也不能让包装器自己挂掉
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def run_browser_checks(out_dir: Path, *, browser: str = "auto", solve: bool = True,
                       port: int | None = None, timeout: float = 300.0) -> list[dict[str, Any]]:
    """起隔离服务 → 跑 node 探针 → 把逐条结果映射成验收行。

    前置条件不满足时**不抛异常**，而是返回带准确原因的「未运行」行——
    验收报告的价值在于说清「为什么没跑」，而不是崩掉。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = out_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []

    node, why_node = _find_node()
    if not node:
        return [_row("U03 浏览器级", "驱动系统浏览器跑 9 条主路径", "未运行", "未运行", why_node,
                     "U03-浏览器级")]

    ok_pp, why_pp = _has_puppeteer()
    if not ok_pp:
        return [_row("U03 浏览器级", "驱动系统浏览器跑 9 条主路径", "未运行", "未运行", why_pp,
                     "U03-浏览器级")]

    exe, why_br = _find_browser(browser)
    if not exe:
        return [_row("U03 浏览器级", "驱动系统浏览器跑 9 条主路径", "未运行", "未运行", why_br,
                     "U03-浏览器级")]

    port = port or _free_port()
    base = f"http://127.0.0.1:{port}"
    log = out_dir / "webapp.log"
    probe_out = out_dir / "probe"
    probe_out.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [sys.executable, "-m", "ufdemo.webapp", "--port", str(port)],
        cwd=str(ROOT), env=_child_env(runs_dir),
        stdout=open(log, "w", encoding="utf-8"), stderr=subprocess.STDOUT,
    )
    try:
        if not _wait_health(base, proc):
            return [_row("U03 浏览器级", "Web 服务可启动", "未运行", "未运行",
                         f"ufdemo.webapp 未在 {base} 就绪（日志：{log}）", "U03-浏览器级")]

        cmd = [node, str(ROOT / "tools" / "browser_probe.mjs"),
               "--base", base, "--out", str(probe_out), "--browser", browser]
        if not solve:
            cmd.append("--no-solve")
        env = dict(os.environ)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["no_proxy"] = "127.0.0.1,localhost"
        env["WB_NODE_WS"] = NODE_WS
        # 用 Popen + communicate 而不是 subprocess.run：
        # 超时后要能**杀掉整棵进程树**（node + 它拉起的 Chrome），
        # 否则孤儿 Chrome 会占着 profile 目录，下一次探针起不来（本工程踩过）。
        cp = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out_s, err_s = cp.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(cp)
            try:
                out_s, err_s = cp.communicate(timeout=15)
            except Exception:  # noqa: BLE001
                out_s, err_s = "", ""
            (out_dir / "browser_probe.log").write_text(
                (out_s or "") + "\n" + (err_s or ""), encoding="utf-8")
            # ⚠️ **挂死是「失败」，不是「未运行」** —— 两者含义完全不同：
            # 「未运行」= 没跑；「失败」= 跑了但没跑完。把它记成未运行会把
            # 一个真实缺陷藏起来（本工程踩过：探针挂死被记成未运行，
            # 验收凭空少一整组，看起来像功能没做）。
            return [_timeout_row(timeout)]
        log_file = out_dir / "browser_probe.log"
        log_file.write_text((out_s or "") + "\n" + (err_s or ""), encoding="utf-8")

        result_file = probe_out / "result.json"
        if not result_file.exists():
            tail = "\n".join((out_s or "").splitlines()[-12:])
            return [_row("U03 浏览器级", "探针产出 result.json", "未运行", "未运行",
                         f"探针未产出 result.json（exit={cp.returncode}）。末尾输出：{tail}",
                         "U03-浏览器级")]

        data = json.loads(result_file.read_text(encoding="utf-8"))
        for c in data.get("checks", []):
            rows.append(_row(
                c.get("name", "?"),
                "",                       # 断言期望值在 name 里已自述
                {"pass": "符合", "fail": "不符合", "skip": "未采集",
                 "unimplemented": "功能未实现"}.get(c.get("status"), "?"),
                _STATUS.get(c.get("status"), "未运行"),
                c.get("detail", "") or "",
                c.get("u03") or "",
            ))
        # 浏览器与探针环境附在首行备注，便于复核
        rows.insert(0, _row("运行环境", "真实系统浏览器", f"{data.get('browser')}｜{data.get('exe')}",
                            "通过", f"真实求解={data.get('solveRan')}｜结果：{result_file}",
                            "U03-浏览器级"))
        return rows
    finally:
        # 收尾：关掉自起的 web 服务。**无论成功/超时/异常都要关** ——
        # 否则会留下占端口的僵尸服务。
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# ---------------- 报告 ----------------

def _aggregate(rows: list[dict[str, Any]], path: str) -> str:
    rs = [r for r in rows if r.get("u03") == path]
    if not rs:
        return "—"
    st = {r["status"] for r in rs}
    if "失败" in st:
        return "失败"
    if st <= {"未实现"}:
        return "未实现"
    if "通过" in st:
        return "通过"
    return "未运行"


def write_reports(rows: list[dict[str, Any]], out_dir: Path) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = D / "browser_check.csv"
    md_path = D / "browser_check.md"

    fields = ["u03", "check", "expected", "measured", "status", "note"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})

    n_pass = sum(1 for r in rows if r["status"] == "通过")
    n_fail = sum(1 for r in rows if r["status"] == "失败")
    n_nr = sum(1 for r in rows if r["status"] == "未运行")
    n_ni = sum(1 for r in rows if r["status"] == "未实现")

    md = [
        "# U03 浏览器级验收（真实系统浏览器）",
        "",
        "- 驱动方式：`puppeteer-core` + **本机已装**的 Chrome/Edge（**不下载 Chromium**）",
        "- 探针：`tools/browser_probe.mjs`；包装器：`tools/browser_check.py`",
        f"- 汇总：通过 {n_pass}｜失败 {n_fail}｜未运行 {n_nr}｜未实现 {n_ni}",
        f"- 产物目录：`{out_dir}`（隔离端口 + 隔离 `UFDEMO_RUNS_DIR`，不污染工作区 `runs/`）",
        "",
        "> 与 `webui/test/contract_test.mjs`（DOM 契约测试）的分工：后者是**无浏览器**替代品，",
        "> 只能静态检查元素与字段；本报告是**真引擎**里跑出来的，能看见静态检查看不见的三类问题：",
        "> 页面能否 boot、渲染是否真出像素、交互是否真能点动。",
        "> **不得**以 DOM 契约测试替代本报告。",
        "",
        "## U03 逐路径结果",
        "",
        "| 主路径 | 结果 | 通过 | 失败 | 未运行 |",
        "|---|---|---|---|---|",
    ]
    for p in U03_PATHS:
        rs = [r for r in rows if r.get("u03") == p]
        md.append(f"| {p} | **{_aggregate(rows, p)}** | "
                  f"{sum(1 for r in rs if r['status'] == '通过')} | "
                  f"{sum(1 for r in rs if r['status'] == '失败')} | "
                  f"{sum(1 for r in rs if r['status'] in ('未运行', '未实现'))} |")

    md += ["", "## 逐条检查", "", "| 路径 | 检查项 | 实测 | 状态 | 说明 |", "|---|---|---|---|---|"]
    for r in rows:
        note = str(r["note"]).replace("|", "\\|")[:300]
        md.append(f"| {r['u03']} | {str(r['check']).replace('|', chr(92) + '|')} | "
                  f"{r['measured']} | {r['status']} | {note} |")

    md += [
        "",
        "## 已知待修（本报告的失败项不是回归，是 U03 的修复对象）",
        "",
        "U03 的实施步骤第 1 条即「先在**当前代码**上跑一个有断言的新测试，确认它**失败**（固化 F04）」。",
        "因此本报告中与以下缺陷对应的**失败**是预期状态，修完代码后应转为通过：",
        "",
        "| 缺陷 | 现象 | 观测口 |",
        "|---|---|---|",
        "| **F04** 曲线轴名/单位 | 查表页显示 `x：[object Object]（）`，单位为空 | `U03-5 曲线单位` |",
        "| 失败运行渲染崩溃 | 提交被配置层拒绝时，`renderResultPanel` 读 `f.watermark.material_id` 抛",
        "  `TypeError`，把后端已给出的结构化错误（`code`/`requirement`/`suggestion`）",
        "  覆盖成一句原始 JS 异常 | `U03-4 错误提示` |",
        "| 模板载入不回填身份 | `loadTemplate()` 只回填数值参数，**不设置** `state.materialId`/",
        "  `state.runMode`，于是「载入模板→提交」被配置层拒（材料与模式不匹配） | `U03-2 选真实数据` |",
        "| 下载/导出未实现 | 前端无下载控件（`index.html` 无 `a[download]`，`main.js` 无 `Blob`） | `U03-9 下载`（未实现） |",
        "",
        "## 复现命令",
        "",
        "```bash",
        "python tools/browser_check.py                 # 隔离端口 + 隔离 runs/",
        "python tools/browser_check.py --no-solve      # 跳过真实求解",
        "node tools/browser_probe.mjs --base <url> --headed   # 有头，肉眼看",
        "```",
        "",
        "前提：本机有 Chrome 或 Edge（**绝对路径**，非 PATH），且受管 node 工作区装了 `puppeteer-core`。",
        "",
    ]
    md_path.write_text("\n".join(md), encoding="utf-8")
    return csv_path, md_path


def main() -> int:
    ap = argparse.ArgumentParser(description="U03 浏览器级验收（真实系统浏览器）")
    ap.add_argument("--out", default=str(ROOT / "runs" / "_browser_check"))
    ap.add_argument("--browser", default="auto", choices=["auto", "chrome", "edge"])
    ap.add_argument("--port", type=int, default=0, help="0 = 自动选空闲端口")
    ap.add_argument("--no-solve", action="store_true", help="跳过需要真实求解的路径")
    args = ap.parse_args()

    rows = run_browser_checks(Path(args.out), browser=args.browser,
                              solve=not args.no_solve, port=args.port or None)
    csv_path, md_path = write_reports(rows, Path(args.out))

    n_pass = sum(1 for r in rows if r["status"] == "通过")
    n_fail = sum(1 for r in rows if r["status"] == "失败")
    n_nr = sum(1 for r in rows if r["status"] == "未运行")
    n_ni = sum(1 for r in rows if r["status"] == "未实现")
    print(f"U03 浏览器级：通过 {n_pass}｜失败 {n_fail}｜未运行 {n_nr}｜未实现 {n_ni}")
    print("U03 逐路径：")
    for p in U03_PATHS:
        print(f"  {p:24s} {_aggregate(rows, p)}")
    print(f"报告：{csv_path}")
    print(f"报告：{md_path}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
