from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.context_engine.payload.payload_storage import OSSPayloadBackend
from app.services.artifact_writer import ArtifactWriter
from app.services.file_service import FileService
from app.storage.oss_storage import OSSFileStorageService


class _FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, key: str, content: bytes):
        self.objects[key] = content
        return SimpleNamespace(status=200)

    def get_object(self, key: str):
        if key not in self.objects:
            raise FileNotFoundError(key)
        return SimpleNamespace(read=lambda: self.objects[key])

    def object_exists(self, key: str) -> bool:
        return key in self.objects

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)

    def list_objects(self, prefix: str = "", marker: str | None = None, max_keys: int = 1_000):
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        if marker:
            keys = [k for k in keys if k > marker]
        page = keys[:max_keys]
        return SimpleNamespace(
            object_list=[SimpleNamespace(key=k) for k in page],
            next_marker=page[-1] if len(keys) > max_keys else None,
        )


@pytest.mark.asyncio
async def test_oss_storage_keeps_durable_file_bytes_out_of_the_application_data_dir():
    bucket = _FakeBucket()
    storage = OSSFileStorageService(bucket_factory=lambda: bucket)

    stored = await storage.save_upload(b"hello", "requirements.txt", "7", "library")

    assert stored.storage_path.startswith("testagent/uploads/7/library/")
    assert await storage.read_bytes(stored.storage_path) == b"hello"
    assert await storage.exists(stored.storage_path) is True

    async with storage.stage_file(stored.storage_path, suffix=".txt") as staged:
        assert staged.read_bytes() == b"hello"
        assert staged.exists()
    assert not staged.exists()

    await storage.delete_file(stored.storage_path)
    assert await storage.exists(stored.storage_path) is False


@pytest.mark.asyncio
async def test_file_service_delete_clears_oss_object(monkeypatch):
    bucket = _FakeBucket()
    bucket.objects["testagent/uploads/7/library/abc.txt"] = b"body"
    storage = OSSFileStorageService(bucket_factory=lambda: bucket)
    monkeypatch.setattr("app.services.file_service.object_storage", storage)
    index_repo_instance = MagicMock()
    index_repo_instance.invalidate_uploaded_file = AsyncMock()
    monkeypatch.setattr(
        "app.services.file_service.ContextIndexDocumentRepository",
        MagicMock(return_value=index_repo_instance),
    )

    service = FileService.__new__(FileService)
    service._session = MagicMock(commit=AsyncMock())
    uploaded = SimpleNamespace(
        public_id="file_abc",
        user_id=7,
        conversation_id=42,
        storage_path="testagent/uploads/7/library/abc.txt",
        deleted_at=None,
    )
    service._file_repo = MagicMock(
        get_any_by_public_id=AsyncMock(return_value=uploaded),
        soft_delete=AsyncMock(),
    )
    service._conv_repo = MagicMock()

    await service.delete("file_abc", user_internal_id=7)

    service._file_repo.soft_delete.assert_awaited_once()
    index_repo_instance.invalidate_uploaded_file.assert_awaited_once()
    assert "testagent/uploads/7/library/abc.txt" not in bucket.objects


@pytest.mark.asyncio
async def test_artifact_writer_cleans_oss_object_on_idempotency_collision(monkeypatch):
    bucket = _FakeBucket()
    storage = OSSFileStorageService(bucket_factory=lambda: bucket)
    monkeypatch.setattr("app.services.artifact_writer.object_storage", storage)

    writer = ArtifactWriter.__new__(ArtifactWriter)
    writer._session = MagicMock()
    writer._art_repo = MagicMock()
    writer._art_repo.get_by_idempotency_key = AsyncMock(return_value=None)
    existing_row = SimpleNamespace(public_id="artifact_existing", storage_path="ignored")
    writer._art_repo.create_or_get_by_idempotency_key = AsyncMock(
        return_value=(existing_row, False)
    )

    await writer.write_and_record(
        user_internal_id=7,
        conversation_internal_id=42,
        task_internal_id=9,
        task_public_id="task_9",
        artifact_type="test_plan_word",
        file_name="plan.docx",
        file_ext=".docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_content=b"docx-bytes",
    )

    # The orphan upload must be removed once we know the row already exists.
    assert bucket.objects == {}


def test_payload_oss_backend_iterate_orphans_walks_prefix(monkeypatch):
    from app.storage import oss_storage

    bucket = _FakeBucket()
    bucket.objects["testagent/context-payloads/7/key-a"] = b"a"
    bucket.objects["testagent/context-payloads/7/key-b"] = b"b"
    bucket.objects["testagent/uploads/7/library/foo.txt"] = b"x"  # not under prefix
    storage = OSSFileStorageService(bucket_factory=lambda: bucket)
    monkeypatch.setattr(oss_storage, "object_storage", storage)

    orphans = OSSPayloadBackend().iterate_orphans()

    assert set(orphans) == {(7, "key-a"), (7, "key-b")}


@pytest.mark.asyncio
async def test_docx_format_check_stages_oss_keys_for_artifact_and_template(monkeypatch):
    """OSS 化后 artifact / template 的 storage_path 都是 OSS key。

    DocxFormatCheckTool 应当:
      * 自动 stage 这两个 key 为本地临时文件;
      * 比较完成后清理临时文件(通过 ExitStack);
      * 成功时不抛 FileNotFoundError。
    """
    from contextlib import ExitStack
    from app.tools.docx_format_check_tool import DocxFormatCheckTool

    bucket = _FakeBucket()
    artifact_key = "testagent/artifacts/1/236/art_6070bb13.docx"
    template_key = "testagent/uploads/1/library/00_PlanWise.docx"
    bucket.objects[artifact_key] = b"artifact-bytes"
    bucket.objects[template_key] = b"template-bytes"
    storage = OSSFileStorageService(bucket_factory=lambda: bucket)
    # tool 内部对 ``from app.storage.oss_storage import object_storage`` 做延迟导入;
    # 改写源模块即可让 stub 生效。
    from app.storage import oss_storage
    monkeypatch.setattr(oss_storage, "object_storage", storage)

    # 比较器走真实 python-docx 解析,这里 stub 一个空 FormatReport。
    def fake_compare(template_path, output_path):
        from app.common.format_checker import FormatReport, HeadingCounts

        return FormatReport(
            headings=HeadingCounts(h1=0, h2=0, h3=0, other=0),
            table_count=0,
            table_column_counts=(),
            bookmark_names=(),
            field_count=0,
            hyperlink_count=0,
            header_text_count=0,
            footer_text_count=0,
            drifts=(),
        )

    monkeypatch.setattr(
        "app.tools.docx_format_check_tool.compare_docx_to_template",
        fake_compare,
    )

    # inputs 走 OSS key,_absolutise 应该把 key 转为本地绝对路径
    inputs = {
        "artifact_path": artifact_key,
        "template_path": template_key,
        "artifact": {"public_id": "art_6070bb13", "artifact_type": "test_plan_word"},
    }
    tool = DocxFormatCheckTool()
    result = await tool.run(inputs, context=SimpleNamespace())

    assert result["success"] is True, result
    assert result["data"]["checked_artifact_public_id"] == "art_6070bb13"
    # key 应当原样回写,不要泄露 staging 临时路径
    assert result["data"]["artifact_path"] == artifact_key
    assert result["data"]["template_path"] == template_key

    # ExitStack 已经在 run() 内部关闭,临时文件应当被清理
    import os as _os
    from glob import glob
    leftovers = glob(_os.path.join(_os.environ.get("TEMP", "/tmp"), "docx_check_*.docx"))
    assert leftovers == []


@pytest.mark.asyncio
async def test_docx_format_check_reports_missing_template(monkeypatch):
    """当 template 的 OSS key 不存在,必须返回 FORMAT_TEMPLATE_FILE_NOT_FOUND。

    这条路径是上一版本 OSS 化后任务卡死的根因。
    """
    from app.tools.docx_format_check_tool import (
        FORMAT_TEMPLATE_FILE_NOT_FOUND,
        DocxFormatCheckTool,
    )

    bucket = _FakeBucket()
    artifact_key = "testagent/artifacts/1/236/art_present.docx"
    template_key = "testagent/uploads/1/library/missing.docx"
    bucket.objects[artifact_key] = b"artifact-bytes"
    # template key 故意不放进 bucket
    storage = OSSFileStorageService(bucket_factory=lambda: bucket)
    from app.storage import oss_storage
    monkeypatch.setattr(oss_storage, "object_storage", storage)

    inputs = {
        "artifact_path": artifact_key,
        "template_path": template_key,
        "artifact": {"public_id": "art_present", "artifact_type": "test_plan_word"},
    }
    tool = DocxFormatCheckTool()
    result = await tool.run(inputs, context=SimpleNamespace())

    assert result["success"] is False
    assert result["error"]["code"] == FORMAT_TEMPLATE_FILE_NOT_FOUND
    assert "template" in result["error"]["message"].lower()
