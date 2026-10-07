"""Lazy image understanding for the current chat message.

Images are interpreted only for the current message/task turn.  This
service does not classify images into business attachment roles and does
not create long-term RAG entries for them.
"""

from __future__ import annotations

import inspect
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.file_capability_registry import FileProcessingCapabilityRegistry
from app.services.vision_service import VisionService
from app.storage.oss_storage import object_storage

logger = logging.getLogger(__name__)


IMAGE_CHAT_SYSTEM_PROMPT = (
    "You are a helpful multimodal assistant. Answer the user's question based on "
    "the current image attachment(s). Do not assign business file roles such as "
    "requirement, template, or reference."
)


@dataclass(frozen=True)
class CurrentMessageImageVisionResult:
    handled: bool
    reply: str = ""
    error_code: str | None = None


class CurrentMessageImageVisionService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        main_llm_client: Any | None = None,
        main_model_supports_vision: bool = False,
        context_llm_invoker: Any | None = None,
        session_factory: Any | None = None,
        chat_service: Any | None = None,
        vision_service_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._session = session
        self._main_llm_client = main_llm_client
        self._main_model_supports_vision = main_model_supports_vision
        self._context_llm_invoker = context_llm_invoker
        self._session_factory = session_factory
        self._chat_service = chat_service
        self._vision_service_factory = vision_service_factory
        self._capabilities = FileProcessingCapabilityRegistry()

    async def generate_reply(
        self,
        *,
        user_content: str,
        user_id: int,
        files: list,
        attached_id_set: set[str] | None,
        chat_context: object | None = None,
    ) -> CurrentMessageImageVisionResult:
        attached_files = self._attached_files(files, attached_id_set)
        image_files = [
            file for file in attached_files
            if self._capabilities.for_extension(getattr(file, "file_ext", "")).can_vision
        ]
        if not image_files:
            if attached_files and any(_looks_like_image(file) for file in attached_files):
                return CurrentMessageImageVisionResult(
                    handled=False,
                    error_code="image.unsupported_type",
                )
            return CurrentMessageImageVisionResult(handled=False)

        staging = AsyncExitStack()
        image_paths: list[str] = []
        try:
            for file in image_files:
                if getattr(file, "storage_type", "local") == "oss":
                    suffix = Path(getattr(file, "original_name", "")).suffix
                    path = await staging.enter_async_context(
                        object_storage.stage_file(file.storage_path, suffix=suffix)
                    )
                    image_paths.append(str(path))
                else:
                    image_paths.append(str(Path(file.storage_path)))
        except FileNotFoundError:
            await staging.aclose()
            return CurrentMessageImageVisionResult(
                handled=True,
                reply="The image attachment was uploaded, but its stored file is not available for image understanding.",
                error_code="image.file_missing",
            )

        missing = [path for path in image_paths if not Path(path).exists()]
        if missing:
            await staging.aclose()
            return CurrentMessageImageVisionResult(
                handled=True,
                reply="The image attachment was uploaded, but its stored file is not available for image understanding.",
                error_code="image.file_missing",
            )

        try:
            bridge = self._context_llm_invoker
            if (
                self._main_model_supports_vision
                and self._main_llm_client is not None
                and bridge is not None
                and getattr(bridge, "available", False)
            ):
                from app.llm.task_profiles import CHAT_PROFILE
                from types import SimpleNamespace

                profile = CHAT_PROFILE.model_copy(update={
                    "name": "chat.image_reply",
                    "system_prompt": IMAGE_CHAT_SYSTEM_PROMPT,
                })
                runtime_context = SimpleNamespace(
                    session_factory=self._session_factory,
                    llm_client=self._main_llm_client,
                    user_internal_id=user_id,
                    conversation_internal_id=getattr(chat_context, "conversation_internal_id", None),
                )
                result = await bridge.generate(
                    user_id=user_id,
                    conversation_id=getattr(chat_context, "conversation_internal_id", None),
                    call_site="chat.image_reply",
                    llm_task_profile=profile,
                    current_goal=user_content,
                    user_content=user_content,
                    output_contract="text",
                    image_paths=image_paths,
                    runtime_context=runtime_context,
                )
                reply = getattr(result, "value", None) if result is not None else None
                if not isinstance(reply, str) or not reply.strip():
                    raise RuntimeError("context_engine_empty_image_reply")
                await staging.aclose()
                return CurrentMessageImageVisionResult(
                    handled=True,
                    reply=str(reply).strip(),
                )
        except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "current message main-model vision failed | err_type=%s | err=%s",
                    type(exc).__name__,
                    str(exc)[:200],
                )

        vision_service = await self._build_vision_service(user_id)
        if vision_service is None:
            await staging.aclose()
            return CurrentMessageImageVisionResult(
                handled=True,
                reply="Image understanding is not configured for this account yet, so I cannot inspect the image attachment.",
                error_code="image.vision_not_configured",
            )

        blocks: list[str] = []
        for index, (file, image_path) in enumerate(zip(image_files, image_paths), start=1):
            result = await vision_service.analyze_image(
                Path(image_path),
                getattr(file, "original_name", "") or getattr(file, "public_id", ""),
                "",
                index,
            )
            if getattr(result, "ok", False):
                blocks.append(result.to_prompt_text(index, getattr(file, "original_name", "")))
            else:
                blocks.append(
                    f"Image {index} understanding failed: {getattr(result, 'error', 'unknown_error')}"
                )

        vision_context = "\n\n".join(blocks).strip()
        if not vision_context:
            await staging.aclose()
            return CurrentMessageImageVisionResult(
                handled=True,
                reply="The image attachment could not be understood.",
                error_code="image.vision_failed",
            )
        prompt = (
            f"User question:\n{user_content}\n\n"
            f"Current image understanding:\n{vision_context}"
        )
        if self._chat_service is None:
            await staging.aclose()
            return CurrentMessageImageVisionResult(handled=True, reply=prompt)
        try:
            reply = await self._chat_service.generate_reply(
                prompt,
                chat_context=chat_context,
                user_id=user_id,
            )
            return CurrentMessageImageVisionResult(handled=True, reply=str(reply).strip())
        finally:
            await staging.aclose()

    def _attached_files(self, files: list, attached_id_set: set[str] | None) -> list:
        if not attached_id_set:
            return list(files)
        return [
            file for file in files
            if getattr(file, "public_id", None) in attached_id_set
        ]

    async def _build_vision_service(self, user_id: int):
        if self._vision_service_factory is not None:
            signature = inspect.signature(self._vision_service_factory)
            if len(signature.parameters) == 0:
                return self._vision_service_factory()
        from app.services.settings_service import SettingsService

        provider = await SettingsService(self._session).build_image_understanding_provider(user_id)
        if provider is None:
            return None
        if self._vision_service_factory is not None:
            return self._vision_service_factory(provider)
        return VisionService(
            provider,
            context_llm_invoker=self._context_llm_invoker,
            user_id=user_id,
            session_factory=self._session_factory,
        )


def _looks_like_image(file) -> bool:
    extension = str(getattr(file, "file_ext", "") or "").lower().lstrip(".")
    mime_type = str(getattr(file, "mime_type", "") or "").lower()
    return extension in {"gif", "bmp", "tif", "tiff", "heic"} or mime_type.startswith("image/")


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (当前消息图片的 Vision 即时识别):
#
#   链路 (与 message_regeneration / chat reply 配合):
#     用户上传图片(随当前消息):
#       → ChatLLMService.generate_reply 拿到 chat_context
#         → MessageService 在落库 message 前:
#           → CurrentMessageImageVisionService.understand_for_message(
#               message_id, image_bytes)
#             → VisionService.understand(...) 即时调用
#             → 把结果挂到 message.metadata.image_understanding
#             → 下次 chat turn 用作 memory
#
# 关键约束(供开发者速查):
#   - 与文件上传阶段的 ImageUnderstandingOrchestrator 不同:
#       * 文件上传: 异步持久化到 attachment_understanding 表;
#       * 当前消息图片: 即时识别 + 挂 message.metadata,不入 attachment_understanding;
#   - 上限:当前消息最多 4 张图片,超过按顺序只识别前 4 张;
#   - 失败仅 metadata 留空,不允许 message 写入失败连带回滚。
