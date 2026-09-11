"""ufdemo —— 七种材料超快激光加工 Demo（逐脉冲事件引擎 + 2.5D 高度场）。

已实施批次：

* A–C（M0 最小 CLI 闭环）：``config`` / ``materials`` / ``beam`` / ``paths`` /
  ``response`` / ``surface`` / ``solver`` / ``metrics`` / ``io``
* D（T07 文献参考评估器）：``references``（YSZ/SiC 有效 N、阈值、平均率；
  **不求解网格，不进入逐事件主循环**）
* E（T09 界面）：``ui_service``（纯逻辑，不导入 Streamlit）+ ``app.py``
  （Streamlit 渲染层）
* F（T10 查表）：``tables``（曲线 schema、分段线性/保形 PCHIP 插值、越界处理、
  语义路由；**不接入逐事件主循环**）

尚未实现（按批次推进）：``accelerators``（I）、``geometry``（J）、
合成相结构（G）、逐事件核查表导入（H）。科学核心不导入 Streamlit 或 Plotly，
也不通过全局 UI 状态取得输入。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "__version__",
    "SCHEMA_VERSION",
    "project_root",
    "default_material_dir",
    "default_runs_dir",
    "default_curves_dir",
]

# 版本轨迹：M0 放行 0.1.0-m0 → D 0.2.0-d → E 0.3.0-e1 → F 0.4.0-f1 →
# G 0.5.0-g1 → H 0.6.0-h1 → I 0.7.0-i1 → J 0.8.0-j1 → K 0.9.0-k1。
# 必须与 pyproject.toml 的 version 一致；由 tests/test_version_consistency.py 守住。
__version__ = "0.9.0-k1"

from .config import SCHEMA_VERSION  # noqa: E402  (放在 __version__ 之后以免循环)


def project_root() -> Path:
    """工程根目录（``ultrafast-demo/``）。"""
    return Path(__file__).resolve().parents[2]


def default_material_dir() -> Path:
    return project_root() / "data" / "materials"


def default_runs_dir() -> Path:
    """默认运行输出根目录。

    可用环境变量 ``UFDEMO_RUNS_DIR`` 覆写——界面冒烟/验收探针据此把「提交计算」
    产生的运行写到临时目录，避免污染工作区 ``runs/``。
    """
    override = os.environ.get("UFDEMO_RUNS_DIR")
    if override:
        return Path(override)
    return project_root() / "runs"


def default_curves_dir() -> Path:
    """默认响应曲线目录（批次 F 的查表卡：``*.curve.json`` + ``*.points.csv``）。"""
    override = os.environ.get("UFDEMO_CURVES_DIR")
    if override:
        return Path(override)
    return project_root() / "data" / "curves"
