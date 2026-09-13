"""U02 验收：wheel 在**干净环境、非仓库目录**下可安装、可定位资源、可启动服务。

修复的缺陷（审查 F03）：``project_root()`` 用 ``Path(__file__).parents[2]``
定位 ``data/``、``webui/``、``examples/``。源码与 editable 安装下碰巧正确，
**wheel 安装后** ``ufdemo`` 位于 ``site-packages``，``parents[2]`` 指向发行版或
venv 根 —— 材料 / 曲线清单为空，Web 界面因「前端目录不存在」直接退出。
源码下完全正常，所以本地开发察觉不到。

本脚本能证明的事（全部实测，不打折扣）：

1. wheel **构建成功**（``0.9.0-k1`` 那种非法版本号在这一步就失败）；
2. wheel **内含**随包资源（``share/ufdemo/...``），不是一个只有 .py 的空包；
3. 在**全新 venv** 里安装该 wheel；
4. 在**非仓库的临时目录**里运行探针：版本可读、资源目录存在、
   材料卡与曲线卡**真能加载**、Web 服务 ``/api/health`` 与 ``/api/materials`` 正常。

用法::

    python tools/package_smoke.py                  # 完整跑（需联网拉 setuptools/numpy）
    python tools/package_smoke.py --wheel PATH     # 复用已构建的 wheel
    python tools/package_smoke.py --keep           # 保留临时目录以便排查

退出码：0 = 全部通过；1 = 有失败；2 = 环境不具备（如无法联网），**不算通过**。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

# wheel 里必须存在的随包资源（相对 wheel 根，'.data/data/' 前缀由 wheel 规范插入）
REQUIRED_WHEEL_ENTRIES: tuple[str, ...] = (
    "share/ufdemo/data/materials",
    "share/ufdemo/data/curves",
    "share/ufdemo/webui/index.html",
    "share/ufdemo/webui/js/api.js",
    "share/ufdemo/examples",
)


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def ok(self, what: str, detail: str = "") -> None:
        self.rows.append(("PASS", what, detail))
        print(f"  PASS  {what}" + (f"  —— {detail}" if detail else ""))

    def fail(self, what: str, detail: str = "") -> None:
        self.rows.append(("FAIL", what, detail))
        print(f"  FAIL  {what}" + (f"  —— {detail}" if detail else ""))

    def skip(self, what: str, why: str) -> None:
        self.rows.append(("SKIP", what, why))
        print(f"  SKIP  {what}  —— {why}")

    @property
    def failed(self) -> int:
        return sum(1 for s, _, _ in self.rows if s == "FAIL")

    @property
    def passed(self) -> int:
        return sum(1 for s, _, _ in self.rows if s == "PASS")

    def summary(self) -> str:
        n_skip = sum(1 for s, _, _ in self.rows if s == "SKIP")
        return f"{self.passed} 通过｜{self.failed} 失败｜{n_skip} 跳过"


def run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, env=merged
    )


def build_wheel(work: Path, report: Report) -> Path | None:
    dist = work / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    print("\n[1] 构建 wheel")
    proc = run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(dist),
         "-i", MIRROR],
        cwd=ROOT,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-12:]
        report.fail("wheel 构建成功", "\n      ".join(tail))
        return None
    wheels = sorted(dist.glob("ufdemo-*.whl"))
    if not wheels:
        report.fail("wheel 构建成功", f"{dist} 下没有 ufdemo-*.whl")
        return None
    report.ok("wheel 构建成功", wheels[-1].name)
    return wheels[-1]


def inspect_wheel(whl: Path, report: Report) -> None:
    print("\n[2] 检查 wheel 是否内含随包资源")
    with zipfile.ZipFile(whl) as zf:
        names = zf.namelist()
    for entry in REQUIRED_WHEEL_ENTRIES:
        hits = [n for n in names if n.startswith(".data/data/" + entry)
                or n.startswith(entry) or ("/" + entry) in n]
        if hits:
            report.ok(f"wheel 含 {entry}", f"{len(hits)} 个条目")
        else:
            report.fail(f"wheel 含 {entry}", "wheel 内未找到 —— 运行时会定位不到")


def make_venv(work: Path, report: Report) -> Path | None:
    print("\n[3] 建全新 venv")
    venv = work / "venv"
    proc = run([sys.executable, "-m", "venv", str(venv)])
    if proc.returncode != 0:
        report.fail("创建 venv", (proc.stderr or "")[-300:])
        return None
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        report.fail("创建 venv", f"未找到解释器 {py}")
        return None
    report.ok("创建 venv", str(venv))
    return py


def install_wheel(py: Path, whl: Path, report: Report) -> bool:
    print("\n[4] 在干净 venv 中安装 wheel（含依赖）")
    proc = run([str(py), "-m", "pip", "install", str(whl), "-i", MIRROR],
               cwd=str(whl.parent))
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-12:]
        report.fail("安装 wheel（含依赖）", "\n      ".join(tail))
        return False
    report.ok("安装 wheel（含依赖）")
    return True


# 在**非仓库目录**运行的探针：不设 PYTHONPATH，不用源码路径
PROBE = textwrap.dedent(
    """
    import json, sys, threading, urllib.request, urllib.error
    from pathlib import Path

    out = {"python": sys.executable, "cwd": str(Path.cwd()), "checks": {}}
    def rec(name, value): out["checks"][name] = value

    import ufdemo
    from ufdemo import (BUILD_BATCH, __version__, default_curves_dir,
                        default_examples_dir, default_material_dir,
                        default_runs_dir, default_webui_dir, resource_root)
    from ufdemo.materials import load_material_catalog
    from ufdemo import tables

    rec("version", __version__)
    rec("build_batch", BUILD_BATCH)
    rec("resource_root", str(resource_root()))
    rec("project_root_is_resource", str(resource_root()) == str(ufdemo.project_root()))

    # 关键：包路径必须**不在**仓库里（证明跑的是已安装的 wheel）
    rec("package_file", ufdemo.__file__)

    rec("material_dir", str(default_material_dir()))
    rec("material_dir_exists", default_material_dir().is_dir())
    rec("curve_dir_exists", default_curves_dir().is_dir())
    rec("webui_dir_exists", default_webui_dir().is_dir())
    rec("examples_dir_exists", default_examples_dir().is_dir())
    rec("runs_dir", str(default_runs_dir()))

    cat = load_material_catalog(default_material_dir())
    rec("material_count", len(cat))
    cards = tables.iter_curve_cards(default_curves_dir())
    rec("curve_card_count", len(cards))

    # 真起一次 HTTP 服务（随机端口），验证前端静态目录与材料清单
    from ufdemo import webapp
    ctx = webapp.build_context(runs_dir=Path(out["cwd"]) / "_runs")
    srv = webapp.make_server(ctx, "127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    st, body = get("/api/health")
    rec("health_status", st)
    rec("health_body", body[:200])
    st2, body2 = get("/api/materials")
    rec("materials_status", st2)
    try:
        rec("materials_catalog_len", len(json.loads(body2).get("catalog", [])))
    except Exception:
        rec("materials_catalog_len", None)
    st3, _ = get("/")
    rec("index_status", st3)
    srv.shutdown()
    print("@@" + json.dumps(out, ensure_ascii=False) + "@@")
    """
)


def run_probe(py: Path, work: Path, report: Report) -> dict | None:
    print("\n[5] 在非仓库目录运行探针（导入 + 资源 + 真实 HTTP）")
    neutral = work / "neutral_cwd"
    neutral.mkdir(parents=True, exist_ok=True)
    probe = work / "_probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    proc = run([str(py), str(probe)], cwd=neutral, env={"PYTHONPATH": ""})
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-20:]
        report.fail("探针运行结束", "\n      ".join(tail))
        return None
    marker = proc.stdout.split("@@")
    if len(marker) < 3:
        report.fail("探针产出 JSON", proc.stdout[-300:])
        return None
    data = json.loads(marker[-2])

    checks = data["checks"]
    # 最关键的一条：包不能来自源码树
    pkg = Path(checks["package_file"]).resolve()
    if ROOT.resolve() in pkg.parents:
        report.fail("导入的是已安装的 wheel（不是源码树）", f"包路径仍在仓库内：{pkg}")
    else:
        report.ok("导入的是已安装的 wheel（不是源码树）", str(pkg))

    cwd = Path(data["cwd"]).resolve()
    if cwd == ROOT.resolve():
        report.fail("工作目录非仓库", str(cwd))
    else:
        report.ok("工作目录非仓库", str(cwd))

    from packaging.version import Version  # 本机 venv 有 packaging

    try:
        Version(checks["version"])
        report.ok("安装后版本号合法（PEP 440）", checks["version"])
    except Exception as exc:  # noqa: BLE001
        report.fail("安装后版本号合法（PEP 440）", f"{checks['version']!r}: {exc}")

    for key, label in (
        ("material_dir_exists", "材料目录存在"),
        ("curve_dir_exists", "曲线目录存在"),
        ("webui_dir_exists", "前端目录存在"),
        ("examples_dir_exists", "示例目录存在"),
    ):
        (report.ok if checks[key] else report.fail)(label, checks.get(key.replace("_exists", "_dir"), ""))

    n_mat, n_cur = checks["material_count"], checks["curve_card_count"]
    (report.ok if n_mat >= 8 else report.fail)("材料卡可加载", f"{n_mat} 张")
    (report.ok if n_cur >= 3 else report.fail)("曲线卡可加载", f"{n_cur} 张")

    (report.ok if checks["health_status"] == 200 else report.fail)(
        "/api/health == 200", str(checks["health_status"]))
    (report.ok if checks["materials_status"] == 200 else report.fail)(
        "/api/materials == 200", str(checks["materials_status"]))
    (report.ok if (checks["materials_catalog_len"] or 0) >= 8 else report.fail)(
        "/api/materials 清单非空", str(checks["materials_catalog_len"]))
    (report.ok if checks["index_status"] == 200 else report.fail)(
        "前端 index.html 可服务", str(checks["index_status"]))
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="U02 验收：wheel 干净环境安装冒烟")
    ap.add_argument("--wheel", default=None, help="复用已构建的 wheel（跳过构建）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args(argv)

    report = Report()
    print("=" * 68)
    print("package_smoke —— wheel 干净环境安装冒烟（U02 / F02+F03）")
    print(f"仓库：{ROOT}")
    print("=" * 68)

    work = Path(tempfile.mkdtemp(prefix="ufdemo_smoke_"))
    try:
        whl = Path(args.wheel) if args.wheel else build_wheel(work, report)
        if whl is None:
            return 1
        inspect_wheel(whl, report)
        py = make_venv(work, report)
        if py is None:
            return 1
        if not install_wheel(py, whl, report):
            print("\n提示：安装失败多为网络问题。可加 --wheel 复用已有 wheel，"
                  "或确认镜像可达。")
            return 2
        run_probe(py, work, report)
    finally:
        print("\n" + "=" * 68)
        print(f"结果：{report.summary()}")
        print("=" * 68)
        if args.keep:
            print(f"临时目录保留：{work}")
        else:
            shutil.rmtree(work, ignore_errors=True)

    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
