"""Artifact repository.

Phase 2.8R-E: 加 ``create_or_get_by_idempotency_key`` 原子操作,
双进程并发写同一 idempotency_key 时第一行成功,其余返回原行。
"""

from __future__ import annotations

import json as _json

from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.core.exceptions import ArtifactIdempotencyConflictError
from app.models.artifact import Artifact
from app.repositories.base import BaseRepository


class ArtifactRepository(BaseRepository[Artifact]):
    model = Artifact

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_by_task(self, user_id: int, task_internal_id: int) -> list[Artifact]:
        result = await self.session.execute(
            select(Artifact).where(
                Artifact.user_id == user_id,
                Artifact.task_id == task_internal_id,
                Artifact.deleted_at.is_(None),
            ).order_by(Artifact.created_at.desc())
        )
        return list(result.scalars().all())

    async def list_by_user(self, user_id: int, limit: int = 200) -> list[Artifact]:
        """Return downloadable generated artifacts without exposing storage metadata."""
        result = await self.session.execute(
            select(Artifact).options(
                load_only(
                    Artifact.id,
                    Artifact.public_id,
                    Artifact.file_name,
                    Artifact.mime_type,
                    Artifact.file_ext,
                    Artifact.file_size,
                    Artifact.artifact_type,
                    Artifact.created_at,
                    Artifact.updated_at,
                    Artifact.deleted_at,
                )
            )
            .where(
                Artifact.user_id == user_id,
                Artifact.status == "available",
                Artifact.deleted_at.is_(None),
            )
            .order_by(Artifact.updated_at.desc(), Artifact.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_by_project(self, user_id: int, project_id: int, limit: int = 200) -> list[Artifact]:
        # Keep the ORDER BY working set narrow. Artifact rows contain JSON and a
        # long storage path; asking MySQL to filesort the complete ORM row can
        # exhaust a constrained sort buffer before LIMIT is applied. Fetch the
        # ordered primary keys first, then hydrate only those rows.
        id_result = await self.session.execute(
            select(Artifact.id)
            .where(
                Artifact.user_id == user_id,
                Artifact.project_id == project_id,
                Artifact.status == "available",
                Artifact.deleted_at.is_(None),
            )
            .order_by(Artifact.updated_at.desc(), Artifact.id.desc())
            .limit(limit)
        )
        ordered_ids = list(id_result.scalars().all())
        if not ordered_ids:
            return []

        result = await self.session.execute(
            select(Artifact).where(Artifact.id.in_(ordered_ids))
        )
        artifacts_by_id = {artifact.id: artifact for artifact in result.scalars().all()}
        return [artifacts_by_id[artifact_id] for artifact_id in ordered_ids if artifact_id in artifacts_by_id]

    async def list_deleted_by_user(self, user_id: int, limit: int = 200) -> list[Artifact]:
        result = await self.session.execute(
            select(Artifact).options(
                load_only(
                    Artifact.id,
                    Artifact.public_id,
                    Artifact.file_name,
                    Artifact.mime_type,
                    Artifact.file_ext,
                    Artifact.file_size,
                    Artifact.artifact_type,
                    Artifact.created_at,
                    Artifact.updated_at,
                    Artifact.deleted_at,
                )
            )
            .where(Artifact.user_id == user_id, Artifact.deleted_at.is_not(None))
            .order_by(Artifact.deleted_at.desc(), Artifact.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_latest_available_by_conversation(
        self,
        user_id: int,
        conversation_id: int,
        *,
        artifact_type: str | None = "test_plan_word",
    ) -> Artifact | None:
        stmt = select(Artifact).where(
            Artifact.user_id == user_id,
            Artifact.conversation_id == conversation_id,
            Artifact.status == "available",
            Artifact.deleted_at.is_(None),
        )
        if artifact_type:
            stmt = stmt.where(Artifact.artifact_type == artifact_type)
        result = await self.session.execute(
            stmt.order_by(Artifact.created_at.desc(), Artifact.id.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def get_by_public_id(self, public_id: str) -> Artifact | None:
        result = await self.session.execute(
            select(Artifact).where(
                Artifact.public_id == public_id, Artifact.deleted_at.is_(None)
            )
        )
        return result.scalar_one_or_none()

    async def get_any_by_public_id(self, public_id: str) -> Artifact | None:
        result = await self.session.execute(select(Artifact).where(Artifact.public_id == public_id))
        return result.scalar_one_or_none()

    async def rename(self, public_id: str, name: str, now: object) -> None:
        await self.session.execute(
            update(Artifact)
            .where(Artifact.public_id == public_id)
            .values(file_name=name, updated_at=now)
        )

    async def soft_delete(self, public_id: str, now: object) -> None:
        await self.session.execute(
            update(Artifact)
            .where(Artifact.public_id == public_id)
            .values(deleted_at=now)
        )

    async def restore(self, public_id: str, now: object) -> None:
        await self.session.execute(
            update(Artifact)
            .where(Artifact.public_id == public_id)
            .values(deleted_at=None, updated_at=now)
        )

    async def hard_delete(self, public_id: str) -> None:
        await self.session.execute(delete(Artifact).where(Artifact.public_id == public_id))

    async def get_by_idempotency_key(self, idempotency_key: str) -> Artifact | None:
        """Phase 2.8R-E:按 idempotency_key UNIQUE lookup,用于 create_or_get 路径。"""
        result = await self.session.execute(
            select(Artifact).where(
                Artifact.idempotency_key == idempotency_key,
                Artifact.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get_by_idempotency_key(
        self,
        art: Artifact,
        *,
        idempotency_key: str,
    ) -> tuple[Artifact, bool]:
        """幂等创建 — 双进程并发安全。

        行为:
          1. 优先 INSERT 新行(若 idempotency_key 唯一)
          2. UNIQUE 冲突 → 重新 SELECT 已有行,**返回原行 + created=False**
          3. 双进程都争同一 key:1 行成功 + 1 行 return original

        Returns:
            (Artifact, created) — Artifact 必然 non-None;created 表新建/已存在。
        """
        art.idempotency_key = idempotency_key
        try:
            await self.create(art)
            await self.session.commit()
            return art, True
        except IntegrityError as exc:
            await self.session.rollback()
            existing = await self.get_by_idempotency_key(idempotency_key)
            if existing is None:
                # 极少见:UNIQUE 触发了,但 lookup 失败 → 抛错
                raise ArtifactIdempotencyConflictError(
                    detail={
                        "reason": "uniqueness_conflict_but_lookup_failed",
                        "idempotency_key": idempotency_key[:64],
                        "error": str(exc),
                    },
                ) from exc
            return existing, False

    async def create(self, art: Artifact) -> Artifact:
        # aiomysql driver chokes on dict params for JSON columns —
        # use raw INSERT with explicit json.dumps.
        import json as _json
        from sqlalchemy import text

        meta_str = None
        if art.metadata_json is not None:
            if isinstance(art.metadata_json, str):
                meta_str = art.metadata_json
            else:
                meta_str = _json.dumps(art.metadata_json, ensure_ascii=False, default=str)

        result = await self.session.execute(
            text(
                "INSERT INTO artifacts "
                "(public_id, user_id, conversation_id, task_id, project_id, artifact_type, "
                " file_name, file_ext, mime_type, file_size, file_hash, "
                " storage_type, storage_path, status, version_no, "
                " source_artifact_id, metadata_json, "
                " idempotency_key, input_hash, graph_run_id, graph_version, "
                " created_at, updated_at) "
                "VALUES "
                "(:pid, :uid, :cid, :tid, :prid, :at, :fn, :fe, :mt, :fs, :fh, "
                " :st, :sp, :s, :vn, :sa, :mj, "
                " :ik, :ih, :gri, :gv, "
                " :ca, :ua)"
            ),
            {
                "pid": art.public_id,
                "uid": art.user_id,
                "cid": art.conversation_id,
                "tid": art.task_id,
                "prid": getattr(art, "project_id", None),
                "at": art.artifact_type,
                "fn": art.file_name,
                "fe": art.file_ext,
                "mt": art.mime_type,
                "fs": art.file_size,
                "fh": art.file_hash,
                "st": art.storage_type,
                "sp": art.storage_path,
                "s": art.status,
                "vn": art.version_no,
                "sa": art.source_artifact_id,
                "mj": meta_str,
                "ik": art.idempotency_key,
                "ih": art.input_hash,
                "gri": art.graph_run_id,
                "gv": art.graph_version,
                "ca": art.created_at,
                "ua": art.updated_at,
            },
        )
        await self.session.flush()
        # Populate PK after raw INSERT (ORM doesn't auto-populate)
        id_result = await self.session.execute(text("SELECT LAST_INSERT_ID()"))
        art.id = id_result.scalar()
        return art


# 模块定位:Artifact 仓储(产物元数据)
#
# 链路:
#   WordExportTool 成功 → atomic 写盘 + upsert Artifact 行
#   api/v1/artifacts/{public_id} download → 读 public_id → ACL 校验 → open
#
# 关键约束:
#   - storage_path **不**对外暴露;
#   - idempotency_key UNIQUE(双进程并发导出由调用方重新 lookup);
#   - 删除要走 hard delete(同时调 StorageHelper 删 .docx)。
