"""
模板章节数据模型（从 legacy_sources 等价迁移）。

与旧项目的差异（架构适配）：
- 移除了 expanded 字段（Qt UI 展开状态），改为服务端默认 True
- 其余字段和行为完全保留
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class TemplateSection:
    """测试方案模板中的一个章节节点。

    Attributes:
        title: 章节标题文本（含编号前缀，如 "1 项目概述"）
        level: 标题层级（0=文档大标题, 1=一级, 2=二级...）
        mode: 生成模式 "ai" / "keep" / "manual"
        checked: 用户是否勾选该章节参与生成
        paragraph_index: 在 doc.paragraphs 中的序号
        body_start_index: 在 doc body 子元素中的起始位置
        body_end_index: 在 doc body 子元素中的结束位置
        table_indexes: 该章节范围内的表格索引列表
        table_schemas: 表格 schema 元信息列表
        source: 标题识别来源 "style" / "outline" / "numbering" / "title"
        description: 章节标题下方的斜体描述文字
        children: 子章节列表
    """

    title: str
    level: int
    mode: str = "ai"
    checked: bool = True
    paragraph_index: int | None = None
    body_start_index: int | None = None
    body_end_index: int | None = None
    table_indexes: List[int] = field(default_factory=list)
    table_schemas: List[Dict[str, object]] = field(default_factory=list)
    source: str = ""
    description: str = ""
    children: List["TemplateSection"] = field(default_factory=list)

    def walk(self) -> List["TemplateSection"]:
        """递归展平树形结构为一维列表。"""
        sections = [self]
        for child in self.children:
            sections.extend(child.walk())
        return sections

    def to_dict(self) -> dict:
        """转为字典，用于 JSON 序列化和 Tool 输出。"""
        return {
            "title": self.title,
            "level": self.level,
            "mode": self.mode,
            "checked": self.checked,
            "paragraph_index": self.paragraph_index,
            "body_start_index": self.body_start_index,
            "body_end_index": self.body_end_index,
            "table_indexes": self.table_indexes,
            "table_schemas": self.table_schemas,
            "source": self.source,
            "description": self.description,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass
class TemplateParseResult:
    """模板解析结果，包含树形章节结构和统计信息。

    Attributes:
        template_name: 模板文件名
        sections: 顶层章节列表（树形结构）
        table_count: 模板中的表格总数
        fixed_approval_count: 包含审批/签署/修订关键词的章节数量
    """

    template_name: str
    sections: List[TemplateSection] = field(default_factory=list)
    table_count: int = 0
    fixed_approval_count: int = 0

    def all_sections(self) -> List[TemplateSection]:
        """展平所有章节节点（树 → 一维列表）。"""
        sections: List[TemplateSection] = []
        for section in self.sections:
            sections.extend(section.walk())
        return sections

    @property
    def top_level_count(self) -> int:
        return len(self.sections)

    @property
    def child_count(self) -> int:
        return max(0, len(self.all_sections()) - self.top_level_count)

    def to_dict(self) -> dict:
        """转为字典格式。"""
        return {
            "template_name": self.template_name,
            "sections": [s.to_dict() for s in self.sections],
            "table_count": self.table_count,
            "fixed_approval_count": self.fixed_approval_count,
        }


# 模块定位:模板章节数据模型(从 legacy_sources 等价迁移)
#
# 数据类:
#   - TemplateSection(level / title / description / ai_fields / keep_sections /
#     table_schemas / content_type)
#
# 关键约束:
#   - 不放磁盘 IO 状态(纯 in-memory 模型);
#   - 与 template_section_service 是 model/service 拆分,保持轻;
#   - 移除原 legacy expanded 字段(Qt UI 状态,服务端默认 True);
#   - 章节字典序列化走 Pydantic v2,字段顺序在 JSON 输出中保持。
