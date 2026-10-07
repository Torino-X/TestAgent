# 告警模板

Logging V2 的 P1/P2/P3 告警应覆盖：HTTP 5xx 速率、`agent.task.failed`、`context.compose.failed`、`llm.request.failed`/timeout、依赖不可用、新错误指纹突增、Postgres fail-closed 与 Redis 退化。

P1：密钥泄露、跨用户/Context 泄露、生产派发不可用、持续 Postgres fail-closed。P2：Agent/LLM/Context 失败率或依赖不可用。P3：重试、延迟与新 WARNING/ERROR 指纹上升。

每个 P1/P2 告警必须链接到中文运行手册中的 symptom、Loki 查询、Tempo Trace、首检与安全缓解步骤。禁止把 task、trace、request、conversation、project 或 user 标识符放入标签或告警分组键。
