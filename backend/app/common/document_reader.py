"""
需求文档读取模块（从 legacy_sources 等价迁移）。

支持 .docx / .txt / .md 三种格式，对 .docx 文件按文档原始顺序
提取段落文本、表格内容和嵌入图片，图片经由 local_storage 保存。

与旧项目的差异（架构适配）：
- 文件路径来源改为 local_storage 获取的 storage_path
- 图片落盘目录改为 local_storage 管理的 temp 子目录
- 异常映射为 DocumentReadError → Tool 层再转为标准错误结构
- 移除了 PyQt/QML 路径依赖（app_paths.get_cache_dir）
- 保留了全部解析能力：段落/表格/图片提取、编码 fallback、SHA1 去重
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

from docx import Document
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.storage.local_storage import local_storage

logger = logging.getLogger(__name__)


class DocumentReadError(Exception):
    """文档读取异常。"""


@dataclass
class RequirementBlock:
    """需求文档中的一个内容块：文本段落、表格或图片。"""

    block_type: str          # "text" / "table" / "image"
    text: str = ""           # 文本内容（text/table 类型使用）
    index: int = 0           # 同类块序号（表格从 1 开始，图片从 1 开始）
    image_path: Optional[Path] = None  # 图片保存到本地的路径（image 类型使用）
    content_type: str = ""   # 图片 MIME 类型
    source: str = ""         # 来源描述（如 "段落"、"表格单元格 R2C3"）


@dataclass
class DocumentReadResult:
    """文档读取结果，包含按原始顺序排列的内容块列表。"""

    blocks: List[RequirementBlock] = field(default_factory=list)

    @property
    def image_blocks(self) -> List[RequirementBlock]:
        """过滤出所有图片块。"""
        return [block for block in self.blocks if block.block_type == "image"]

    def to_prompt_text(self, image_understanding: Optional[dict[int, object]] = None) -> str:
        """将全部内容块拼接为 prompt 文本。

        image_understanding: {图片序号: ImageUnderstandingResult}，提供时用理解结果替代占位信息。
        """
        lines: List[str] = []
        image_understanding = image_understanding or {}
        for block in self.blocks:
            if block.block_type == "text" and block.text:
                lines.append(block.text)
            elif block.block_type == "table" and block.text:
                lines.append(f"【表格 {block.index}】\n{block.text}")
            elif block.block_type == "image" and block.image_path:
                understood = image_understanding.get(block.index)
                if understood and hasattr(understood, "to_prompt_text"):
                    lines.append(understood.to_prompt_text())
                else:
                    lines.append(
                        "\n".join(
                            [
                                f"【图片 {block.index}】",
                                f"来源位置：{block.source or '需求文档'}",
                                f"本地路径：{block.image_path}",
                                "处理状态：已提取，等待图片理解模型解析。",
                            ]
                        )
                    )
        return "\n\n".join(line for line in lines if line.strip())


class DocumentReader:
    """需求文档读取器。

    读取 .docx / .txt / .md 文件，对 .docx 按文档原始顺序提取
    段落文本、表格内容和嵌入图片。

    OCR / 视觉理解不再是 DocumentReader 的职责 — 它们由 OcrService
    和（未来的）VisionClient 独立提供。DocumentReader 仅负责从文件
    中提取文本块和图片二进制数据。
    """

    SUPPORTED_EXTENSIONS = {".docx", ".txt", ".md"}

    def __init__(self, image_output_dir: str | Path | None = None):
        """image_output_dir 默认使用 local_storage 的 temp 子目录。"""
        if image_output_dir:
            self.image_output_dir = Path(image_output_dir)
        else:
            self.image_output_dir = local_storage._base / "temp" / "extracted_images"

    def read(self, file_path: str) -> str:
        """读取文档并返回纯文本（便捷方法）。"""
        result = self.read_structured(file_path)
        return result.to_prompt_text()

    def read_structured(self, file_path: str) -> DocumentReadResult:
        """读取文档并返回结构化结果（保留文本/表格/图片顺序）。"""
        path = Path(file_path)
        suffix = path.suffix.lower()
        if suffix not in self.SUPPORTED_EXTENSIONS:
            raise DocumentReadError(f"不支持的需求文档格式：{path.suffix}")
        if not path.exists():
            raise DocumentReadError("需求文档不存在")

        try:
            if suffix == ".docx":
                return self._read_docx_structured(path)
            return DocumentReadResult([RequirementBlock("text", text=self._read_text_file(path))])
        except DocumentReadError:
            raise
        except Exception as exc:
            raise DocumentReadError(f"读取需求文档失败：{exc}") from exc

    def _read_text_file(self, path: Path) -> str:
        """读取纯文本文件，先尝试 UTF-8，失败则用 GBK。"""
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="gbk")

    # ── docx 结构化读取 ──────────────────────────────────────────

    def _read_docx_structured(self, path: Path) -> DocumentReadResult:
        """按文档 body 子元素顺序遍历，依次提取段落和表格中的文本与图片。"""
        doc = Document(str(path))
        blocks: List[RequirementBlock] = []
        table_index = 1
        image_index = 1
        image_dir = self._doc_image_dir(path)

        for element in self._iter_document_blocks(doc):
            if isinstance(element, Paragraph):
                paragraph_blocks, image_index = self._paragraph_to_blocks(
                    element, doc, path, image_dir, image_index, "段落",
                )
                blocks.extend(paragraph_blocks)
                continue

            table_text = self._table_to_text(element)
            if table_text:
                blocks.append(RequirementBlock("table", text=table_text, index=table_index, source="表格"))
                table_index += 1
            table_image_blocks, image_index = self._table_images_to_blocks(
                element, doc, path, image_dir, image_index,
            )
            blocks.extend(table_image_blocks)

        return DocumentReadResult(blocks)

    @staticmethod
    def _iter_document_blocks(doc: Document) -> Iterable[Paragraph | Table]:
        """遍历 docx body 的直接子元素，按类型转为 Paragraph 或 Table。"""
        for child in doc.element.body.iterchildren():
            if isinstance(child, CT_P):
                yield Paragraph(child, doc)
            elif isinstance(child, CT_Tbl):
                yield Table(child, doc)

    def _paragraph_to_blocks(
        self,
        paragraph: Paragraph,
        doc: Document,
        doc_path: Path,
        image_dir: Path,
        image_index: int,
        source: str,
    ) -> tuple[List[RequirementBlock], int]:
        """解析段落：收集文本 run，发现内嵌图片时先输出文本块再输出图片块。"""
        blocks: List[RequirementBlock] = []
        pending_text: List[str] = []

        for run in paragraph.runs:
            if run.text:
                pending_text.append(run.text)
            rel_ids = self._image_rel_ids(run._element)
            if rel_ids:
                self._flush_text_block(blocks, pending_text, source)
                for rel_id in rel_ids:
                    image_block = self._image_block_from_rel(
                        doc, rel_id, doc_path, image_dir, image_index, source,
                    )
                    if image_block:
                        blocks.append(image_block)
                        image_index += 1

        if paragraph.text and not paragraph.runs:
            pending_text.append(paragraph.text)
        self._flush_text_block(blocks, pending_text, source)
        return blocks, image_index

    @staticmethod
    def _table_to_text(table: Table) -> str:
        """将 Word 表格转为管道分隔的文本表示（单元格用 " | " 连接）。"""
        lines: List[str] = []
        for row in table.rows:
            cells = [DocumentReader._normalize_cell_text(cell.text) for cell in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))
        return "\n".join(lines)

    def _table_images_to_blocks(
        self,
        table: Table,
        doc: Document,
        doc_path: Path,
        image_dir: Path,
        image_index: int,
    ) -> tuple[List[RequirementBlock], int]:
        """遍历表格所有单元格的段落，提取其中嵌入的图片。"""
        blocks: List[RequirementBlock] = []
        for row_index, row in enumerate(table.rows, start=1):
            for col_index, cell in enumerate(row.cells, start=1):
                for paragraph in cell.paragraphs:
                    paragraph_blocks, image_index = self._paragraph_to_blocks(
                        paragraph, doc, doc_path, image_dir, image_index,
                        f"表格单元格 R{row_index}C{col_index}",
                    )
                    blocks.extend(block for block in paragraph_blocks if block.block_type == "image")
        return blocks, image_index

    # ── 文本块工具 ───────────────────────────────────────────────

    @staticmethod
    def _flush_text_block(blocks: List[RequirementBlock], pending_text: List[str], source: str) -> None:
        """将缓存的文本片段合并输出为一个文本块，并清空缓存。"""
        text = "".join(pending_text).strip()
        pending_text.clear()
        if text:
            blocks.append(RequirementBlock("text", text=text, source=source))

    # ── 图片提取与落盘 ───────────────────────────────────────────

    @staticmethod
    def _image_rel_ids(element) -> List[str]:
        """查找 XML 元素中的所有图片引用 ID（a:blip 的 r:embed 或 r:link）。"""
        rel_ids: List[str] = []
        for blip in element.xpath(".//a:blip"):
            rel_id = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
            if rel_id:
                rel_ids.append(rel_id)
        return rel_ids

    def _image_block_from_rel(
        self,
        doc: Document,
        rel_id: str,
        doc_path: Path,
        image_dir: Path,
        image_index: int,
        source: str,
    ) -> Optional[RequirementBlock]:
        """根据 relationship ID 获取图片数据并保存到本地，返回图片块。"""
        image_part = doc.part.related_parts.get(rel_id)
        if not image_part or not hasattr(image_part, "blob"):
            return None
        image_path = self._save_image_blob(doc_path, image_dir, image_index, image_part)
        return RequirementBlock(
            "image",
            index=image_index,
            image_path=image_path,
            content_type=getattr(image_part, "content_type", ""),
            source=source,
        )

    def _save_image_blob(self, doc_path: Path, image_dir: Path, image_index: int, image_part) -> Path:
        """将图片二进制数据写入磁盘，文件名为 image_{序号}_{sha1}.{后缀}。

        使用 SHA1 去重：相同内容不重复写入。
        """
        image_dir.mkdir(parents=True, exist_ok=True)
        blob = image_part.blob
        digest = hashlib.sha1(blob).hexdigest()[:10]
        suffix = self._image_suffix(image_part)
        image_path = image_dir / f"image_{image_index:03d}_{digest}{suffix}"
        if not image_path.exists() or image_path.read_bytes() != blob:
            image_path.write_bytes(blob)
        return image_path

    @staticmethod
    def _doc_image_dir(path: Path) -> Path:
        """为每个文档生成独立的图片输出子目录：doc_{路径sha1前8位}。"""
        digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:8]
        return local_storage._base / "temp" / "extracted_images" / f"doc_{digest}"

    @staticmethod
    def _image_suffix(image_part) -> str:
        """根据图片 part 的扩展名或 MIME 类型推断文件后缀。"""
        part_suffix = Path(str(getattr(image_part, "partname", ""))).suffix
        if part_suffix:
            return part_suffix
        return {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/gif": ".gif",
            "image/bmp": ".bmp",
            "image/tiff": ".tif",
            "image/webp": ".webp",
        }.get(getattr(image_part, "content_type", ""), ".img")

    @staticmethod
    def _normalize_cell_text(text: str) -> str:
        """将表格单元格中的多行文本合并为单行（空格分隔）。"""
        return " ".join(part.strip() for part in text.splitlines() if part.strip())


# 模块定位:需求文档读取模块(支持 .docx / .txt / .md 三种格式)
#
# .docx 文件按文档原始顺序提取:
#   - 段落文本;
#   - 表格内容;
#   - 嵌入图片(走 local_storage 落盘)。
#
# 链路:
#   RequirementParserTool.run
#     → DocumentReader.read(path, mime) → {text_content, images, tables}
#       → 下一跳:ImageUnderstandingOrchestrator(图片) + result_parser(文本)
#
# 关键约束:
#   - .docx 解析走 python-docx 包,稳定但不解析加密 doc;
#   - 表格内容按行列顺序拼成可读文本,**不要**擅自"结构化"输出;
#   - 图片**仅**读到 local_storage,**不做**AI 理解(那是 Orchestrator 后面);
#   - 不放 LLM / DB 连接。
