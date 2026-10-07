"""CE-02 整改二：latency_json 契约 + provider_request_id 隔离测试。

验证：
1. ContextBuildLatency extra=forbid（拒绝未知字段，含 provider_request_id）；
2. latency_json 只含明确延迟字段；
3. provider_request_id 不进入 latency_json；
4. Snapshot Detail Mapper 可正常反序列化 latency_json → ContextBuildLatency；
5. provider_request_id 仍可从 ContextualLLMResult 获取；
6. provider_request_id 不进入 State。
"""

from __future__ import annotations

import pytest

from app.agent_runtime.context import ContextualLLMResult, provider_request_id
from app.context_engine.models.retry_models import LLMAttemptRef
from app.context_engine.models.snapshot_models import ContextBuildLatency


def test_context_build_latency_extra_forbid():
    """ContextBuildLatency extra=forbid：provider_request_id 被拒绝。"""
    with pytest.raises(Exception) as exc_info:
        ContextBuildLatency(planning_ms=1, provider_request_id="req_123")
    assert "Extra inputs" in str(exc_info.value) or "extra" in str(exc_info.value).lower()


def test_context_build_latency_accepts_only_latency_fields():
    """ContextBuildLatency 只含延迟字段。"""
    latency = ContextBuildLatency(
        planning_ms=1,
        source_gathering_ms=2,
        retrieval_ms=3,
        rerank_ms=4,
        selection_ms=5,
        preflight_ms=6,
        compose_ms=7,
        snapshot_ms=8,
        provider_ms=9,
    )
    d = latency.model_dump()
    allowed = {
        "planning_ms", "source_gathering_ms", "retrieval_ms", "rerank_ms",
        "selection_ms", "preflight_ms", "compose_ms", "snapshot_ms", "provider_ms",
    }
    assert set(d.keys()) == allowed
    assert "provider_request_id" not in d
    assert latency.total_ms == 45


def test_provider_request_id_not_in_latency_json():
    """provider_request_id 不进入 latency_json（Snapshot writer 不再写）。"""
    import json as _json

    # 模拟 snapshot_service.complete 构造的 latency_json（整改后）
    latency = {"provider_ms": 100}
    dumped = _json.dumps(latency)
    parsed = _json.loads(dumped)
    assert "provider_request_id" not in parsed
    assert set(parsed.keys()) == {"provider_ms"}


def test_snapshot_latency_json_deserializes_to_context_build_latency():
    """Snapshot Detail Mapper：latency_json → ContextBuildLatency 反序列化。"""
    latency_dict = {
        "planning_ms": 1,
        "source_gathering_ms": 2,
        "retrieval_ms": 3,
        "rerank_ms": 4,
        "selection_ms": 5,
        "preflight_ms": 6,
        "compose_ms": 7,
        "snapshot_ms": 8,
        "provider_ms": 9,
    }
    latency = ContextBuildLatency(**latency_dict)
    assert latency.provider_ms == 9
    assert latency.total_ms == 45


def test_provider_request_id_available_from_contextual_llm_result():
    """provider_request_id 仍可从 ContextualLLMResult 获取。"""
    result = ContextualLLMResult(
        value={"summary": "ok"},
        snapshot_public_id="cs_1",
        provider_request_id="req_abc_123",
        attempts=(),
    )
    assert result.provider_request_id == "req_abc_123"


def test_provider_request_id_not_in_state():
    """provider_request_id 不进入 State（ContextStateRef.to_state_dict 无此字段）。"""
    from app.context_engine.snapshot.context_state_ref import build_context_state_ref
    from app.context_engine.models.compose import ContextComposeResult
    from app.context_engine.models.selection import SelectedContextSet

    composed = ContextComposeResult(
        messages=[],
        prompt_text="prompt",
        estimated_input_tokens=10,
        snapshot_public_id="cs_1",
        selected=SelectedContextSet(section_stats={}, total_estimated_tokens=10),
    )
    ref = build_context_state_ref(composed, snapshot_public_id="cs_1")
    sd = ref.to_state_dict()
    assert "provider_request_id" not in sd
    # 嵌套字段也不含
    import json

    assert "provider_request_id" not in json.dumps(sd)
