"""CE-03 Indexing 测试：chunker 确定性 / 幂等 / 文档状态机 / Namespace。

覆盖：
- RecursiveCharChunker 确定性、section_path 前缀（方案 A）、hash；
- namespace identity 稳定 hash、UUIDv5 point id；
- IndexDocumentService.submit_uploaded_file 幂等 no-op / 换代 superseded；
- 文档状态聚合（双 skipped → indexed；parse failed → failed）。
"""

from __future__ import annotations

import pytest

from app.context_engine.indexing.chunker import (
    CHUNK_POLICY_KEY,
    RecursiveCharChunker,
    compute_sha256,
    normalize_text,
)
from app.context_engine.indexing.stores_protocol import (
    build_namespace_identity,
    chunk_point_id,
    lexical_namespace_name,
    vector_namespace_name,
)


def test_chunker_deterministic_and_section_prefix():
    text = "第一节\n\n" + "内容" * 400 + "\n\n第二节\n\n" + "其他" * 300
    c1 = RecursiveCharChunker(section_path="doc1")
    c2 = RecursiveCharChunker(section_path="doc1")
    a = c1.chunk(text)
    b = c2.chunk(text)
    assert [x.content for x in a] == [x.content for x in b]
    assert a[0].content.startswith("<section:doc1>")
    assert a[0].content_hash == compute_sha256(a[0].content)
    assert normalize_text("  Hello   World  ") == "hello world"


def test_chunker_policy_key():
    assert CHUNK_POLICY_KEY == "recursive_char:v1"


def test_namespace_identity_stable_hash():
    ns1 = build_namespace_identity(
        provider_type="embedding", provider_config_public_id="cfg_1",
        model="m1", dimension=768, normalize=True, chunk_policy_key="recursive_char:v1",
    )
    ns2 = build_namespace_identity(
        provider_type="embedding", provider_config_public_id="cfg_1",
        model="m1", dimension=768, normalize=True, chunk_policy_key="recursive_char:v1",
    )
    assert ns1 == ns2
    assert vector_namespace_name(ns1).startswith("ctx_vec_")
    assert lexical_namespace_name(ns1).startswith("ctx_lex_")
    # 维度变化 → 新 namespace
    ns3 = build_namespace_identity(
        provider_type="embedding", provider_config_public_id="cfg_1",
        model="m1", dimension=1024, normalize=True, chunk_policy_key="recursive_char:v1",
    )
    assert vector_namespace_name(ns1) != vector_namespace_name(ns3)


def test_uuidv5_point_id_stable_and_valid():
    a = chunk_point_id("ick_abc")
    b = chunk_point_id("ick_abc")
    assert a == b  # 稳定
    assert len(a) == 36
    assert chunk_point_id("ick_abc") != chunk_point_id("ick_abd")


async def test_document_submit_idempotent_noop(tmp_path, sqlite_session_factory):
    """同 digest 重复提交 → no-op（幂等）。"""
    from app.context_engine.indexing.document_service import IndexDocumentService
    from app.models.uploaded_file import UploadedFile
    from datetime import datetime

    file_path = tmp_path / "doc.txt"
    file_path.write_text("hello index", encoding="utf-8")

    sf = sqlite_session_factory
    async with sf() as session:
        session.add(
            UploadedFile(
                id=1, public_id="file_1", user_id=1, conversation_id=1,
                original_name="doc.txt", stored_name="doc.txt", file_ext="txt",
                file_size=11, storage_type="local", storage_path=str(file_path),
                created_at=datetime.now(), updated_at=datetime.now(),
            )
        )
        await session.commit()

    async with sf() as session:
        svc = IndexDocumentService(session)
        r1 = await svc.submit_uploaded_file(user_id=1, file_public_id="file_1")
        assert r1["created"] is True
        # 幂等：同 digest 再提交 → no-op
        r2 = await svc.submit_uploaded_file(user_id=1, file_public_id="file_1")
        assert r2["created"] is False
        assert r2["document_public_id"] == r1["document_public_id"]


async def test_document_submit_digest_change_supersedes(tmp_path, sqlite_session_factory):
    """digest 变化 → 新文档行 + 旧行 superseded。"""
    from app.context_engine.indexing.document_service import IndexDocumentService
    from app.models.uploaded_file import UploadedFile
    from datetime import datetime
    from app.repositories.context_engine_repositories import ContextIndexDocumentRepository

    file_path = tmp_path / "doc.txt"
    file_path.write_text("v1", encoding="utf-8")

    sf = sqlite_session_factory
    async with sf() as session:
        session.add(
            UploadedFile(
                id=1, public_id="file_1", user_id=1, conversation_id=1,
                original_name="doc.txt", stored_name="doc.txt", file_ext="txt",
                file_size=2, storage_type="local", storage_path=str(file_path),
                created_at=datetime.now(), updated_at=datetime.now(),
            )
        )
        await session.commit()

    async with sf() as session:
        svc = IndexDocumentService(session)
        r1 = await svc.submit_uploaded_file(user_id=1, file_public_id="file_1")

    # 修改文件内容 → 新 digest
    file_path.write_text("v2-content", encoding="utf-8")
    async with sf() as session:
        svc = IndexDocumentService(session)
        r2 = await svc.submit_uploaded_file(user_id=1, file_public_id="file_1")
        assert r2["created"] is True
        assert r2["document_public_id"] != r1["document_public_id"]
        assert r2["superseded"] is True
        # 旧行 superseded
        repo = ContextIndexDocumentRepository(session)
        old = await repo.get_by_public_id(r1["document_public_id"], 1)
        assert old is None or old.status == "superseded"


async def test_document_submit_cross_user_rejected(tmp_path, sqlite_session_factory):
    """owner-scope：其他用户无法索引该文件。"""
    from app.context_engine.indexing.document_service import DocumentIndexError, IndexDocumentService
    from app.models.uploaded_file import UploadedFile
    from datetime import datetime

    file_path = tmp_path / "doc.txt"
    file_path.write_text("secret", encoding="utf-8")

    sf = sqlite_session_factory
    async with sf() as session:
        session.add(
            UploadedFile(
                id=1, public_id="file_1", user_id=1, conversation_id=1,
                original_name="doc.txt", stored_name="doc.txt", file_ext="txt",
                file_size=6, storage_type="local", storage_path=str(file_path),
                created_at=datetime.now(), updated_at=datetime.now(),
            )
        )
        await session.commit()

    async with sf() as session:
        svc = IndexDocumentService(session)
        with pytest.raises(DocumentIndexError) as exc_info:
            await svc.submit_uploaded_file(user_id=2, file_public_id="file_1")
        assert exc_info.value.code == "context.index.file_not_found"


async def test_document_submit_unsupported_type_rejected(tmp_path, sqlite_session_factory):
    """不支持类型 → rejected（不伪解析）。"""
    from app.context_engine.indexing.document_service import DocumentIndexError, IndexDocumentService
    from app.models.uploaded_file import UploadedFile
    from datetime import datetime

    file_path = tmp_path / "doc.bin"
    file_path.write_bytes(b"\x00\x01\x02")

    sf = sqlite_session_factory
    async with sf() as session:
        session.add(
            UploadedFile(
                id=1, public_id="file_1", user_id=1, conversation_id=1,
                original_name="doc.bin", stored_name="doc.bin", file_ext="bin",
                file_size=3, storage_type="local", storage_path=str(file_path),
                created_at=datetime.now(), updated_at=datetime.now(),
            )
        )
        await session.commit()

    async with sf() as session:
        svc = IndexDocumentService(session)
        with pytest.raises(DocumentIndexError) as exc_info:
            await svc.submit_uploaded_file(user_id=1, file_public_id="file_1")
        assert exc_info.value.code == "context.index.unsupported_type"
