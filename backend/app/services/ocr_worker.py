

# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (F020 OCR 后台抽取 worker):
#
#   链路:
#     F020 (Phase 异步 OCR 队列,可选路径):
#       RequirementParserTool 解析文档时,
#         图片可走"先入队,稍后批量 OCR"模式降低首屏延迟
#       → ocr_worker.enqueue(image_id)
#         → 任务落到 in-memory 队列(or Redis Phase 升级后)
#         → 异步循环消费 → ocr_service → ocr_process_client
#       → 结果回填到 attachment_understanding + RequirementParserTool 的 text_content
#
# 关键约束(供开发者速查):
#   - 本 worker 可关闭(feature flags 控制);关闭后走 OCR 同步路径;
#   - 队列容量 256 项,超出后入队失败直接拒绝(让上游 fallback);
#   - 不持久化:image 只在内存;重启后队列清空;
#   - 失败仅日志,不动上游 RequirementParserTool 的主流程。
