

# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (F020 OCR Pipeline 服务层):
#
#   链路:
#     RequirementParserTool 解析含图片的文档时:
#       → DocumentReader.extract_images(...) 抽出 image blocks
#       → 对每张图片依次:
#           ImageUnderstandingOrchestrator.understand_one(image)
#             → VisionService.understand(...)  ← vision 模型首选
#             → 失败/不可用 → ocr_service.process(image_bytes)
#               → ocr_worker 拉起 → 三方 OCR API(baidu/aliyun/...)
#     OCR 文本回填到 text_content 里(标 fallback 标记)
#
# 关键约束(供开发者速查):
#   - OCR 是 Vision 不可用时的兜底,而非主路径;
#   - 失败的图片标"未能识别";只要 OCR 也失败才算真失败;
#   - ocr_process_client 是 HTTP 客户端,不允许本服务直接 urllib;
#   - ocr_worker 是后台异步队列(F020 后台抽取任务);
#   - PDF/Doc 内的图片嵌入由 document_reader 提前处理,与 OCR 解耦。
