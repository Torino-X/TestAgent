"""Source Adapter 共享辅助。

CE-02 整改七：adapter 的 owner-scope 查询必须使用**内部 user id**（DB BIGINT），
不能对 public user_id 字符串做 int()（会抛 ValueError → 被降级成错误分类，
而非真正的 owner-scope 过滤）。优先取 runtime_context.user_internal_id；
仅当测试注入的 runtime_context 无内部 id 时回退 int(request.user_id)。
"""

from __future__ import annotations


def user_internal_id(runtime_context, request) -> int:
    """解析内部 user id（DB BIGINT）。

    - 优先 ``runtime_context.user_internal_id``（已认证用户内部 id）；
    - 回退 ``int(request.user_id)``（纯逻辑测试的 mock context 无内部 id）。
    """
    internal = getattr(runtime_context, "user_internal_id", None)
    if internal is not None:
        return int(internal)
    return int(request.user_id)
# auto-appended module-level note: sources 内部 helpers。
