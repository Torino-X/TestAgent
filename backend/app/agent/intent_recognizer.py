"""Task intent recognizer — determines whether a user message maps to a known task type."""

from __future__ import annotations


class TaskIntentRecognizer:
    """Checks if the user is requesting test-plan generation.

    Phase 1 mock: simple keyword matching. Later phases can replace with
    an LLM classification call.
    """

    TASK_KEYWORDS: dict[str, list[str]] = {
        "test_plan_generation": [
            "测试方案",
            "生成测试方案",
            "测试文档",
            "测试计划",
            "test plan",
            "测试方案生成",
        ],
    }

    def recognize(self, user_message: str) -> str | None:
        """Return a task_type string like 'test_plan_generation', or None."""
        lower = user_message.lower()
        for task_type, keywords in self.TASK_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in lower:
                    return task_type
        return None

    def requires_files(self, _task_type: str) -> list[str]:
        """Which file types are required for the given task type."""
        return ["requirement_doc", "test_plan_template"]


# 模块定位:任务意图识别 = 判断用户消息映射到哪个 task 类型
#
# 链路:
#   MessageService → IntentRecognizer.classify(prompt, attachments)
#     → 5 路由中的一个或 None(tool_confidence < threshold 视为 None)
#       chat_reply / ask_for_files / unsupported / clarify / agent_task
#
# 关键约束:
#   - 是 orchestrator.run() 旧入口的前置步骤,Phase 2.9A 起请走
#     intent_router.IntentRouter(LLM-based),更稳定;
#   - 不写 DB,只纯函数分类,返回 dict;
#   - 任务路由附 confidence 字段;<threshold → 走 clarify。
