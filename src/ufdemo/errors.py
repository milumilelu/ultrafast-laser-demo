"""机器可读错误类型。

执行细则第 11.2 节要求至少区分下列错误码。本模块是唯一错误码来源，
所有模块抛出 ``UFDemoError``，禁止用裸 ``ValueError`` 传递可归类错误。

本文件相对细则第 3 节的建议布局属于新增文件（``errors.py``），原因：细则要求
错误能在配置、材料、响应、求解、导出各层之间统一传播。已在
``docs/decisions/ADR-0006-error-taxonomy.md`` 记录。
"""

from __future__ import annotations

from typing import Any

# --- 执行细则 11.2 规定的错误码 -------------------------------------------------
CONFIG_INVALID = "CONFIG_INVALID"
ENERGY_CONFLICT = "ENERGY_CONFLICT"
MATERIAL_CAPABILITY_MISSING = "MATERIAL_CAPABILITY_MISSING"
CONDITION_MISMATCH = "CONDITION_MISMATCH"
RESPONSE_SEMANTICS_INVALID = "RESPONSE_SEMANTICS_INVALID"
TABLE_OUT_OF_RANGE = "TABLE_OUT_OF_RANGE"
RESOURCE_BUDGET_EXCEEDED = "RESOURCE_BUDGET_EXCEEDED"
GEOMETRY_UNSUPPORTED = "GEOMETRY_UNSUPPORTED"
NUMERIC_NONFINITE = "NUMERIC_NONFINITE"

# --- 本批新增（不属于细则 11.2 的必需集合，用于区分“尚未实现”与“配置错误”）---
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"

ALL_CODES: tuple[str, ...] = (
    CONFIG_INVALID,
    ENERGY_CONFLICT,
    MATERIAL_CAPABILITY_MISSING,
    CONDITION_MISMATCH,
    RESPONSE_SEMANTICS_INVALID,
    TABLE_OUT_OF_RANGE,
    RESOURCE_BUDGET_EXCEEDED,
    GEOMETRY_UNSUPPORTED,
    NUMERIC_NONFINITE,
    NOT_IMPLEMENTED,
)

# 中文原因，供界面显示（细则 11.2：CLI 返回非零退出码；UI 显示中文原因）
CODE_ZH: dict[str, str] = {
    CONFIG_INVALID: "配置无效",
    ENERGY_CONFLICT: "能量与功率输入不一致",
    MATERIAL_CAPABILITY_MISSING: "材料能力缺失",
    CONDITION_MISMATCH: "激光条件与材料卡不匹配",
    RESPONSE_SEMANTICS_INVALID: "响应语义非法",
    TABLE_OUT_OF_RANGE: "查表越界",
    RESOURCE_BUDGET_EXCEEDED: "超出资源预算",
    GEOMETRY_UNSUPPORTED: "当前几何组合不受支持",
    NUMERIC_NONFINITE: "出现非有限数值",
    NOT_IMPLEMENTED: "该功能尚未实现",
}


class UFDemoError(Exception):
    """带字段路径、实际值、要求与修正建议的可机器读取错误。

    Parameters
    ----------
    code:
        上述错误码之一。
    message:
        简短英文/中文描述。
    field_path:
        出错字段的点分路径，例如 ``laser.pulse_energy_J``。
    actual:
        实际读到的值（``None`` 表示字段缺失）。
    requirement:
        该字段的要求。
    suggestion:
        可执行的修正建议。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field_path: str | None = None,
        actual: Any = None,
        requirement: str | None = None,
        suggestion: str | None = None,
    ) -> None:
        if code not in ALL_CODES:
            raise ValueError(f"unknown error code: {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.field_path = field_path
        self.actual = actual
        self.requirement = requirement
        self.suggestion = suggestion

    @property
    def code_zh(self) -> str:
        return CODE_ZH[self.code]

    def to_dict(self) -> dict[str, Any]:
        """稳定序列化，便于写入 ``diagnostics.json`` 与 CLI JSON 输出。"""
        return {
            "code": self.code,
            "code_zh": self.code_zh,
            "message": self.message,
            "field_path": self.field_path,
            "actual": _jsonable(self.actual),
            "requirement": self.requirement,
            "suggestion": self.suggestion,
        }

    def format_human(self) -> str:
        parts = [f"[{self.code}] {self.message}"]
        if self.field_path:
            parts.append(f"  字段：{self.field_path}")
        if self.actual is not None:
            parts.append(f"  实际值：{self.actual!r}")
        if self.requirement:
            parts.append(f"  要求：{self.requirement}")
        if self.suggestion:
            parts.append(f"  建议：{self.suggestion}")
        return "\n".join(parts)

    def __str__(self) -> str:  # pragma: no cover - 直接复用
        return self.format_human()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return repr(value)
