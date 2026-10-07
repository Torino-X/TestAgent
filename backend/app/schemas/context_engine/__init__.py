"""CE-05 Context Engine REST Schema 包。

统一资源响应结构（复用 ApiResponse envelope），避免与 app/schemas/context.py
既有领域模型冲突。只定义 API 边界 DTO，不持有 ORM 引用。
"""
# auto-appended module-level note: schemas/context_engine 子包: ContextEngine 的 Pydantic 输出契约(rest + cursor)。
