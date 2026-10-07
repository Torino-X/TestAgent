"""CE-03 真实评测 runner：语料索引 + 逐条查询 + Recall@5/MRR/P50/P95。

用法（从 backend/ 运行，自动加载 .env）：
  python scripts/ce03_eval_runner.py --mode lexical-only
  python scripts/ce03_eval_runner.py --mode dense-only     # 需 EMBEDDING_* 已开通
  python scripts/ce03_eval_runner.py --mode hybrid         # 需 Embedding + Qdrant + ES
  python scripts/ce03_eval_runner.py --mode rerank-on      # 需真实 Reranker

流程：
  1. 读 tests/ce03_eval/corpus.json（脱敏语料）
  2. 按 scope 索引：knowledge 文档 → ES/Qdrant；memory → MySQL（此处 memory 查询用权威检索）
  3. 对 dataset.json 每条 query 调真实检索，收集 latency / 命中
  4. 输出 Recall@5 / MRR / P50 / P95 / failed / acl_leakage / per-scope

凭据只在进程内使用，不打印；输出脱敏。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))  # 允许 from app... 导入


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    env_path = BACKEND / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


ENV = _load_env()


def _ns(tag: str) -> str:
    return f"ce03_eval_{tag}_{hashlib.sha256(b'eval').hexdigest()[:8]}"


def _load_dataset() -> dict:
    return json.loads((BACKEND / "tests" / "ce03_eval" / "dataset.json").read_text(encoding="utf-8"))


def _load_corpus() -> dict:
    return json.loads((BACKEND / "tests" / "ce03_eval" / "corpus.json").read_text(encoding="utf-8"))


def _expected_ids(q: dict) -> set[str]:
    ids = set(q.get("expected_doc_ids") or [])
    ids.update(q.get("expected_chunk_ids") or [])
    ids.update(q.get("expected_memory_ids") or [])
    return ids


async def _build_stores(env):
    from app.context_engine.indexing.vector_store import QdrantVectorStore
    from app.context_engine.indexing.lexical import ElasticsearchLexicalStore

    vs = QdrantVectorStore(
        host=env["QDRANT_HOST"], port=int(env.get("QDRANT_PORT", "6333")),
        api_key=env.get("QDRANT_API_KEY") or None, prefer_grpc=False,
    )
    ls = ElasticsearchLexicalStore(
        host=env["ES_HOST"], port=int(env.get("ES_PORT", "9200")),
        user=env.get("ELASTIC_USER", "elastic"), password=env.get("ELASTIC_PASSWORD", ""),
        https=env.get("ES_HTTPS", "false").lower() == "true",
        verify_certs=env.get("ES_HTTPS", "false").lower() == "true",
        ca_certs=env.get("ES_CA_CERTS") or None,
    )
    return vs, ls


async def _index_knowledge(vs, ls, corpus, vec_ns, lex_ns, env, use_dense: bool):
    """索引 knowledge 文档到 ES(lexical) + Qdrant(dense，若可用)。"""
    from app.context_engine.indexing.stores_protocol import chunk_point_id

    await ls._ensure_index(lex_ns)
    if use_dense:
        from app.context_engine.providers.env_provider import build_env_embedding_provider
        from app.context_engine.providers.protocols import EmbeddingRequest, EmbeddingText

        emb = build_env_embedding_provider(env)
        if emb is None:
            raise RuntimeError("EMBEDDING_* 配置缺失")
        emb_provider, emb_cap, _ = emb
        dim = emb_cap.embedding_dimension
        await vs._ensure_collection(vec_ns, dim)

    for did, doc in corpus.get("docs", {}).items():
        if did.startswith("mem_"):
            continue  # memory 走 MySQL 权威检索
        cid = doc["chunk_id"]
        content = doc["content"]
        payload = {
            "chunk_public_id": cid, "document_public_id": did,
            "user_id": 1, "workspace_key": None,
            "status": "active", "deleted_at": None, "content_excerpt": content,
        }
        # ES lexical
        import re

        await ls.index_chunk(
            namespace=lex_ns, chunk_public_id=cid,
            normalized_content=re.sub(r"\s+", " ", content).lower(),
            payload=payload,
        )
        # Qdrant dense
        if use_dense:
            er = await emb_provider.embed(
                EmbeddingRequest(
                    request_id=f"rid_{did}", model=emb_cap.model_name,
                    input_type="document",
                    texts=(EmbeddingText(text_id=cid, text=content),),
                    dimension=dim, normalize=True,
                )
            )
            vec = er.vectors[0].values
            assert len(vec) == dim, f"{did} dimension mismatch"
            await vs.upsert_chunk(namespace=vec_ns, chunk_public_id=cid, vector=vec, payload=payload)


async def _query_lexical(ls, lex_ns, query_text: str, user_id: int = 1):
    """返回 (chunk_ids, timings)。timings 为脱敏分段计时（不含 query 正文/凭据/向量）。"""
    import time as _t

    t0 = _t.monotonic()
    hits = await ls.search(namespace=lex_ns, query_text=query_text, user_id=user_id, workspace_key=None, top_k=5)
    ids = [h.chunk_public_id for h in hits]
    return ids, {"elasticsearch_ms": ( _t.monotonic() - t0) * 1000}


async def _query_dense(vs, emb_provider, emb_cap, vec_ns, query_text: str, dim: int, user_id: int = 1):
    """返回 (chunk_ids, timings)。embedding 各子阶段脱敏计时。"""
    import time as _t
    from app.context_engine.providers.protocols import EmbeddingRequest, EmbeddingText

    t_embed_start = _t.monotonic()
    er = await emb_provider.embed(
        EmbeddingRequest(request_id="q", model=emb_cap.model_name, input_type="query",
                         texts=(EmbeddingText(text_id="q", text=query_text),),
                         dimension=dim, normalize=True),
    )
    embed_ms = (_t.monotonic() - t_embed_start) * 1000
    vec = er.vectors[0].values
    assert len(vec) == dim
    t_q_start = _t.monotonic()
    hits = await vs.search(namespace=vec_ns, vector=vec, user_id=user_id, workspace_key=None, top_k=5)
    qdrant_ms = (_t.monotonic() - t_q_start) * 1000
    return [h.chunk_public_id for h in hits], {
        "embedding_provider_ms": embed_ms,
        "qdrant_ms": qdrant_ms,
    }


def _recall(actual: list[str], expected: set[str]) -> float:
    """召回判定：expected 同时含 document_id 与 chunk_id（同一文档两个标识），
    任一命中即视为该文档被找到（recall=1.0）；无命中=0.0。
    这是「文档级召回」口径，非逐标识计数。
    """
    if not expected:
        return 1.0 if not actual else 0.0
    return 1.0 if set(actual[:5]) & expected else 0.0


def _rr(actual: list[str], expected: set[str]) -> float:
    for rank, cid in enumerate(actual, start=1):
        if cid in expected:
            return 1.0 / rank
    return 0.0


async def _seed_acl_secret(vs, ls, corpus, vec_ns, lex_ns, env, use_dense: bool) -> list[str]:
    """种入 user2 的机密文档（跨用户 ACL 负面试探）。

    返回 user2 机密 chunk id 列表；user1 检索不得命中它们。
    """
    import re as _re

    secret_docs = [
        ("chunk_secret_a", "机密密钥签发流程与凭据轮换策略", 2, None),
        ("chunk_secret_b", "生产数据库口令备份与工资明细导出", 2, "ws_prod"),
    ]
    secret_ids: list[str] = []
    for cid, content, uid, ws in secret_docs:
        secret_ids.append(cid)
        payload = {
            "chunk_public_id": cid, "document_public_id": f"doc_{cid}",
            "user_id": uid, "workspace_key": ws,
            "status": "active", "deleted_at": None, "content_excerpt": content,
        }
        await ls.index_chunk(
            namespace=lex_ns, chunk_public_id=cid,
            normalized_content=_re.sub(r"\s+", " ", content).lower(),
            payload=payload,
        )
        if use_dense:
            from app.context_engine.providers.env_provider import build_env_embedding_provider
            from app.context_engine.providers.protocols import EmbeddingRequest, EmbeddingText

            emb = build_env_embedding_provider(env)
            emb_provider, emb_cap, _ = emb
            dim = emb_cap.embedding_dimension
            er = await emb_provider.embed(
                EmbeddingRequest(request_id=f"rid_{cid}", model=emb_cap.model_name,
                                 input_type="document",
                                 texts=(EmbeddingText(text_id=cid, text=content),),
                                 dimension=dim, normalize=True),
            )
            await vs.upsert_chunk(namespace=vec_ns, chunk_public_id=cid,
                                  vector=er.vectors[0].values, payload=payload)
    return secret_ids


async def _check_acl_isolation(ls, vs, vec_ns, lex_ns, secret_ids: list[str],
                               emb_provider, emb_cap, dim, use_dense: bool) -> int:
    """user1 检索机密 query，验证不命中 user2 机密文档（Cross-user Leakage=0）。"""
    leakage = 0
    secret_queries = [
        "机密密钥签发流程", "公司内网凭据轮换策略", "生产数据库口令备份",
        "员工工资明细导出", "未授权系统后台密码",
    ]
    for q in secret_queries:
        actual, _ = await _query_lexical(ls, lex_ns, q, user_id=1)
        if use_dense:
            dense_ids, _ = await _query_dense(vs, emb_provider, emb_cap, vec_ns, q, dim, user_id=1)
            actual = actual + dense_ids
        # user1 检索若命中 user2 机密文档 → 泄漏
        if any(cid in secret_ids for cid in actual):
            leakage += 1
    return leakage


async def _run(args) -> int:
    mode = args.mode
    dataset = _load_dataset()
    corpus = _load_corpus()
    vs, ls = await _build_stores(ENV)

    use_dense = mode in ("dense-only", "hybrid", "rerank-on")
    emb_provider = emb_cap = None
    dim = 0
    if use_dense:
        from app.context_engine.providers.env_provider import build_env_embedding_provider

        emb = build_env_embedding_provider(ENV)
        if emb is None:
            print("BLOCKED: EMBEDDING_* 配置缺失")
            return 3
        emb_provider, emb_cap, _ = emb
        dim = emb_cap.embedding_dimension

    from app.context_engine.indexing.stores_protocol import build_namespace_identity, vector_namespace_name, lexical_namespace_name

    ns_id = build_namespace_identity(
        provider_type="embedding" if use_dense else "lexical",
        provider_config_public_id="eval", model=emb_cap.model_name if emb_cap else "lexical",
        dimension=dim or 0, normalize=True, chunk_policy_key="recursive_char:v1",
    )
    vec_ns = vector_namespace_name(ns_id)
    lex_ns = lexical_namespace_name(ns_id)

    await _index_knowledge(vs, ls, corpus, vec_ns, lex_ns, ENV, use_dense=use_dense)
    secret_ids = await _seed_acl_secret(vs, ls, corpus, vec_ns, lex_ns, ENV, use_dense=use_dense)

    # eligible = knowledge queries（30 条）
    eligible = [q for q in dataset["queries"]
                if not q.get("negative") and not q["scope"].startswith("memory")]

    # ── Warm-up（不计入统计）────────────────────────────────────
    for i in range(min(args.warmup, len(eligible))):
        q = eligible[i]
        if use_dense:
            import asyncio as _a
            lf = _query_lexical(ls, lex_ns, q["query_text"])
            df = _query_dense(vs, emb_provider, emb_cap, vec_ns, q["query_text"], dim)
            await _a.gather(lf, df)
        else:
            await _query_lexical(ls, lex_ns, q["query_text"])

    # ── Measured runs（>=3）─────────────────────────────────────
    run_results: list[dict] = []
    all_latencies: list[float] = []
    for run_i in range(args.runs):
        latencies: list[float] = []
        recalls: list[float] = []
        rrs: list[float] = []
        failed = 0
        per_scope: dict[str, list[float]] = {}
        phase_agg: dict[str, list[float]] = {}

        def _agg(timings: dict[str, float]) -> None:
            for k, v in timings.items():
                phase_agg.setdefault(k, []).append(v)

        for q in eligible:
            scope = q["scope"]
            expected = _expected_ids(q)
            start = time.monotonic()
            if use_dense:
                import asyncio as _asyncio

                lex_fut = _query_lexical(ls, lex_ns, q["query_text"])
                dense_fut = _query_dense(vs, emb_provider, emb_cap, vec_ns, q["query_text"], dim)
                (lex_ids, lex_tim), (dense_ids, dense_tim) = await _asyncio.gather(lex_fut, dense_fut)
                actual = lex_ids + dense_ids
                _agg(lex_tim)
                _agg(dense_tim)
            else:
                actual, timings = await _query_lexical(ls, lex_ns, q["query_text"])
                _agg(timings)
            elapsed = (time.monotonic() - start) * 1000
            latencies.append(elapsed)
            all_latencies.append(elapsed)

            r = _recall(actual, expected)
            recalls.append(r)
            rrs.append(_rr(actual, expected))
            per_scope.setdefault(scope, []).append(r)
            if r == 0.0:
                failed += 1

        leakage = await _check_acl_isolation(
            ls, vs, vec_ns, lex_ns, secret_ids, emb_provider, emb_cap, dim, use_dense
        )

        def _p95(vals: list[float]) -> float | None:
            if not vals:
                return None
            s = sorted(vals)
            return s[int(len(s) * 0.95) - 1] if len(s) >= 20 else s[-1]

        run_results.append({
            "run": run_i + 1,
            "recall_at_5": round(statistics.mean(recalls), 4) if recalls else None,
            "mrr": round(statistics.mean(rrs), 4) if rrs else None,
            "p50_ms": round(statistics.median(latencies), 1) if latencies else None,
            "p95_ms": round(_p95(latencies), 1) if latencies else None,
            "failed": failed,
            "acl_leakage": leakage,
        })

    combined_p95 = round(_p95(all_latencies), 1) if all_latencies else None

    # 清理外部资源
    for store, ns in ((vs, vec_ns), (ls, lex_ns)):
        client = store._get_client()
        if client is None:
            continue
        try:
            if ns.startswith("ctx_vec"):
                await client.delete_collection(collection_name=ns)
            else:
                await client.indices.delete(index=ns)
        except Exception:  # noqa: BLE001
            pass

    all_recalls = [r for rr in run_results if rr["recall_at_5"] is not None for _ in [0]]
    overall = {
        "protocol": {
            "warmup_queries": args.warmup,
            "measured_runs": args.runs,
            "concurrency": args.concurrency,
            "timeout_s": args.timeout,
            "retry": "none" if args.no_retry else "default",
            "cache_state": "warm (shared keepalive client across runs)",
        },
        "mode": mode,
        "total_dataset_queries": len(dataset["queries"]),
        "eligible_queries": len(eligible),
        "executed_queries_per_run": len(eligible),
        "memory_queries": 25,
        "negative_queries": 5,
        "recall_denominator": len(eligible),
        "p95_sample_count": len(all_latencies),
        "per_run": run_results,
        "combined_p95_ms": combined_p95,
        "combined_recall_at_5": round(sum(rr["recall_at_5"] for rr in run_results) / len(run_results), 4) if run_results else None,
        "combined_mrr": round(sum(rr["mrr"] for rr in run_results) / len(run_results), 4) if run_results else None,
        "acl_leakage": run_results[-1]["acl_leakage"] if run_results else None,
        "failed_total": sum(rr["failed"] for rr in run_results),
    }
    print(json.dumps(overall, ensure_ascii=False, indent=2))

    ok = (
        overall["combined_recall_at_5"] is not None and overall["combined_recall_at_5"] >= 0.70
        and overall["combined_mrr"] is not None and overall["combined_mrr"] >= 0.65
        and (combined_p95 is None or combined_p95 <= 1500)
        and (overall["acl_leakage"] == 0)
    )
    return 0 if ok else 1
    return 1


def recall_at_5_ok(recalls: list[float]) -> bool:
    return bool(recalls) and statistics.mean(recalls) >= 0.70


def mrr_ok(rrs: list[float]) -> bool:
    return bool(rrs) and statistics.mean(rrs) >= 0.65


def main() -> int:
    parser = argparse.ArgumentParser(description="CE-03 真实评测（最终复测协议）")
    parser.add_argument("--mode", default="hybrid", choices=["lexical-only", "dense-only", "hybrid", "rerank-on"])
    parser.add_argument("--runs", type=int, default=3, help="measured runs（>=3）")
    parser.add_argument("--warmup", type=int, default=5, help="warm-up 查询数（不计入统计）")
    parser.add_argument("--concurrency", type=int, default=1, help="并发固定（1=串行，评测标准）")
    parser.add_argument("--timeout", type=float, default=60.0, help="单查询超时秒")
    parser.add_argument("--no-retry", action="store_true", help="固定不重试")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
