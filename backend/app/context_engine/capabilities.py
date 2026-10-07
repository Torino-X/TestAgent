"""Truthful product exposure status for optional Context Engine capabilities."""

from __future__ import annotations

from typing import Final, Literal

CapabilityStatus = Literal["production", "internal_only"]

CONTEXT_ENGINE_PRODUCT_CAPABILITIES: Final[dict[str, CapabilityStatus]] = {
    "compose": "production",
    "retrieval": "production",
    "tool_output_governance": "production",
    # Implementations exist for isolated validation, but no production request
    # path may advertise them until their ACL/lifecycle contracts are complete.
    "shadow": "internal_only",
    "rehydrate": "internal_only",
}

__all__ = ["CONTEXT_ENGINE_PRODUCT_CAPABILITIES", "CapabilityStatus"]
