

# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (OCR 上游 HTTP 客户端):
#
#   链路:
#     ocr_service.process(image_bytes) →
#       → ocr_process_client.send(image_bytes, config)
#         → HTTP POST 到上游 OCR provider(baidu/aliyun/tencent/...)
#         → 返回 OCR 文本 + bbox 信息
#
# 关键约束(供开发者速查):
#   - 严禁直接 urllib/requests 调 HTTP,只能走本 client(便于统一鉴权 + retry
#     + 监控);
#   - 三方 API key 由 settings_service 注入,本 client 不持久化;
#   - 上游超时 10s/请求,失败重试 3 次(指数退避);
#   - 单 image 不超过 5MB(超过则压缩后上传)。
