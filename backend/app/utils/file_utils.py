"""File-related utility helpers."""

from __future__ import annotations

from pathlib import Path


def ensure_dir(path: Path | str) -> Path:
    """Create a directory if it doesn't exist and return its Path."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def readable_size(size_bytes: int) -> str:
    """Return a human-readable file size string."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes} {unit}"
        size_bytes //= 1024
    return f"{size_bytes} TB"


def safe_ext(filename: str) -> str:
    """Return a lowercase extension *without* the dot, e.g. 'docx'."""
    return Path(filename).suffix.lstrip(".").lower()
# file_utils:文件名清洗 / 扩展名推断 / MIME 嗅探 / 大小校验;上传链路统一入口。
