"""File processing capability registry for attachment PHASE-1."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FileProcessingCapability:
    extension: str
    media_category: str
    can_parse: bool
    can_index: bool
    can_vision: bool
    can_ocr: bool
    can_semantic_profile: bool


class FileProcessingCapabilityRegistry:
    """Code-level truth for current file processing capabilities."""

    _CAPABILITIES: dict[str, FileProcessingCapability] = {
        "docx": FileProcessingCapability("docx", "document", True, True, False, False, True),
        "txt": FileProcessingCapability("txt", "document", True, True, False, False, True),
        "md": FileProcessingCapability("md", "document", True, True, False, False, True),
        "json": FileProcessingCapability("json", "document", True, True, False, False, True),
        "png": FileProcessingCapability("png", "image", False, False, True, True, False),
        "jpg": FileProcessingCapability("jpg", "image", False, False, True, True, False),
        "jpeg": FileProcessingCapability("jpeg", "image", False, False, True, True, False),
        "webp": FileProcessingCapability("webp", "image", False, False, True, True, False),
        "pdf": FileProcessingCapability("pdf", "document", False, False, False, False, False),
        "xlsx": FileProcessingCapability("xlsx", "spreadsheet", False, False, False, False, False),
        "pptx": FileProcessingCapability("pptx", "presentation", False, False, False, False, False),
    }

    def for_extension(self, extension: str) -> FileProcessingCapability:
        normalized = extension.lower().lstrip(".")
        return self._CAPABILITIES.get(
            normalized,
            FileProcessingCapability(normalized, "unknown", False, False, False, False, False),
        )



# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (File 类型 → 可用能力 单例注册表):
#
#   链路:
#     文件上传时 file_service 调 capability_registry.classify(file_name, mime)
#       → 返回 "docx" / "image" / "audio" / "video" / "code" / "binary"
#     上层(ImageUnderstanding / RequirementParser / WordExport)按这个
#       capability 决定能否用本服务处理这个文件
#
# 关键约束(供开发者速查):
#   - 这是 in-memory singleton;不允许本地状态被持久化;
#   - 新增能力必须先在 registry 中注册,再被 file_understanding_service 消费;
#   - 误分类可能导致图片走文本解析路径(沉默失败),测试覆盖要排查此类用例。
