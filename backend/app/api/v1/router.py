"""API v1 router — aggregates all sub-routers."""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.conversations import router as conversations_router
from app.api.v1.messages import router as messages_router
from app.api.v1.files import router as files_router
from app.api.v1.agent_tasks import router as agent_tasks_router
from app.api.v1.artifacts import router as artifacts_router
from app.api.v1.settings import router as settings_router
from app.api.v1.knowledge import router as knowledge_router
from app.api.v1.health import router as health_router
from app.api.v1.image_understanding import router as image_understanding_router
from app.api.v1.library import router as library_router
from app.api.v1.context_index import router as context_index_router
from app.api.v1.context_memory import router as context_memory_router
from app.api.v1.workspace_instructions import router as workspace_instructions_router
from app.api.v1.context_audit import router as context_audit_router
from app.api.v1.context_payload import router as context_payload_router
from app.api.v1.context_admin import router as context_admin_router
from app.api.v1.context_debug import router as context_debug_router
from app.api.v1.context_observability import router as context_observability_router
from app.api.v1.projects import conversation_router as project_conversation_router
from app.api.v1.projects import router as projects_router
from app.api.v1.templates import router as templates_router
from app.api.v1.help import router as help_router

router = APIRouter()

router.include_router(health_router, tags=["health"])
router.include_router(auth_router, prefix="/auth", tags=["auth"])
router.include_router(conversations_router, prefix="/conversations", tags=["conversations"])
router.include_router(messages_router, tags=["messages"])
router.include_router(files_router, tags=["files"])
router.include_router(agent_tasks_router, prefix="/agent", tags=["agent"])
router.include_router(artifacts_router, prefix="/artifacts", tags=["artifacts"])
router.include_router(settings_router, prefix="/settings", tags=["settings"])
router.include_router(knowledge_router, prefix="/knowledge", tags=["knowledge"])
router.include_router(library_router, prefix="/library", tags=["library"])
router.include_router(projects_router, prefix="/projects", tags=["projects"])
router.include_router(project_conversation_router, tags=["projects"])
router.include_router(templates_router, prefix="/templates", tags=["templates"])
router.include_router(help_router, prefix="/help", tags=["help"])
router.include_router(context_index_router, prefix="/context/index", tags=["context_index"])
router.include_router(context_memory_router, prefix="/context/memory", tags=["context_memory"])
router.include_router(workspace_instructions_router, prefix="/context/workspace-instructions", tags=["workspace_instructions"])
router.include_router(context_audit_router, prefix="/context", tags=["context_audit"])
router.include_router(context_payload_router, prefix="/context", tags=["context_payload"])
router.include_router(context_admin_router, prefix="/context", tags=["context_admin"])
router.include_router(context_debug_router, prefix="/context", tags=["context_debug"])
router.include_router(context_observability_router, prefix="/context", tags=["context_observability"])
router.include_router(
    image_understanding_router,
    prefix="/image-understanding",
    tags=["image_understanding"],
)


# ════════════════════════════════════════════════════════════════════════════════
# API v1 主路由聚合 (FastAPI APIRouter include_router 模式):
#
#   本文件是整层 v1 的"目录":include 所有子路由,给 main.py 一个唯一入口。
#
#   包含顺序不重要 ——
#     FastAPI 按 path + method 注册,与 include 顺序解耦;
#     真正决定优先级的是路径本身(如 '/api/v1/...' vs '/api/v1/auth/...')。
#
#   路由分组:
#     - 聊天核心: conversations / messages / agent_tasks / artifacts(主流程)
#     - 文件管理: files / image_understanding
#     - 设置: settings / auth
#     - 知识库: knowledge
#     - 上下文引擎 (CE-0X): context_admin / context_audit / context_debug /
#       context_index / context_memory / context_observability / context_payload
#     - 系统: workspace_instructions / health
#
# 关键约束(供开发者速查):
#   - 不要在这里写业务逻辑 — 仅 include_router;
#   - 主路由统一挂载 prefix='/api/v1'(由 main.py 注册);
#   - CORS / 中间件 / 鉴权全局层面在 main.py,不在这里。
