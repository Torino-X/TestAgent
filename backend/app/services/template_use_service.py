"""Materialize a saved template as an ordinary conversation attachment."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    TemplateConversationNotOwnedError,
    TemplateNotFoundError,
    TemplateNotOwnedError,
    TemplateUseCopyFailedError,
    TemplateVersionMissingError,
)
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.template_repository import TemplateRepository
from app.repositories.template_version_repository import TemplateVersionRepository
from app.repositories.user_template_repository import UserTemplateRepository
from app.services.conversation_service import ConversationService
from app.services.file_service import FileService
from app.storage.oss_storage import object_storage
from app.utils.datetime import utcnow

logger = logging.getLogger(__name__)


class TemplateUseService:
    def __init__(self, session: AsyncSession, *, storage=None) -> None:
        self._session = session
        self._storage = storage or object_storage
        self._user_templates = UserTemplateRepository(session)
        self._templates = TemplateRepository(session)
        self._versions = TemplateVersionRepository(session)
        self._conversations = ConversationRepository(session)

    async def use(
        self,
        *,
        user_id: int,
        user_template_public_id: str,
        conversation_public_id: str | None = None,
    ) -> dict:
        owned = await self._user_templates.get_owned(user_template_public_id, user_id)
        if owned is None:
            raise TemplateNotOwnedError()
        version = await self._versions.get_active(owned.user_template.version_id)
        if version is None:
            raise TemplateVersionMissingError()

        result = await self._materialize_version(
            user_id=user_id,
            conversation_public_id=conversation_public_id,
            version=version,
            template_public_id=owned.asset.public_id,
        )
        await self._user_templates.touch_last_used(owned.user_template.id, utcnow())
        return result

    async def use_public_default_test_plan_template(
        self,
        *,
        user_id: int,
        conversation_public_id: str,
        template_name: str,
    ) -> dict:
        """Materialize the configured public test-plan template for a task.

        A default is a platform policy, not a user library save.  It therefore
        copies the current public template version into the user's conversation
        but does not create a ``UserTemplate`` row or increment marketplace
        save counts.
        """
        asset = await self._templates.get_active_public_by_name(
            name=template_name,
            category_code="test_plan",
        )
        if asset is None or asset.current_version_id is None:
            raise TemplateNotFoundError()
        version = await self._versions.get_active(asset.current_version_id)
        if version is None:
            raise TemplateVersionMissingError()
        return await self._materialize_version(
            user_id=user_id,
            conversation_public_id=conversation_public_id,
            version=version,
            template_public_id=asset.public_id,
        )

    async def _materialize_version(
        self,
        *,
        user_id: int,
        conversation_public_id: str | None,
        version,
        template_public_id: str,
    ) -> dict:
        if conversation_public_id:
            conversation = await self._conversations.get_owned_by_public_id(
                conversation_public_id, user_id
            )
            if conversation is None:
                raise TemplateConversationNotOwnedError()
            conversation_summary = ConversationService._to_summary(conversation)
        else:
            conversation_summary = await ConversationService(self._session).create(
                "新会话", user_id
            )
            conversation_public_id = conversation_summary["id"]

        try:
            content = await self._storage.read_bytes(version.storage_path)
            file_service = FileService(self._session, storage=self._storage)
            uploaded = await file_service.upload(
                content=content,
                file_name=version.original_filename,
                user_internal_id=user_id,
                conv_public_id=conversation_public_id,
                content_type=version.mime_type,
            )
            # The platform knows this copied asset is the output template;
            # do not make the attachment resolver wait for filename/LLM
            # classification before a test-plan task can start.
            await file_service.confirm_type(
                str(uploaded["id"]),
                "test_plan_template",
                user_internal_id=user_id,
            )
        except (TemplateConversationNotOwnedError, TemplateVersionMissingError):
            raise
        except Exception as exc:
            logger.warning(
                "template use copy failed | template=%s | user_id=%s | error=%s",
                template_public_id,
                user_id,
                type(exc).__name__,
            )
            raise TemplateUseCopyFailedError() from exc

        uploaded["file_id"] = uploaded["id"]
        uploaded["file_name"] = uploaded["original_name"]
        logger.info(
            "template.used | template=%s | user_id=%s | file_ext=%s",
            template_public_id,
            user_id,
            version.file_ext,
        )
        return {
            "conversation": {
                **conversation_summary,
                "conversation_id": conversation_summary["id"],
            },
            "uploaded_file": uploaded,
        }
