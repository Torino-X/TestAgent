"""Phase 2.8R-G E2E test package.

这些测试**需要真实 PostgreSQL + Redis**(由 docker-compose.test.yml 提供)。
无 docker 时 conftest.py 自动 skip 整个 package。

14 个 case 列表(docs/35 §7.2):
  1. 无 SSE 任务创建后继续       ── test_01_no_sse_continues.py
  2. 跨 Worker 实时事件          ── test_02_cross_worker_events.py
  3. Last-Event-ID 断线恢复      ── test_03_last_event_id_recovery.py
  4-7. Worker Kill 在不同阶段后跨进程恢复
                                   ── test_04_worker_kill_recovery.py
  8. 跨 Worker Resume              ── test_05_cross_worker_resume.py
  9. 跨 Worker Cancel              ── test_06_cross_worker_cancel.py
  10. Redis 故障行为               ── test_07_redis_failure.py
  11. PostgreSQL 故障行为          ── test_08_postgres_failure.py
  12. 双进程 Artifact 并发          ── test_09_artifact_race.py
  13. v2 / v3 同时运行              ── test_10_v2_v3_concurrent.py
  14. 跨 Worker LangGraph Interrupt─ test_11_cross_worker_interrupt.py

不在范围:
  ❌ LLM 真实调用(成本 / 不稳定)
  ❌ WordExportTool 真实写文件(只断言 DB 记录 + idempotency)
"""