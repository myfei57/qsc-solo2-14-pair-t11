"""请求参数与工艺数值校验。"""

from __future__ import annotations

from typing import Any, Iterable

from .errors import ValidationError


def require_text(value: Any, field: str, max_length: int = 200) -> str:
    """校验必填文本字段。"""

    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是文本", field=field, value=value)
    text = value.strip()
    if not text:
        raise ValidationError(f"{field} 不能为空", field=field)
    if len(text) > max_length:
        raise ValidationError(
            f"{field} 长度不能超过 {max_length}", field=field, length=len(text)
        )
    return text


def require_number(
    value: Any,
    field: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """校验数值字段并返回 ``float``。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} 必须是数字", field=field, value=value)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValidationError(f"{field} 必须是有限数字", field=field, value=value)
    if minimum is not None and number < minimum:
        raise ValidationError(
            f"{field} 不能小于 {minimum}", field=field, value=number, minimum=minimum
        )
    if maximum is not None and number > maximum:
        raise ValidationError(
            f"{field} 不能大于 {maximum}", field=field, value=number, maximum=maximum
        )
    return number


def require_int(
    value: Any,
    field: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """校验整数字段。"""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} 必须是整数", field=field, value=value)
    if minimum is not None and value < minimum:
        raise ValidationError(
            f"{field} 不能小于 {minimum}", field=field, value=value, minimum=minimum
        )
    if maximum is not None and value > maximum:
        raise ValidationError(
            f"{field} 不能大于 {maximum}", field=field, value=value, maximum=maximum
        )
    return value


def require_choice(value: Any, field: str, choices: Iterable[str]) -> str:
    """校验枚举取值。"""

    allowed = tuple(choices)
    text = require_text(value, field=field)
    if text not in allowed:
        raise ValidationError(
            f"{field} 取值不在允许范围", field=field, value=text, allowed=list(allowed)
        )
    return text
