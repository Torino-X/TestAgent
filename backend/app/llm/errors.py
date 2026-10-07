"""Exception types for the LLM task contract layer (F014).

Kept separate from ``app.core.exceptions.AppError`` because these are
**library-level** exceptions (raised inside parser adapters / thin
wrappers around ``LLMClient``); they are not user-facing API errors.
Callers convert them to user-facing messages via the ``fallback_text``
on the profile.

``LLMClientError`` from ``app.integrations.llm_client`` is intentionally
NOT a subclass of these — the two hierarchies stay independent so a
transport-layer error does not masquerade as a contract/parse error.
"""

from __future__ import annotations


class LLMProfileError(Exception):
    """Base error for the task-contract layer (registry / config / etc.)."""


class LLMProfileParseError(LLMProfileError):
    """Raised when an LLM response cannot be parsed for its profile.

    Triggered when ``LLMTaskProfile.on_parse_failure`` is
    ``LLMParseFailurePolicy.RAISE``.  For ``FALLBACK_DEFAULT`` the
    caller never sees this — the ``LLMClient.generate_with_profile``
    wrapper returns ``LLMProfileResult(success=False, ...)`` instead.
    """


class LLMProfileConfigError(LLMProfileError):
    """Raised when an ``LLMTaskProfile`` is malformed (e.g. unknown parser)."""# llm.errors:契约层异常(LLMProfileParseError / LLMProfileValidationError);库内异常,不与 AppError / LLMClientError 混用。
