"""原子快照读写。"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.errors import PersistenceError


def write_snapshot(path: Path, document: dict[str, Any], fsync: bool = True) -> int:
    """先写临时文件再原子替换，返回写入字节数。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        os.replace(temporary, target)
        if fsync:
            _sync_directory(target.parent)
    except OSError as exc:
        raise PersistenceError("快照写入失败", path=str(target)) from exc
    return len(text.encode("utf-8"))


def read_snapshot(path: Path) -> dict[str, Any] | None:
    """读取快照，不存在时返回 ``None``。"""

    target = Path(path)
    if not target.exists():
        return None
    try:
        text = target.read_text(encoding="utf-8")
        document = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise PersistenceError("快照读取失败", path=str(target)) from exc
    if not isinstance(document, dict):
        raise PersistenceError("快照根节点必须是对象", path=str(target))
    return document


def quarantine(path: Path, reason: str) -> str:
    """把损坏的快照移到隔离目录并返回新路径。"""

    target = Path(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = target.parent / "quarantine"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"{target.name}.{stamp}.bad"
    try:
        shutil.move(str(target), str(destination))
        (folder / f"{target.name}.{stamp}.reason").write_text(reason, encoding="utf-8")
    except OSError as exc:
        raise PersistenceError("损坏快照隔离失败", path=str(target)) from exc
    return str(destination)


def snapshot_size(path: Path) -> int:
    """返回快照文件大小，不存在时为 0。"""

    target = Path(path)
    if not target.exists():
        return 0
    return target.stat().st_size


def _sync_directory(folder: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(str(folder), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
