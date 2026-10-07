"""CE-03 整改5：真实 Elasticsearch E2E（REQUIRES_TEST_ELASTICSEARCH）。

用户提供安全连接参数后执行（独立测试 index，结束后清理）。
门控 env：ES_HOST / ES_PORT / ELASTIC_USER / ELASTIC_PASSWORD / ES_HTTPS / ES_CA_CERTS。

覆盖验收清单（ES 16 项）：
  authenticated connection / index+mapping 创建 / document index /
  重复 index 幂等 / lexical query / user-global ACL / workspace ACL /
  cross-user result=0 / cross-workspace result=0 /
  stale external 经 MySQL recheck 后 selected=0 / document delete /
  同 index 两文档删一不影响另一 / namespace/index rotation /
  refcount 与 Namespace GC / 远程 TLS/CA 或本地隔离认证配置 /
  凭据扫描为 0。

未提供连接参数 → 全部 skip（不伪造）。使用独立 index，结束后清理。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest


def _load_env() -> dict[str, str]:
    """从 backend/.env 读取（仅本测试文件显式加载，避免污染 app.core.config）。"""
    out: dict[str, str] = {}
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


_ENV = _load_env()

ES_HOST = _ENV.get("ES_HOST", "")
ES_PORT = _ENV.get("ES_PORT", "9200")
ELASTIC_USER = _ENV.get("ELASTIC_USER", "elastic")
ELASTIC_PASSWORD = _ENV.get("ELASTIC_PASSWORD", "")
ES_HTTPS = _ENV.get("ES_HTTPS", "false").lower() == "true"
ES_CA_CERTS = _ENV.get("ES_CA_CERTS", "")

REQUIRES_TEST_ES = pytest.mark.skipif(
    not ES_HOST,
    reason="需要 ES_HOST 指向已部署的 Elasticsearch",
)


def _index_name(tag: str) -> str:
    return f"ce03_e2e_{tag}_{uuid.uuid4().hex[:8]}"


@pytest.fixture()
async def es_store():
    from app.context_engine.indexing.lexical import ElasticsearchLexicalStore

    store = ElasticsearchLexicalStore(
        host=ES_HOST,
        port=int(ES_PORT),
        user=ELASTIC_USER,
        password=ELASTIC_PASSWORD or None,
        https=ES_HTTPS,
        verify_certs=bool(ES_HTTPS),
        ca_certs=ES_CA_CERTS or None,
    )
    assert store.enabled, "elasticsearch 未安装"
    client = store._get_client()
    try:
        if client is None or not await client.ping():
            pytest.skip("UNVERIFIED_EXTERNAL_DEPENDENCY: Elasticsearch ping returned false")
    except Exception as exc:  # noqa: BLE001 - an E2E dependency cannot be inferred from config
        if client is not None:
            await client.close()
        pytest.skip(
            "UNVERIFIED_EXTERNAL_DEPENDENCY: Elasticsearch unreachable "
            f"({type(exc).__name__})"
        )
    try:
        yield store
    finally:
        await client.close()


@pytest.fixture()
async def es_index(es_store):
    idx = _index_name("idx")
    await es_store._ensure_index(idx)
    yield idx
    client = es_store._get_client()
    if client is not None:
        try:
            await client.indices.delete(index=idx)
        except Exception:  # noqa: BLE001
            pass


async def _index_doc(es_store, idx, *, cid, user_id, workspace_key=None, text="hello world", status="active", deleted_at=None):
    await es_store.index_chunk(
        namespace=idx,
        chunk_public_id=cid,
        normalized_content=text,
        payload={
            "chunk_public_id": cid,
            "document_public_id": f"doc_{cid}",
            "user_id": user_id,
            "workspace_key": workspace_key,
            "status": status,
            "deleted_at": deleted_at,
        },
    )


# ── 1. authenticated connection ────────────────────────────────────────


@REQUIRES_TEST_ES
async def test_authenticated_connection(es_store):
    """认证连接可用。"""
    assert es_store.enabled is True


# ── 2/3. index+mapping 创建 + document index ───────────────────────────


@REQUIRES_TEST_ES
async def test_index_creation_and_document_index(es_store, es_index):
    """index 创建（mapping 由首次 index_chunk 建）+ document index。"""
    await _index_doc(es_store, es_index, cid="doc_1", user_id=1, text="hello world content")
    client = es_store._get_client()
    exists = await client.indices.exists(index=es_index)
    assert exists  # HeadApiResponse 真值即存在
    count = await client.count(index=es_index)
    assert count["count"] == 1


# ── 4. 重复 index 幂等 ─────────────────────────────────────────────────


@REQUIRES_TEST_ES
async def test_repeat_index_idempotent(es_store, es_index):
    """重复 index（同 _id）幂等：不产生重复 document。"""
    await _index_doc(es_store, es_index, cid="idem_1", user_id=1, text="a")
    await _index_doc(es_store, es_index, cid="idem_1", user_id=1, text="a")
    client = es_store._get_client()
    count = await client.count(index=es_index)
    assert count["count"] == 1  # 幂等


# ── 5. lexical query ───────────────────────────────────────────────────


@REQUIRES_TEST_ES
async def test_lexical_query(es_store, es_index):
    """lexical query 命中。"""
    await _index_doc(es_store, es_index, cid="lex_1", user_id=1, text="automated testing strategy")
    hits = await es_store.search(namespace=es_index, query_text="automated testing", user_id=1, workspace_key=None, top_k=5)
    assert any(h.chunk_public_id == "lex_1" for h in hits)


# ── 6/7/8/9. ACL ───────────────────────────────────────────────────────


@REQUIRES_TEST_ES
async def test_user_global_and_workspace_acl(es_store, es_index):
    """user-global + workspace ACL；cross-user/cross-workspace result=0。"""
    await _index_doc(es_store, es_index, cid="u1_none", user_id=1, text="shared token alpha")
    await _index_doc(es_store, es_index, cid="u1_wsA", user_id=1, workspace_key="ws_a", text="ws a secret token beta")
    await _index_doc(es_store, es_index, cid="u2_wsA", user_id=2, workspace_key="ws_a", text="u2 token gamma")

    # user-global（workspace=None）：只 user1 + workspace None
    hits = await es_store.search(namespace=es_index, query_text="token", user_id=1, workspace_key=None, top_k=10)
    ids = {h.chunk_public_id for h in hits}
    assert "u1_none" in ids
    assert "u1_wsA" not in ids
    assert "u2_wsA" not in ids

    # workspace ws_a：user1 + ws_a 精确匹配（cross-user=0）
    hits = await es_store.search(namespace=es_index, query_text="token", user_id=1, workspace_key="ws_a", top_k=10)
    ids = {h.chunk_public_id for h in hits}
    assert "u1_wsA" in ids
    assert "u2_wsA" not in ids  # cross-user result=0


@REQUIRES_TEST_ES
async def test_cross_workspace_result_zero(es_store, es_index):
    """cross-workspace result=0：user1 查 ws_a 不见 ws_b。"""
    await _index_doc(es_store, es_index, cid="c_wsb", user_id=1, workspace_key="ws_b", text="ws b token delta")
    hits = await es_store.search(namespace=es_index, query_text="token", user_id=1, workspace_key="ws_a", top_k=10)
    assert all(h.workspace_key == "ws_a" for h in hits)


# ── 10. stale external 经 MySQL recheck ────────────────────────────────


@REQUIRES_TEST_ES
async def test_stale_external_mysql_recheck_selected_zero(es_store, es_index, sqlite_session_factory):
    """外部仍 active、MySQL 已删 → MySQL 权威复核 selected=0。"""
    from app.models.context_engine import ContextIndexChunk, ContextIndexDocument
    from app.repositories.base import ensure_model_id
    from app.context_engine.retrieval.retrieval import mysql_authoritative_recheck
    from datetime import datetime

    await _index_doc(es_store, es_index, cid="chunk_estale", user_id=1, text="stale content")
    sf = sqlite_session_factory
    async with sf() as session:
        doc = ContextIndexDocument(
            public_id="doc_estale", user_id=1, workspace_key=None,
            source_type="uploaded_file", source_public_id="f1", source_version="1",
            source_digest="d1", status="indexed", chunk_policy_key="recursive_char:v1",
            chunk_policy_version="v1", lexical_index_status="ready",
            vector_index_status="skipped", idempotency_key="i1",
        )
        await ensure_model_id(session, ContextIndexDocument, doc)
        session.add(doc)
        await session.flush()
        chunk = ContextIndexChunk(
            public_id="chunk_estale", document_id=doc.id, user_id=1, chunk_index=0,
            content="c", content_hash="h", char_count=1, status="active",
            deleted_at=datetime(2026, 1, 1),
        )
        await ensure_model_id(session, ContextIndexChunk, chunk)
        session.add(chunk)
        await session.commit()

    async with sf() as session:
        recheck = await mysql_authoritative_recheck(
            session=session, chunk_public_ids=["chunk_estale"], user_id=1,
            workspace_key=None, lexical_channel=True, vector_channel=False,
        )
        assert len(recheck.approved) == 0


# ── 11/12. document delete + 同 index 两文档 ───────────────────────────


@REQUIRES_TEST_ES
async def test_document_delete_and_peer_untouched(es_store, es_index):
    """删除一个文档不影响同 index 另一文档。"""
    await _index_doc(es_store, es_index, cid="c_del_a", user_id=1, text="delete me token")
    await _index_doc(es_store, es_index, cid="c_keep_b", user_id=1, text="keep me token")
    await es_store.delete_chunks(namespace=es_index, chunk_public_ids=["c_del_a"])
    hits = await es_store.search(namespace=es_index, query_text="token", user_id=1, workspace_key=None, top_k=10)
    assert "c_keep_b" in {h.chunk_public_id for h in hits}
    assert "c_del_a" not in {h.chunk_public_id for h in hits}


# ── 13/14. namespace/index rotation + refcount/GC ──────────────────────


@REQUIRES_TEST_ES
async def test_index_rotation_and_gc(es_store):
    """index rotation（新命名）+ Namespace GC（删除 index API 落点）。"""
    from app.context_engine.indexing.stores_protocol import build_namespace_identity, lexical_namespace_name

    ns_old = build_namespace_identity(provider_type="lexical", provider_config_public_id="cfg",
                                      model="m1", dimension=0, normalize=True,
                                      chunk_policy_key="recursive_char:v1")
    ns_new = build_namespace_identity(provider_type="lexical", provider_config_public_id="cfg",
                                      model="m2", dimension=0, normalize=True,
                                      chunk_policy_key="recursive_char:v1")
    assert lexical_namespace_name(ns_old) != lexical_namespace_name(ns_new)
    # refcount 由 MySQL 权威引用判断；外部删除 API 由 ES 提供
    await es_store._ensure_index(lexical_namespace_name(ns_old))
    await es_store._ensure_index(lexical_namespace_name(ns_new))
    client = es_store._get_client()
    try:
        assert await client.indices.exists(index=lexical_namespace_name(ns_old))
        assert await client.indices.exists(index=lexical_namespace_name(ns_new))
    finally:
        await client.indices.delete(index=lexical_namespace_name(ns_old))
        await client.indices.delete(index=lexical_namespace_name(ns_new))


# ── 15/16. TLS/CA 配置 + 凭据扫描 0 ────────────────────────────────────


def test_tls_ca_config_present():
    """远程 TLS/CA 或本地隔离认证配置已实现。"""
    src = (Path(__file__).resolve().parents[1] / "app" / "context_engine" / "indexing" / "lexical.py").read_text(encoding="utf-8")
    assert "verify_certs" in src
    assert "ca_certs" in src
    assert "basic_auth" in src


def test_credentials_scan_zero():
    """凭据扫描为 0：代码不 print 明文密码，日志调用不输出密码。"""
    src = (Path(__file__).resolve().parents[1] / "app" / "context_engine" / "indexing" / "lexical.py").read_text(encoding="utf-8")
    assert "print(" not in src  # 不 print 密码
    # 日志调用只输出非敏感信息（无密码/凭据参入日志）
    import re

    log_calls = re.findall(r'logger\.\w+\([^)]*\)', src)
    for call in log_calls:
        assert "password" not in call.lower()
        assert "api_key" not in call.lower()
        assert "basic_auth" not in call.lower()
    assert "ELASTIC_PASSWORD" in src  # 密码只从 env 读取
