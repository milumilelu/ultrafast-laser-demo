"""M3 法向、投影与可见性 —— **批次 J（T18）交付，本批仅占位**。

执行细则第 8 节与任务书 6.6 节规定：M0/M1/M2 只开放正入射，
斜入射、法向厚度转换和遮挡功能只在 M3 开放。因此本模块在本批不提供实现。

批次 J 必须实现（细则 9.2）：

* ``q=(x,y,h)``、``n=(-h_x,-h_y,1)/sqrt(1+h_x^2+h_y^2)``、``mu=max(0,-k·n)``。
* 轴向距离 ``s=(q-q_f)·k``，横向 ``r^2=|q-q_f|^2-s^2``；``F_s=mu*F_perp``。
* 法向厚度到高度更新：``dh = -a_n/n_z``（**不是** ``-a_n*n_z``）。
* 无吸收依据时只做几何修正，界面禁用材料偏振吸收预测。
* 高度梯度用内部中心差分、边界单边差分，并通过平面解析法向检查。
* ``n_z >= 0.5``、入射角 <= 60° 之外停止该模式并给出位置与原因，不裁剪角度继续。
"""

from __future__ import annotations

from .errors import NOT_IMPLEMENTED as NOT_IMPLEMENTED_CODE
from .errors import UFDemoError


def surface_normal(*args, **kwargs):
    _raise("surface_normal")


def project_fluence(*args, **kwargs):
    _raise("project_fluence")


def first_intersection_visibility(*args, **kwargs):
    _raise("first_intersection_visibility")


def normal_thickness_to_height_drop(*args, **kwargs):
    _raise("normal_thickness_to_height_drop")


def _raise(name: str) -> None:
    raise UFDemoError(
        NOT_IMPLEMENTED_CODE,
        f"{name} 尚未实现：动态角度属 M3（T18）",
        field_path="geometry",
        actual=None,
        requirement="M3 阶段开放（细则 9.2）",
        suggestion="先完成 M0/M1/M2；未实现遮挡前只允许无自遮挡预设，不提供任意曲面开关后静默忽略阴影。",
    )
