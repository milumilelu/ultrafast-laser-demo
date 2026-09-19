"""ufdemo —— 七种材料超快激光加工 Demo（逐脉冲事件引擎 + 2.5D 高度场）。

已实施批次：

* A–C（M0 最小 CLI 闭环）：``config`` / ``materials`` / ``beam`` / ``paths`` /
  ``response`` / ``surface`` / ``solver`` / ``metrics`` / ``io``
* D（T07 文献参考评估器）：``references``（YSZ/SiC 有效 N、阈值、平均率；
  **不求解网格，不进入逐事件主循环**）
* E（T09 界面 → K/L 演进）：``ui_service``（纯逻辑，不导入任何界面运行时）；
  唯一界面为 ``webui/demo.html``（2026-09-19 收敛，见 ADR-0023）
* F（T10 查表）：``tables``（曲线 schema、分段线性/保形 PCHIP 插值、越界处理、
  语义路由；**不接入逐事件主循环**）

尚未实现（按批次推进）：``accelerators``（I）、``geometry``（J）、
合成相结构（G）、逐事件核查表导入（H）。科学核心不导入 Streamlit 或 Plotly，
也不通过全局 UI 状态取得输入。
"""

from __future__ import annotations

import os
import sysconfig
from pathlib import Path

__all__ = [
    "__version__",
    "BUILD_BATCH",
    "SCHEMA_VERSION",
    "project_root",
    "resource_root",
    "default_material_dir",
    "default_runs_dir",
    "default_curves_dir",
    "default_webui_dir",
    "default_examples_dir",
]

# 版本轨迹：M0 放行 0.1.0-m0 → D 0.2.0-d → E 0.3.0-e1 → F 0.4.0-f1 →
# G 0.5.0-g1 → H 0.6.0-h1 → I 0.7.0-i1 → J 0.8.0-j1 → K 0.9.0-k1 → 0.9.1。
#
# ⚠️ 版本号必须是**合法 PEP 440**。`0.9.0-k1` 那种「连字符 + 批次」写法不是
# PEP 440 预发行标记，`pip install` / `python -m build` 会直接失败
# （实测：`configuration error: project.version must be pep440`）。
# 「两个文件字符串一致」的旧测试**结构上发现不了**这个问题，故必须补
# `packaging.version.Version()` 可解析性断言 —— 见 tests/test_version_consistency.py。
#
# 批次改由独立元数据承载：见 BUILD_BATCH。
__version__ = "0.9.1"

# 交付批次（当前为 L 批：发布与接口）。与 pyproject.toml 的
# ``[tool.ufdemo] build_batch`` 必须一致，由 test_version_consistency.py 守住。
BUILD_BATCH = "L"

from .config import SCHEMA_VERSION  # noqa: E402  (放在 __version__ 之后以免循环)


def _looks_like_source_tree(root: Path) -> bool:
    """``root`` 是否像本仓库的源码根（含 ``data/`` 与 ``webui/``）。"""
    return (root / "data").is_dir() and (root / "webui").is_dir()


def project_root() -> Path:
    """源码树根目录（``ultrafast-demo/``）。

    ⚠️ **仅在源码 / editable 安装下有意义**。wheel 安装后包位于
    ``site-packages/ufdemo/``，其 ``parents[2]`` 指向发行版或 venv 根，
    **不含** ``data/``、``webui/``、``examples/``。
    读随包资源请用 :func:`resource_root`（它会自动回退），
    写产物请用 :func:`default_runs_dir`。
    """
    return Path(__file__).resolve().parents[2]


def resource_root() -> Path:
    """随包只读资源的根目录（材料卡 / 曲线卡 / 前端静态文件 / 示例配置）。

    解析顺序（第一个命中即用）：

    1. ``UFDEMO_RESOURCE_DIR`` 环境变量 —— 部署方显式指定；
    2. **包内资源目录** ``<package>/_resources`` —— 若将来把资源移进包内，此处即命中；
    3. **安装期数据目录** ``<sys.prefix>/share/ufdemo`` —— wheel 安装后的正规位置，
       由 ``pyproject.toml`` 的 ``[tool.setuptools.data-files]`` 铺开；
    4. **源码树根** ``project_root()`` —— 开发 / editable 安装；
    5. 退而求其次返回候选列表最后一项，让调用方拿到可读的错误路径
       （而不是 ``None`` 引发更难查的 ``TypeError``）。

    为什么需要这一层：原实现直接 ``project_root() / "data"``，在 wheel
    安装后指向**不存在**的目录 —— 材料/曲线清单为空、Web 界面启动即退出
    （原因：wheel 里 ``ufdemo`` 位于 ``site-packages``，其 ``parents[2]``
    是发行版或 venv 根）。源码下却完全正常，所以本地开发**察觉不到**。
    """
    override = os.environ.get("UFDEMO_RESOURCE_DIR")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    # 包内资源（PEP 302 下 zip 导入时也可能不是真目录，故用 is_dir() 兜底）
    candidates.append(Path(__file__).resolve().parent / "_resources")
    # 安装期数据目录：wheel 用 data-files 铺到 <prefix>/share/ufdemo
    try:
        candidates.append(Path(sysconfig.get_path("data")) / "share" / "ufdemo")
    except Exception:  # noqa: BLE001 - 极端环境下 sysconfig 可能取不到
        pass
    candidates.append(project_root())
    for cand in candidates:
        if _looks_like_source_tree(cand):
            return cand
    return candidates[-1]


def _resource_dir(parts: tuple[str, ...]) -> Path:
    base = resource_root()
    for sub in (base / "_resources", base):
        cand = sub.joinpath(*parts)
        if cand.is_dir():
            return cand
    return base.joinpath(*parts)


def default_material_dir() -> Path:
    """材料卡目录（随包只读资源）。"""
    return _resource_dir(("data", "materials"))


def default_curves_dir() -> Path:
    """响应曲线目录（批次 F 的查表卡：``*.curve.json`` + ``*.points.csv``）。

    可用 ``UFDEMO_CURVES_DIR`` 覆写（优先于随包资源）。
    """
    override = os.environ.get("UFDEMO_CURVES_DIR")
    if override:
        return Path(override)
    return _resource_dir(("data", "curves"))


def default_measured_dir() -> Path:
    """实测数据集目录（U04 导入的审计数据包 + U05 权限注册表）。

    可用 ``UFDEMO_MEASURED_DIR`` 覆写（优先于随包资源）。
    """
    override = os.environ.get("UFDEMO_MEASURED_DIR")
    if override:
        return Path(override)
    return _resource_dir(("data", "measured"))


def default_webui_dir() -> Path:
    """前端静态文件目录（本地 Web 界面用）。"""
    override = os.environ.get("UFDEMO_WEBUI_DIR")
    if override:
        return Path(override)
    return _resource_dir(("webui",))


def default_examples_dir() -> Path:
    """示例配置模板目录（``examples/*.json``）。"""
    override = os.environ.get("UFDEMO_EXAMPLES_DIR")
    if override:
        return Path(override)
    return _resource_dir(("examples",))


def default_runs_dir() -> Path:
    """默认运行输出根目录 —— **可写目录，绝不是随包资源**。

    解析顺序：``UFDEMO_RUNS_DIR`` → 源码树下 ``runs/``（开发时便于查看）
    → 用户目录 ``~/.ufdemo/runs``（wheel 安装后 site-packages 不可写）。
    """
    override = os.environ.get("UFDEMO_RUNS_DIR")
    if override:
        return Path(override)
    root = project_root()
    if (root / "pyproject.toml").is_file():
        return root / "runs"
    return Path.home() / ".ufdemo" / "runs"
