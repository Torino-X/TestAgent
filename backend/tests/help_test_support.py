from __future__ import annotations

from pathlib import Path


def write_help_fixture(root: Path) -> Path:
    articles = root / "articles"
    assets = root / "assets"
    articles.mkdir(parents=True)
    assets.mkdir(parents=True)
    (articles / "quick-start.md").write_text(
        """# 5 分钟快速开始

## 准备文件

准备需求文档和测试方案模板。

## 准备文件

确认文件用途后开始生成。
""",
        encoding="utf-8",
    )
    (articles / "template.md").write_text(
        """# 为什么采用模板驱动

## 模板驱动

TestAgent 使用测试方案模板控制结构，并在 Word 模板中回填内容。
""",
        encoding="utf-8",
    )
    (assets / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    (root / "manifest.yaml").write_text(
        """schema_version: 1
site:
  title: 帮助中心
  description: TestAgent 使用说明
sections:
  - id: getting-started
    title: 快速开始
    order: 10
    articles:
      - slug: quick-start
        title: 5 分钟快速开始
        description: 从准备文件开始
        file: articles/quick-start.md
        order: 10
        keywords: [快速开始, 测试方案]
        featured: true
        updated_at: '2026-09-17'
  - id: concepts
    title: 认识 TestAgent
    order: 20
    articles:
      - slug: template-driven
        title: 为什么采用模板驱动
        description: 了解模板的作用
        file: articles/template.md
        order: 10
        keywords: [模板, Word]
        featured: true
        updated_at: '2026-09-17'
""",
        encoding="utf-8",
    )
    return root
