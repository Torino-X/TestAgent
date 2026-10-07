"""Atomic file writer — Phase 2.8R-E 临时文件 + fsync + atomic rename。

用途:写 Word / TXT / 任何用户上传的产物到磁盘,防止半成品文件被读到。
关键不变式:
  * 写到 ``.tmp`` 文件 → fsync → ``os.rename`` 替换。
  * 失败(中途崩、no space)→ 仅留下 .tmp,不污染目标路径。
  * 跨进程 / 同进程多次调用:tmp 文件名随机(uuid),避免 race。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

from app.core.exceptions import ArtifactAtomicWriteFailedError

logger = logging.getLogger(__name__)


def atomic_write_file(
    target_path: str | os.PathLike,
    content: bytes | str,
    *,
    encoding: str = "utf-8",
) -> str:
    """原子写文件 — 临时 + fsync + rename.

    Args:
        target_path: 最终目标路径(绝对路径或 Path)
        content: bytes 或 str(str 会按 encoding 编码)
        encoding: 仅对 str 有效

    Returns:
        target_path 字符串(方便链式调用)

    Raises:
        ArtifactAtomicWriteFailedError: 写或 rename 失败
    """
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # 准备临时文件(同目录以保证 rename 原子性)
    tmp_name = f".{target.name}.tmp.{uuid.uuid4().hex[:8]}"
    tmp_path = target.parent / tmp_name

    try:
        if isinstance(content, str):
            data = content.encode(encoding)
        else:
            data = content
        with open(tmp_path, mode="wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # 原子 rename(同目录 + 同 fs → POSIX atomic;Windows 不保证原子但 close-then-replace 一般 OK)
        os.replace(tmp_path, target)
        logger.info("AtomicFileWriter: %s (%d bytes) OK", target, len(data))
        return str(target)
    except OSError as exc:
        # 清理 tmp
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise ArtifactAtomicWriteFailedError(
            detail={
                "reason": "atomic_write_failed",
                "target_path": str(target),
                "error": str(exc),
            },
        ) from exc


async def atomic_write_bytes_async(
    target_path: str | os.PathLike,
    data: bytes,
) -> str:
    """Async wrapper — 当前实现是同步 I/O 直接跑(文件小),不需要 thread。

    Phase 2.8R-E 后续如需大文件支持可改成 ``await asyncio.to_thread``。
    """
    import asyncio

    return await asyncio.to_thread(atomic_write_file, target_path, data)


def safe_move_file(src_path: str | os.PathLike, target_path: str | os.PathLike) -> str:
    """Atomic 文件移动(simple wrapper) — 用于 WordExportTool 的产物落地。"""
    src = Path(src_path)
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # shutil.move 跨设备会 fallback 到 copy + unlink,
        # 但本场景(same fs)走 os.replace 原子 move
        os.replace(src, target)
        return str(target)
    except OSError as exc:
        # 跨 fs fallback
        shutil.move(str(src), str(target))
        return str(target)


__all__ = [
    "atomic_write_file",
    "atomic_write_bytes_async",
    "safe_move_file",
]


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (原子化文件写入 + tmp + rename):
#
#   链路:
#     WordExportTool.export_from_template(...) 期间
#       → AtomicFileWriter.write(path, content_bytes):
#         1. 写临时文件: target + '.tmp' (避免半截文件被读到)
#         2. fsync 刷盘
#         3. os.replace(tmp, target) (POSIX 原子)
#       失败 → 删 tmp + raise
#
# 关键约束(供开发者速查):
#   - 仅保证单进程原子;不解决多进程并发写同一文件(由 flock 负责);
#   - 失败必须删 tmp,避免残留;
#   - 路径必须在 local_storage 根目录之内,不允许 ../ 逃逸。
