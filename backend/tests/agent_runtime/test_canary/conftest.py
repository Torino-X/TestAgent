"""Phase 2.7 — conftest for tests/agent_runtime/test_canary/."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running ``pytest tests/agent_runtime/test_canary`` directly;
# ensure backend's ``app`` package is importable.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# 确保 PYTEST_CURRENT_TEST 是真实的(exists 即可,get_canary_config() 才能用)
os.environ.setdefault("PYTEST_CURRENT_TEST", "phase_2_7_canary")
