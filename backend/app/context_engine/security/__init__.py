"""CE-05 Context Security Service — 唯一安全入口。

计划 §3.4：现有 Injection / Redaction / Composer / Memory / Payload / Audit /
日志入口全部委托本 Service；不得保留两套独立规则。

子模块：
- pii.py：PII 检测/掩码（CONTEXT_PII_MODE ∈ redact|mask|block）
- injection.py：Prompt Injection 检测/中性化（委托 injection_filter）
- safe_excerpt.py：安全审计摘要（委托 composer redaction 规则）
- policy_version.py：安全策略版本常量
"""

from app.context_engine.security.injection import InjectionGuard
from app.context_engine.security.pii import PIIRedactor
from app.context_engine.security.policy_version import (
    CURRENT_SECURITY_POLICY_VERSION,
)
from app.context_engine.security.safe_excerpt import SafeExcerpt

__all__ = [
    "InjectionGuard",
    "PIIRedactor",
    "SafeExcerpt",
    "CURRENT_SECURITY_POLICY_VERSION",
]
# auto-appended module-level note: security 子包: 安全/隐私/注入防护入口。
