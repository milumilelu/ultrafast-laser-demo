"""冻结几何批量加速 —— **批次 I（T16/T17）交付，本批仅占位**。

执行细则 9.1：第一版只允许固定阈值、同相、无历史及受支持路径。
每个脉冲单独计算非线性响应后相加，**禁止**先累加能流再取一次对数。
事件块和空间块同时限额，不构建全事件×全网格张量。
Numba 是可选依赖，缺失时回退 NumPy；不得并行化依赖历史的事件轴。
"""

from __future__ import annotations

from .errors import NOT_IMPLEMENTED as NOT_IMPLEMENTED_CODE
from .errors import UFDemoError


def grouped_solve(*args, **kwargs):
    raise UFDemoError(
        NOT_IMPLEMENTED_CODE,
        "分组批量求解尚未实现",
        field_path="solver.acceleration",
        actual=None,
        requirement="批次 I（T17）交付，需先通过 G08 逐脉冲对照",
        suggestion="使用 solver.acceleration=off 的逐脉冲参考实现。",
    )


def estimate_local_error(*args, **kwargs):
    raise UFDemoError(
        NOT_IMPLEMENTED_CODE,
        "局部步长误差估计尚未实现",
        field_path="solver.acceleration",
        requirement="批次 I（T17）",
        suggestion="见细则 9.1：B 与两个 B/2 试算比较更新场，失败则缩小 B 重试。",
    )


def numba_available() -> bool:
    """Numba 是可选依赖；缺失时回退 NumPy（任务书 8 节）。"""
    try:  # pragma: no cover - 取决于环境
        import numba  # noqa: F401

        return True
    except Exception:  # pragma: no cover
        return False
