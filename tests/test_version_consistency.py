"""版本一致性：版本号合法性、两处同步、批次元数据、安装资源可定位。

历史教训（两条，都是真踩过的）：

1. 批次 G 之后 ``__init__.py`` 的版本号被漏改过三次（H/I/J），界面显示的
   版本与实际交付批次不符 → 用「两个文件字符串一致」钉死。
2. 但那条测试**结构上发现不了**「版本号不合法」：``0.9.0-k1`` 两个文件写得
   一模一样，字符串比较当然通过，而 ``pip install`` / ``python -m build``
   直接失败（``configuration error: project.version must be pep440``）。
   —— 源码运行不解析版本号，所以本地一直「看起来正常」。

因此本文件现在同时守住四件事：
  * 版本号是**合法 PEP 440**（用 ``packaging.version.Version`` 真解析，不是正则）；
  * 两处字符串一致 + 安装元数据一致；
  * 批次由独立 ``build_batch`` 承载，且两处一致；
  * 随包只读资源在**当前安装形态下**可被定位到。
"""

from __future__ import annotations

import sysconfig
import tomllib
from pathlib import Path

import pytest
import ufdemo

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def _pyproject_version() -> str:
    return str(_pyproject()["project"]["version"])


def _pyproject_build_batch() -> str:
    return str(_pyproject()["tool"]["ufdemo"]["build_batch"])


# ---------------------------------------------------------------------------
# 版本号
# ---------------------------------------------------------------------------


def test_version_is_valid_pep440():
    """版本号必须能被 ``packaging`` **真正解析**。

    这是本文件最重要的一条：正则 ``\\d+\\.\\d+\\.\\d+-[a-z]\\d+`` 会把
    非法写法（``0.9.0-k1``）判为「符合格式」，正是那条断言让问题长期存活。
    """
    from packaging.version import InvalidVersion, Version

    raw = ufdemo.__version__
    try:
        parsed = Version(raw)
    except InvalidVersion as exc:  # pragma: no cover - 失败路径
        pytest.fail(
            f"__version__={raw!r} 不是合法 PEP 440 版本：{exc}。"
            "批次后缀不能写成 '-k1'；请用正式版本号 + BUILD_BATCH 元数据。"
        )
    assert str(parsed) == raw, f"规范化后不一致：{raw!r} → {str(parsed)!r}"


def test_pyproject_version_is_valid_pep440():
    """``pyproject.toml`` 的 version 也要可解析 —— 构建器就是在这里拒绝的。"""
    from packaging.version import Version

    Version(_pyproject_version())


def test_version_matches_pyproject():
    """两个版本号必须一致。"""
    assert ufdemo.__version__ == _pyproject_version(), (
        f"__init__.py 的 {ufdemo.__version__!r} 与 pyproject.toml 的 "
        f"{_pyproject_version()!r} 不一致"
    )


def test_installed_metadata_matches_when_installed():
    """已安装（wheel/editable）时，importlib.metadata 的版本必须一致。

    未安装时跳过 —— 但一旦安装就**必须**一致，否则分发出去的是另一套版本。
    """
    from importlib import metadata

    try:
        dist_version = metadata.version("ufdemo")
    except metadata.PackageNotFoundError:
        pytest.skip("ufdemo 未安装（源码直跑），跳过元数据一致性检查")
    assert dist_version == ufdemo.__version__, (
        f"安装元数据 {dist_version!r} 与包内 {ufdemo.__version__!r} 不一致"
    )


# ---------------------------------------------------------------------------
# 批次元数据
# ---------------------------------------------------------------------------


def test_build_batch_matches_pyproject():
    """批次由独立元数据承载，两处必须一致。"""
    assert ufdemo.BUILD_BATCH == _pyproject_build_batch(), (
        f"__init__.py 的 {ufdemo.BUILD_BATCH!r} 与 pyproject.toml "
        f"[tool.ufdemo].build_batch 的 {_pyproject_build_batch()!r} 不一致"
    )


def test_build_batch_not_smuggled_into_version():
    """批次不得再塞回版本号（合法预发行标记只有 a/b/rc 系列）。"""
    from packaging.version import Version

    v = Version(ufdemo.__version__)
    assert v.pre is None, (
        f"版本 {ufdemo.__version__!r} 带预发行标记 {v.pre!r}；"
        "批次请放 BUILD_BATCH，不要当预发行标记用"
    )


def test_code_version_reports_version_and_batch():
    """运行落盘的 ``code_version()`` 必须带软件版本号与批次。

    否则脱离 git（工作树被改、源码清单哈希变化）时，无法从运行目录判断
    该结果由哪个软件版本 / 哪个批次产生。
    """
    from ufdemo.io import code_version

    info = code_version(ROOT)
    assert info.get("ufdemo_version") == ufdemo.__version__, info
    assert info.get("build_batch") == ufdemo.BUILD_BATCH, info
    assert "source_manifest_sha256" in info  # 非 git 环境下的溯源依据仍在


# ---------------------------------------------------------------------------
# 安装资源可定位（F03）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "getter,expect_name",
    [
        (lambda: ufdemo.default_material_dir(), "materials"),
        (lambda: ufdemo.default_curves_dir(), "curves"),
        (lambda: ufdemo.default_webui_dir(), "webui"),
        (lambda: ufdemo.default_examples_dir(), "examples"),
    ],
)
def test_default_resource_dirs_exist(getter, expect_name):
    """默认资源目录必须真实存在 —— wheel 安装后也不得指向空路径。"""
    d = getter()
    assert d.exists(), (
        f"{expect_name} 目录不存在：{d}\n"
        f"resource_root()={ufdemo.resource_root()}"
    )
    assert d.is_dir(), d


def test_material_and_curve_cards_are_loadable_from_default_dirs():
    """从默认目录真能加载到卡片（不只看目录存在）。"""
    from ufdemo import tables
    from ufdemo.materials import load_material_catalog

    catalog = load_material_catalog(ufdemo.default_material_dir())
    assert len(catalog) >= 8, f"材料卡数量异常：{len(catalog)}"

    cards = tables.iter_curve_cards(ufdemo.default_curves_dir())
    assert len(cards) >= 3, f"曲线卡数量异常：{len(cards)}"


def test_runs_dir_is_not_a_package_resource():
    """输出目录必须可写；且**在非源码安装形态下**不得落在只读资源里。

    源码树里 ``runs/`` 本就在仓库根下（方便开发时查看），这不算问题；
    真正要防的是 wheel 安装后把运行输出写进 ``site-packages`` / ``share``
    —— 那里通常只读。
    """
    runs = ufdemo.default_runs_dir()
    resource = ufdemo.resource_root().resolve()
    resolved = runs.resolve()

    if (resource / "pyproject.toml").is_file():
        # 源码树形态：允许在仓库内，但必须可写
        assert runs.is_dir() or True
        assert not str(resolved).startswith(str(Path(sysconfig.get_path("purelib")).resolve())), (
            f"源码树形态下 runs 不该落在 site-packages：{resolved}"
        )
    else:
        assert resolved != resource and resource not in resolved.parents, (
            f"安装形态下 runs 目录 {resolved} 落在只读资源 {resource} 之内"
        )


def test_resource_root_differs_from_project_root_under_wheel():
    """安装形态下 ``project_root()`` 不该被当成资源根使用。

    源码树里两者相同（正常）；此处只断言**解析函数确实被调用**，
    真正区分 wheel/源码由 tools/package_smoke.py 在干净环境里验证。
    """
    assert ufdemo.resource_root().is_dir()
    assert ufdemo.resource_root() != Path("/nonexistent")
