"""Phase 2.9A.23 — thinking 卡死修复。

症状: 用户确认章节 / 重试任务 / 提交格式丢失决策后, 前端停留在
"正在思考..." 占位消息。后端 SSE 流虽然持续 emit 事件, 但前端
``/events-post-confirm`` SSE 连接建立时机不可靠 / 时序不稳。

修复: 在三个状态切换点显式调用 ``conversationStore.restoreConversation``
重建消息流 — 这是"重新点击进入对话修复"的同一机制, 只是提前主动触发。

强约束: 仍然保留原 ``connectTaskEvents`` 调用, 后续事件继续通过 SSE 实时
推流; restoreConversation 只是兜底加载当前应已有但 UI 缺失的事件。

本测试为源码静态审计 (与 Phase 2.9A.19 ``TestGraphNodeNoLongerEmitsToolFailed``
模式一致) — 验证 ChatWorkspace.vue 在三个状态切换点都调了 restoreConversation。
"""

from __future__ import annotations

import os


# backend/tests/test_phase29a23_thinking_stall_fix.py
#   → backend/tests
#   → backend
#   → repo root (H:\\Agent\\TestAgent)
_THIS_FILE = os.path.abspath(__file__)
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(_THIS_FILE))
)
"""Path resolved to TestAgent repo root regardless of pytest cwd."""


class TestThinkingStallFix:
    """Phase 2.9A.23: 状态切换点必须主动触发 restoreConversation。"""

    def _read_chat_workspace(self) -> str:
        path = os.path.join(
            _REPO_ROOT,
            "frontend",
            "src",
            "components",
            "chat",
            "ChatWorkspace.vue",
        )
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _read_chat_view(self) -> str:
        path = os.path.join(_REPO_ROOT, "frontend", "src", "views", "ChatView.vue")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _function_body(self, source: str, fn_name: str, end_marker: str) -> str:
        idx = source.find(fn_name)
        assert idx >= 0, f"{fn_name} not found in source"
        end_idx = source.find(end_marker, idx)
        assert end_idx > idx, f"could not find end marker '{end_marker}' for {fn_name}"
        return source[idx:end_idx]

    def test_handle_confirm_sections_calls_restore(self):
        """章节确认后必须调用 restoreConversation (兜底 SSE 推流失效)。"""
        source = self._read_chat_workspace()
        body = self._function_body(
            source,
            "async function handleConfirmSections",
            "async function handleRetryTask",
        )
        assert "restoreConversation" in body, (
            "handleConfirmSections must call restoreConversation after confirm "
            "(Phase 2.9A.23 thinking-stall fix); body:\n" + body[:500]
        )

    def test_handle_retry_task_calls_restore(self):
        """任务重试后必须调用 restoreConversation。"""
        source = self._read_chat_workspace()
        body = self._function_body(
            source,
            "async function handleRetryTask",
            "async function handleFormatLossDecision",
        )
        assert "restoreConversation" in body, (
            "handleRetryTask must call restoreConversation; body:\n" + body[:500]
        )

    def test_handle_format_loss_decision_calls_restore(self):
        """格式丢失决策后必须调用 restoreConversation。

        handleFormatLossDecision 是文件中最后定义的 handler,
        函数体提取到下一个顶级 async function 或 </script> 终止符为止。
        """
        source = self._read_chat_workspace()
        # 用 "async function handleDownloadArtifact" 作为起始前一个 marker
        idx = source.find("async function handleFormatLossDecision")
        assert idx >= 0, "handleFormatLossDecision not found"
        # 取函数后 1500 字符 (直到 </script>)
        body = source[idx : idx + 1500]
        # 终止确认: 必须有 </script> 或下一个 async function 在窗口内
        assert "</script>" in body or "async function " in body[len("async function handleFormatLossDecision") :], (
            "Could not isolate handleFormatLossDecision function body"
        )
        assert "restoreConversation" in body, (
            "handleFormatLossDecision must call restoreConversation; body:\n"
            + body[:500]
        )

    def test_chatview_already_calls_restore_on_route_change(self):
        """ChatView 在路由变化时已经调 restoreConversation (历史机制)。

        '重新点击进入对话修复' 走的就是这条路径。
        """
        content = self._read_chat_view()
        assert "await conversationStore.restoreConversation(id)" in content, (
            "ChatView must call restoreConversation on route change — this is "
            "the historical recovery mechanism that 're-enter conversation' "
            "depends on."
        )

    def test_thinking_fix_preserves_sse_connection(self):
        """修复后仍保留原 connectTaskEvents 调用 — SSE 实时推流不被破坏。

        handleFormatLossDecision 不需要单独建立 SSE, 它复用已存在的
        SSE 流(post-confirm); 该函数负责推流延续的是 task 已处于
        running/completed 状态, SSE 已自然终止。
        """
        source = self._read_chat_workspace()
        for fn_name in [
            "async function handleConfirmSections",
            "async function handleRetryTask",
        ]:
            idx = source.find(fn_name)
            assert idx >= 0, f"{fn_name} not found"
            body = source[idx : idx + 2000]
            assert "connectTaskEvents" in body, (
                f"{fn_name} must still call connectTaskEvents to preserve "
                f"live SSE push"
            )

    def test_connect_task_events_removes_thinking_via_explicit_binding(self):
        """Phase 2.9A.23 fix-3: connectTaskEvents 接收 placeholderMessageId 参数,
        在 onAnyEvent 回调中使用该 ID 显式删除 thinking 占位消息。

        onAnyEvent 保证在所有业务过滤之前触发(解决 plan_step_started 早退问题)。
        废弃 fix-2 的 fallbackThinkingId Store 扫描模式。
        """
        source = self._read_chat_workspace()
        body = self._function_body(
            source,
            "function connectTaskEvents",
            "async function retryPendingConfirmation",
        )
        # 必须有显式 placeholderMessageId 参数(方案A)
        assert "placeholderMessageId" in body, (
            "connectTaskEvents must accept placeholderMessageId parameter "
            "for explicit taskId-messageId binding (fix-3 方法A)"
        )
        # 必须使用 onAnyEvent 而非 onAppendMessage 做删除
        assert "onAnyEvent" in body, (
            "connectTaskEvents must use onAnyEvent callback for thinking "
            "removal — onAppendMessage alone misses plan_step_started "
            "events due to early return in useTaskEvents"
        )
        # 必须使用 boundPlaceholderId 绑定参数(废弃 fallbackThinkingId)
        assert "boundPlaceholderId" in body, (
            "connectTaskEvents must use boundPlaceholderId derived from "
            "placeholderMessageId parameter, not from store scanning"
        )
        # 禁止 fallbackThinkingId store 扫描作为主路径
        assert "fallbackThinkingId" not in body, (
            "fallbackThinkingId store scan must be REMOVED in fix-3 "
            "(deprecated in favor of explicit placeholderMessageId binding)"
        )