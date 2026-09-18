"""标识与批次编号生成。"""

from __future__ import annotations

import re
import uuid

from .errors import ValidationError

_PREFIX = re.compile(r"^[a-z][a-z0-9_]{1,15}$")
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """把配方名或单元名折叠成短标识。"""

    if not isinstance(text, str):
        raise ValidationError("名称必须是文本", value=text)
    slug = _NON_SLUG.sub("-", text.strip().lower()).strip("-")
    if not slug:
        raise ValidationError("名称无法生成标识", value=text)
    return slug


def new_id(prefix: str) -> str:
    """生成 ``prefix-xxxxxxxxxxxx`` 形式的对象标识。"""

    if not _PREFIX.match(prefix):
        raise ValidationError("标识前缀不合法", prefix=prefix)
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def batch_code(sequence: int, style: str) -> str:
    """生成人类可读的批次号，例如 ``B0007-IPA``。"""

    if sequence < 1:
        raise ValidationError("批次序号必须为正整数", sequence=sequence)
    short_style = slugify(style).replace("-", "")[:8].upper() or "BREW"
    return f"B{sequence:04d}-{short_style}"


def tank_code(index: int) -> str:
    """生成发酵罐编号，例如 ``FV-03``。"""

    if index < 1:
        raise ValidationError("发酵罐序号必须为正整数", index=index)
    return f"FV-{index:02d}"


def circuit_code(index: int) -> str:
    """生成 CIP 回路编号，例如 ``CIP-02``。"""

    if index < 1:
        raise ValidationError("CIP 回路序号必须为正整数", index=index)
    return f"CIP-{index:02d}"
