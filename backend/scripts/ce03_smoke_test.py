"""CE-03 E2E-4：连接 Smoke Test（输出脱敏，密码/API Key 不打印）。

从运行 TestAgent 的本地机器验证：
- Qdrant authenticated connection + version
- Elasticsearch authenticated connection + version
URL 使用云服务器公网 IP（来自 backend/.env），不使用 127.0.0.1。
"""

from __future__ import annotations

import os
from pathlib import Path


def _load_env(env_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not env_path.is_file():
        return out
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _redact(value: str | None) -> str:
    if not value:
        return "(unset)"
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def main() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    env = _load_env(env_path)

    q_host = env.get("QDRANT_HOST", "")
    q_port = int(env.get("QDRANT_PORT", "6333"))
    q_key = env.get("QDRANT_API_KEY", "")
    q_https = env.get("QDRANT_HTTPS", "false").lower() == "true"

    es_host = env.get("ES_HOST", "")
    es_port = int(env.get("ES_PORT", "9200"))
    es_user = env.get("ELASTIC_USER", "elastic")
    es_pass = env.get("ELASTIC_PASSWORD", "")
    es_https = env.get("ES_HTTPS", "false").lower() == "true"

    print("=== Qdrant Smoke ===")
    print(f"host={q_host} port={q_port} https={q_https} api_key={_redact(q_key)}")
    try:
        from qdrant_client import AsyncQdrantClient
        import qdrant_client as _qc

        # q_host 已含 scheme（http:// 或 https://）；port 由 QDRANT_PORT 补全
        base = q_host.rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = f"{'https' if q_https else 'http'}://{base}"
        client = AsyncQdrantClient(
            url=f"{base}:{q_port}",
            api_key=q_key or None,
            prefer_grpc=False,
        )
        client.get_collections()
        print(f"authenticated_connection=PASS collections_accessible=True")
        print(f"client_version={getattr(_qc, '__version__', 'unknown')}")
    except Exception as exc:  # noqa: BLE001
        print(f"authenticated_connection=FAIL error={type(exc).__name__}: {exc}")
    finally:
        import asyncio

        async def _close():
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

        try:
            asyncio.run(_close())
        except Exception:  # noqa: BLE001
            pass

    print()
    print("=== Elasticsearch Smoke ===")
    print(f"host={es_host} port={es_port} https={es_https} user={es_user} password={_redact(es_pass)}")
    try:
        from elasticsearch import AsyncElasticsearch

        base = es_host.rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = f"{'https' if es_https else 'http'}://{base}"
        client = AsyncElasticsearch(
            hosts=[f"{base}:{es_port}"],
            basic_auth=(es_user, es_pass) if es_pass else None,
            verify_certs=False,
            request_timeout=15,
        )
        info = __import__("asyncio").run(client.info())
        version = info.get("version", {}).get("number", "?")
        print(f"authenticated_connection=PASS cluster={info.get('cluster_name', '?')} version={version}")
    except Exception as exc:  # noqa: BLE001
        print(f"authenticated_connection=FAIL error={type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
