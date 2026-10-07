"""Dev-only fault injection package.

This package is intentionally outside ``app`` so it can be deleted as one unit
before release. Business modules should import only
``app.fault_injection_gateway``.
"""

from __future__ import annotations

__all__ = ["maybe_inject"]

from .engine import maybe_inject
