"""ID generation utilities — produces kebab-style public IDs."""

from __future__ import annotations

import secrets
import time
import unicodedata
import re


_ID_PREFIXES: dict[str, str] = {
    "user": "user",
    "conversation": "conv",
    "message": "msg",
    "file": "file",
    "task": "task",
    "event": "evt",
    "tool_call": "tc",
    "confirmation": "confirm",
    "artifact": "art",
    "project": "prj",
    "project_source": "psrc",
    "project_knowledge_binding": "pkb",
    "template": "tpl",
    "template_version": "tplv",
    "user_template": "utpl",
    "model_config": "mdlcfg",
    "kb_config": "kbcfg",
    "sys_config": "syscfg",
}


def _random_suffix(length: int = 8) -> str:
    return secrets.token_hex(length // 2)


def _timestamp_suffix() -> str:
    return hex(int(time.time() * 1000))[-8:]


def generate_public_id(prefix_key: str) -> str:
    """Generate a public ID like 'conv_abc12d34'."""
    prefix = _ID_PREFIXES.get(prefix_key, prefix_key)
    return f"{prefix}_{_random_suffix()}"


def generate_request_id() -> str:
    return f"req_{_timestamp_suffix()}{_random_suffix(4)}"


def safe_filename(original_name: str) -> str:
    """Produce a safe, collision-resistant storage filename.

    Returns something like '20250619_a1b2c3d4_requirement.docx'.
    """
    name = unicodedata.normalize("NFKD", original_name or "unknown")
    name = re.sub(r"[^\w\-.]", "_", name, flags=re.UNICODE)
    name = re.sub(r"_+", "_", name).strip("_") or "file"
    date_part = time.strftime("%Y%m%d")
    suffix = _random_suffix()
    return f"{date_part}_{suffix}_{name}"
# ids:生成 task_id / conversation_id / artifact_id / public_id(雪花/UUID),所有 ID 唯一入口,避免 hardcode 字符串。
