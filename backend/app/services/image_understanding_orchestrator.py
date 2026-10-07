"""F020 — Image understanding orchestrator.

Centralised pipeline called from :class:`RequirementParserTool`:

  1. Filter all image blocks from the parsed document.
  2. Run OCR concurrently (always-on in F020 — the previous opt-in
     ``extract_images`` switch is removed because OCR text is now
     actually consumed downstream).
  3. Decide whether to invoke the vision model:
       - ``img_config_provider is None``           → OCR only.
       - ``enable_in_doc_parsing == False``        → OCR only.
       - otherwise                                → VisionService.
  4. Insert results back into ``RequirementBlock.index`` order so the
     final prompt preserves document position.

The orchestrator never raises on a vision failure — it records
``vision_error`` on the block and falls back to OCR-only.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.common.document_reader import RequirementBlock
from app.common.ocr_service import ocr_service
from app.services.vision_service import VisionResult, VisionService

logger = logging.getLogger(__name__)


@dataclass
class ImageBlockResult:
    """Per-image outcome keyed by ``RequirementBlock.index``."""

    index: int
    source: str
    image_path: Optional[Path]
    ocr_text: str = ""
    ocr_error: str = ""
    vision: Optional[VisionResult] = None
    vision_error: str = ""

    def to_prompt_text(self) -> str:
        """Render this image's information as a prompt-insertion block."""
        source_label = self.source or "需求文档"

        if self.vision is not None and self.vision.ok:
            return self.vision.to_prompt_text(self.index, source_label)

        if self.ocr_text:
            lines = [
                f"【图片 {self.index}】",
                f"来源位置：{source_label}",
                f"OCR 识别文字：{self.ocr_text}",
            ]
            if self.image_path is not None:
                lines.append(f"本地路径：{self.image_path}")
            if self.vision_error:
                lines.append(f"图片理解失败（已降级为OCR）：{self.vision_error}")
            return "\n".join(lines)

        # No vision and no OCR — surface a clear marker.
        path_str = str(self.image_path) if self.image_path else "未知路径"
        details: list[str] = []
        if self.ocr_error:
            details.append(f"OCR 失败：{self.ocr_error}")
        if self.vision_error:
            details.append(f"图片理解失败：{self.vision_error}")
        body = "；".join(details) if details else "未能识别任何内容"
        return (
            f"【图片 {self.index}】\n"
            f"来源位置：{source_label}\n"
            f"本地路径：{path_str}\n"
            f"处理状态：{body}"
        )


async def process_images(
    blocks: list[RequirementBlock],
    *,
    img_config_provider: Any = None,
    context_llm_invoker: Any | None = None,
    user_id: int | None = None,
    runtime_context: Any | None = None,
) -> dict[int, ImageBlockResult]:
    """Run OCR + optional Vision across all image blocks.

    Parameters
    ----------
    blocks
        The full list of :class:`RequirementBlock` instances produced
        by :class:`DocumentReader`.  Only ``block_type == "image"``
        blocks participate.
    img_config_provider
        :class:`ImageUnderstandingConfigProvider` (or any object with
        ``api_url / api_key / model_name / timeout /
        enable_in_doc_parsing``) or ``None``.  ``None`` → OCR only.

    Returns
    -------
    dict[int, ImageBlockResult]
        Mapping of ``block.index`` → result, populated for every
        image block in the document.
    """
    image_blocks: list[RequirementBlock] = [
        block
        for block in blocks
        if block.block_type == "image" and block.image_path
    ]
    if not image_blocks:
        return {}

    logger.info(
        "ImageUnderstandingOrchestrator: 开始处理 | 图片数=%d | vision=%s",
        len(image_blocks),
        bool(img_config_provider and getattr(img_config_provider, "enable_in_doc_parsing", False)),
    )

    # ── Step 1: serial OCR ───────────────────────────────────────
    # 2026-07-14：原本用 asyncio.gather(*_safe_ocr) 并发，3 张图会
    # 同时触发 OcrService._ensure_ocr()，由于 ``if self._ocr is not
    # None: return`` 不是原子操作，3 个线程都通过了检查并各自 new
    # 了 PaddleOCR 实例 → 6 个线程抢 OMP_NUM_THREADS=2 的 BLAS 锁
    # 互相死等、CPU 187% 持续 30s+ 没跑完。
    # 修复后：OcrService._ensure_ocr 加了双重检查锁保护单实例；此处
    # 仍改为串行（最差情况也是单实例顺序处理，多张图总耗时 ≈
    # init 5-10s + N×1-3s，跟老项目"一张图几秒"的体感一致）。
    ocr_texts: list[str] = []
    for block in image_blocks:
        ocr_texts.append(
            await _safe_ocr(block.image_path)  # type: ignore[arg-type]
        )

    use_vision = bool(
        img_config_provider is not None
        and getattr(img_config_provider, "enable_in_doc_parsing", False)
    )
    vision_service: Optional[VisionService] = None
    if use_vision:
        vision_service = VisionService(
            img_config_provider,
            context_llm_invoker=context_llm_invoker,
            user_id=user_id,
            session_factory=getattr(runtime_context, "session_factory", None),
            conversation_id=getattr(runtime_context, "conversation_internal_id", None),
            runtime_context=runtime_context,
        )

    # ── Step 2: serial Vision (network-bound, but small) ─────────
    # Concurrency here is intentionally bounded: many parallel
    # multimodal calls to a single OpenAI-compatible endpoint can
    # trigger upstream rate limiting.  A serial pass keeps the
    # failure surface small and the logs predictable.
    results: dict[int, ImageBlockResult] = {}
    for block, ocr_text in zip(image_blocks, ocr_texts):
        result = ImageBlockResult(
            index=block.index,
            source=block.source or "需求文档",
            image_path=block.image_path,
            ocr_text=ocr_text,
        )
        if vision_service is not None and block.image_path is not None:
            try:
                vision_result = await vision_service.analyze_image(
                    image_path=block.image_path,
                    source=block.source or "需求文档",
                    ocr_text=ocr_text,
                    image_index=block.index,
                )
                if vision_result.ok:
                    result.vision = vision_result
                else:
                    result.vision_error = vision_result.error
            except Exception as exc:  # noqa: BLE001 — orchestrator must not raise
                logger.warning(
                    "ImageUnderstandingOrchestrator: 视觉分析异常 | "
                    "image_index=%d | err=%s", block.index, exc,
                )
                result.vision_error = str(exc)
        results[block.index] = result

    logger.info(
        "ImageUnderstandingOrchestrator: 处理完成 | 图片数=%d | "
        "vision_success=%d | vision_failed=%d",
        len(results),
        sum(1 for r in results.values() if r.vision is not None and r.vision.ok),
        sum(1 for r in results.values() if r.vision_error),
    )
    return results


async def _safe_ocr(image_path: Path) -> str:
    """Run OCR in a worker thread, returning "" on failure."""
    try:
        return await asyncio.to_thread(ocr_service.extract_text, image_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR 失败 (%s): %s", image_path, exc)
        return ""

# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (F020 Image Understanding 编排):
#
#   链路:
#     文件上传 / RequirementParserTool 解析时:
#       → ImageUnderstandingOrchestrator.understand_one(image_bytes)
#         → VisionService.understand(...)  ← 主要路径
#         → 失败 → ocr_service.process(...)  ← 兜底
#       → 返回 ImageBlockResult(structured caption + tags + ocr_overlay)
#
#   与 Phase 2.9A.20 PHASE-1 batch backfill 配合:
#     attachment_backfill_service 调同样的 orchestrator
#
# 关键约束(供开发者速查):
#   - 单图超时 30s,失败仅 LOG,不阻塞上层;
#   - Vision 不通就 OCR,OCR 不通就标"未能识别"(无第四层兜底);
#   - ImageBlockResult 走 allowed_internal_fields 过滤后再给前端;
#   - 不在 orchestrator 内做 cache(Image Block 识别结果由上下文层决定缓存)。
