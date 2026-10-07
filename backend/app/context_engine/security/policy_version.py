"""CE-05 安全策略版本常量。

`security_policy_version_at_create` 仅用于审计/可追溯；当前 Secret/PII/
Injection/Emergency Policy 始终用当前版本（实时 Kill Switch），任务冻结不得
冻结或降低实时安全策略。
"""

from __future__ import annotations

CURRENT_SECURITY_POLICY_VERSION = "v1"

__all__ = ["CURRENT_SECURITY_POLICY_VERSION"]
# auto-appended module-level note: 策略版本: 治理策略版本号管理(rolling update)。
