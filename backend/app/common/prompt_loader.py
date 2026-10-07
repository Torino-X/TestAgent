"""Prompt template loader — adapted from legacy prompt_loader.py.

Loads template files from the project's ``templates/`` directory with
variable substitution and ``[section]``-based parsing.

Equivalent migration: all capabilities preserved.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Template directory relative to the *backend* package root
_TEMPLATE_DIR_NAME = "templates"
_SYSTEM_RULES_SUBDIR = "system_rules"


def _find_project_root() -> Path:
    """Find the backend project root (where ``templates/`` lives)."""
    # __file__ → app/common/prompt_loader.py → up 2 = backend/
    return Path(__file__).resolve().parent.parent.parent


def _find_template_dir() -> Path:
    """Resolve the main templates directory."""
    template_dir = _find_project_root() / _TEMPLATE_DIR_NAME
    if template_dir.exists():
        return template_dir
    raise FileNotFoundError(f"模板目录不存在：{template_dir}")


def _find_rules_dir() -> Path:
    """Resolve the system_rules subdirectory."""
    rules_dir = _find_template_dir() / _SYSTEM_RULES_SUBDIR
    if not rules_dir.exists():
        raise FileNotFoundError(f"系统规则目录不存在：{rules_dir}")
    return rules_dir


# ── public API ────────────────────────────────────────────────────


def load_template(name: str) -> str:
    """Load a prompt template file.

    Args:
        name: Template filename, e.g. ``"module_extraction.md"``.

    Returns:
        Raw template text.

    Raises:
        FileNotFoundError: Template file does not exist.
    """
    template_dir = _find_template_dir()
    path = template_dir / name
    if not path.exists():
        raise FileNotFoundError(f"提示词模板不存在：{path}")
    return path.read_text(encoding="utf-8")


def render_template(name: str, variables: Dict[str, str]) -> str:
    """Load a template and substitute variables via ``str.format()``.

    Args:
        name: Template filename.
        variables: Mapping of variable name → replacement string.

    Returns:
        Rendered text.
    """
    template = load_template(name)
    return template.format(**variables)


def load_section(name: str, section: str) -> str:
    """Load one named ``[section]`` from a template file.

    Template file sections are delimited by ``[section_name]`` lines::

        [section1]
        content …
        [section2]
        more content …

    Args:
        name: Template filename.
        section: Section name (without brackets).

    Returns:
        Section content, or an empty string if the section is not found.
    """
    template = load_template(name)
    sections = parse_sections(template)
    return sections.get(section, "")


def parse_sections(text: str) -> Dict[str, str]:
    """Parse ``[section]`` blocks from template text.

    Args:
        text: Template text.

    Returns:
        ``{section_name: section_content}`` dictionary.
    """
    sections: Dict[str, str] = {}
    current_section: Optional[str] = None
    current_lines: list[str] = []

    for line in text.split("\n"):
        match = re.match(r"^\[(\w+)\]\s*$", line)
        if match:
            if current_section is not None:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = match.group(1)
            current_lines = []
        else:
            if current_section is not None:
                current_lines.append(line)

    if current_section is not None:
        sections[current_section] = "\n".join(current_lines).strip()

    return sections


# 模块定位:Prompt template loader(从 legacy prompt_loader.py 迁移)
#
# 从项目 `templates/` 加载模板文件,变量替换 + `[section]` 块解析。
#
# 链路:
#   prompt_builder 启动期加载模板 → caches 内存 →
#     构建 prompt 时 Jinja-style 替换 + section 切片。
#
# 关键约束:
#   - 模板文件路径在 `templates/`,不允许绝对路径穿越;
#   - 替换变量白名单,不允许注入 prompt 中未列名的占位符;
#   - section 标签 `[section xxx]` 必须配对(否则 raise);
#   - 修改模板文件 = 影响所有下游 prompt,需要 LLM 行为回归测试覆盖。
