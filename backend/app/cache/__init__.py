"""Business Cache Redis 基础设施 (Phase 1 — Redis Cache project).

设计基线:
  - docs/prompt/TestAgent_Redis缓存系统详细技术设计文档.md (§3, §5, §6)
  - docs/prompt/TestAgent_Redis缓存系统_ZCode开发实施提示词.md (§5, §8)
  - docs/102_TestAgent_Redis缓存系统_Claude_Code_交接文档.md (§3, §4)

目录布局::

    backend/app/cache/
    ├── __init__.py              # 本文件
    ├── backend.py               # CacheBackend: 共享 client + lifespan 启停
    ├── specs.py                 # CacheSpec dataclass（域 + TTL + jitter + lock）
    ├── key_builder.py           # 统一 Key 构造器（ta:{env}:cache:v1:{domain}:...）
    ├── serializer.py            # JSON envelope（v / negative / created_at / payload）
    ├── singleflight.py          # 进程内 SingleFlight（dict[key, Future]）
    ├── distributed_lock.py      # SET NX PX + token compare Lua（fill lock）
    ├── circuit_breaker.py       # Redis 操作轻量熔断
    ├── bulkhead.py              # DB fallback 并发闸门
    ├── metrics.py               # 最小 cache metrics 适配（hit/miss/latency/...）
    ├── manager.py               # CacheManager 统一接口（get/set/delete/get_or_load/...）
    └── domains/                 # 各业务域的 domain cache service
        ├── auth_cache.py
        ├── config_cache.py
        ├── conversation_cache.py
        ├── task_cache.py
        ├── library_cache.py
        ├── context_cache.py
        └── semantic_profile_cache.py

所有 Domain Cache 必须遵守 (设计文档 §41 + 提示词 §4):
  1. MySQL 仍是 SoT；Cache Redis 不可用 → DB fallback（限流）
  2. DB commit → cache set（绝不先 set 再 commit）
  3. 禁止 pickle / cloudpickle / marshal；仅 JSON
  4. 禁止缓存明文 API Key / password_hash / JWT / Cookie
  5. 禁止把 agent_events / agent_execution_requests / human_confirmations /
     context_payloads 大对象 / MEDIUMTEXT 字段搬进 Redis
  6. 禁止 Redlock / Bloom Filter / Redis Cluster / Event Stream 双写（V1）
  7. 禁止每个 request 创建 Redis client；lifespan 共享
  8. 不允许 KEYS * / SCAN 做常规业务 invalidation
  9. 不允许缓存完整 message timeline（V1）；仅侧栏 + 任务 status

本会话在 Step 2.3 仅创建包骨架与最小契约；具体实现在 Step 3 起按模块填充。
"""

from __future__ import annotations

__all__: list[str] = []