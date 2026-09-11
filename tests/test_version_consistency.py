"""版本一致性：``ufdemo.__version__`` 必须与 ``pyproject.toml`` 的 version 相同。

批次 G 之后 ``__init__.py`` 的版本号被漏改过三次（H/I/J），界面上显示的版本
与实际交付批次不符。本测试把这条一致性钉死，避免再次漂移。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import ufdemo

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return str(data["project"]["version"])


def test_version_matches_pyproject():
    """两个版本号必须一致。"""
    assert ufdemo.__version__ == _pyproject_version(), (
        f"__init__.py 的 {ufdemo.__version__!r} 与 pyproject.toml 的 "
        f"{_pyproject_version()!r} 不一致"
    )


def test_version_has_batch_suffix():
    """版本号需带批次后缀（如 0.8.0-j1），便于从界面直接读出交付批次。"""
    assert re.fullmatch(r"\d+\.\d+\.\d+-[a-z]\d+", ufdemo.__version__), ufdemo.__version__


def test_code_version_reports_ufdemo_version():
    """运行落盘的 ``code_version()`` 必须带软件版本号。

    否则脱离 git（工作树被改、源码清单哈希变化）时，无法从运行目录判断
    该结果由哪个软件版本产生。
    """
    from ufdemo.io import code_version

    info = code_version(ROOT)
    assert info.get("ufdemo_version") == ufdemo.__version__, info
    assert "source_manifest_sha256" in info  # 非 git 环境下的溯源依据仍在
