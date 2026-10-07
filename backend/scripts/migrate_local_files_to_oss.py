r"""Copy legacy upload/artifact bodies to OSS and update their DB metadata.

Run only after configuring OSS_* values in ``backend/.env``:

    .\.venv\Scripts\python.exe scripts\migrate_local_files_to_oss.py
    .\.venv\Scripts\python.exe scripts\migrate_local_files_to_oss.py --delete-local

The first command is non-destructive: it verifies the database metadata can be
updated while leaving local copies available for rollback.  Use
``--delete-local`` only after validating download and preview behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.artifact import Artifact
from app.models.uploaded_file import UploadedFile
from app.storage.local_storage import local_storage
from app.storage.oss_storage import object_storage


def _legacy_path(storage_path: str) -> Path:
    path = Path(storage_path)
    if not storage_path or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe legacy storage path: {storage_path!r}")
    return local_storage._base / path  # noqa: SLF001 - controlled migration input


async def _migrate(*, delete_local: bool) -> tuple[int, int, int]:
    uploads = artifacts = skipped = 0
    delete_after_commit: list[Path] = []
    async with AsyncSessionLocal() as session:
        upload_rows = list(
            (await session.execute(select(UploadedFile).where(UploadedFile.storage_type != "oss")))
            .scalars()
            .all()
        )
        artifact_rows = list(
            (await session.execute(select(Artifact).where(Artifact.storage_type != "oss")))
            .scalars()
            .all()
        )

        for row in upload_rows:
            source = _legacy_path(row.storage_path)
            if not source.is_file():
                print(f"SKIP missing upload {row.public_id}: {source}")
                skipped += 1
                continue
            content = source.read_bytes()
            stored = await object_storage.save_upload(
                content,
                row.original_name or row.stored_name or "file",
                str(row.user_id),
                str(row.conversation_id or "library"),
            )
            row.storage_type = "oss"
            row.storage_path = stored.storage_path
            row.file_size = stored.file_size
            uploads += 1
            if delete_local:
                delete_after_commit.append(source)

        for row in artifact_rows:
            source = _legacy_path(row.storage_path)
            if not source.is_file():
                print(f"SKIP missing artifact {row.public_id}: {source}")
                skipped += 1
                continue
            content = source.read_bytes()
            stored = await object_storage.save_artifact(
                content,
                row.file_name or "artifact",
                str(row.user_id),
                str(row.task_id or "unknown"),
            )
            row.storage_type = "oss"
            row.storage_path = stored.storage_path
            row.file_size = stored.file_size
            artifacts += 1
            if delete_local:
                delete_after_commit.append(source)

        await session.commit()
    for source in delete_after_commit:
        os.unlink(source)
    return uploads, artifacts, skipped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete-local", action="store_true")
    args = parser.parse_args()
    uploads, artifacts, skipped = asyncio.run(_migrate(delete_local=args.delete_local))
    print(f"migrated uploads={uploads} artifacts={artifacts} skipped={skipped}")


if __name__ == "__main__":
    main()
