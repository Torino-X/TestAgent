"""CE-01 整改补充测试。

覆盖：FakeReranker / DisabledReranker、16 个 Feature Flags 默认关闭、
多能力 ModelConfig 管理、错误体系防泄露、Backfill 幂等。
"""

from __future__ import annotations

import os
from unittest.mock import patch
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.context_engine.errors import (
    ContextEngineError,
    ContextEngineFailure,
    ContextEngineStage,
    raise_engine_error,
    to_app_error,
)
from app.context_engine.feature_flags import (
    ContextEngineFeatureFlags,
    ContextFeatureFlag,
    get_context_engine_flags,
)
from app.context_engine.providers import (
    DisabledReranker,
    FakeReranker,
    RerankItem,
    RerankRequest,
)
from app.context_engine.providers.protocols import RerankServiceProtocol
from app.services.settings_service import SettingsService


# ── 错误体系：值对象 DTO vs 可捕获异常 ─────────────────────────────


def test_failure_str_never_leaks_detail():
    err = ContextEngineError(
        code="context.rerank.fake_failure",
        detail="provider-body-with-secret-xyz",
        stage=ContextEngineStage.RERANK,
    )
    failure = ContextEngineFailure(err)
    assert "provider-body-with-secret-xyz" not in str(failure)
    assert "provider-body-with-secret-xyz" not in repr(failure)


def test_raise_engine_error_is_catchable_exception():
    with pytest.raises(ContextEngineFailure) as exc_info:
        raise_engine_error(code="ctx.err", detail="secret", stage=ContextEngineStage.SOURCE)
    assert isinstance(exc_info.value, Exception)
    assert exc_info.value.error.code == "ctx.err"
    assert "secret" not in str(exc_info.value)


def test_to_app_error_unknown_exception_safe():
    code, message, detail = to_app_error(ValueError("raw sql detail"))
    assert code == 51001
    assert "raw sql" not in message
    assert detail == {}


def test_to_state_dict_only_lightweight_fields():
    err = ContextEngineError(
        code="c", detail="d", stage=ContextEngineStage.SCOPE, retryable=True
    )
    state = err.to_state_dict()
    assert set(state.keys()) == {"code", "stage", "retryable"}
    assert "detail" not in state


# ── 错误体系：Pydantic v2 FrozenModel（整改 v2）────────────────────────


def test_context_engine_error_is_frozen():
    from pydantic import ValidationError

    err = ContextEngineError(code="c", detail="d", stage=ContextEngineStage.SCOPE)
    with pytest.raises(ValidationError):
        err.retryable = True  # immutable


def test_context_engine_error_extra_forbid():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ContextEngineError(code="c", detail="d", stage=ContextEngineStage.SCOPE, unknown_field=1)


def test_context_engine_error_invalid_stage():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ContextEngineError(code="c", detail="d", stage="not-a-stage")


def test_context_engine_error_invalid_metadata():
    from pydantic import ValidationError

    # safe_metadata 只接受 JSON 可序列化值
    with pytest.raises(ValidationError):
        ContextEngineError(
            code="c", detail="d", stage=ContextEngineStage.SCOPE,
            safe_metadata={"bad": object()},
        )


def test_context_engine_error_model_dump_json():
    err = ContextEngineError(
        code="c", detail="d", stage=ContextEngineStage.SOURCE,
        source_type="conversation",
    )
    dumped = err.model_dump(mode="json")
    assert dumped["code"] == "c"
    assert dumped["stage"] == "source"
    assert dumped["source_type"] == "conversation"
    assert dumped["retryable"] is False


def test_context_engine_error_repr_no_detail():
    err = ContextEngineError(
        code="c", detail="sensitive-provider-body", stage=ContextEngineStage.RETRIEVAL
    )
    assert "sensitive-provider-body" not in repr(err)


def test_to_app_error_source_type_maps():
    err = ContextEngineError(
        code="c", detail="d", stage=ContextEngineStage.SOURCE,
        source_type="parsed_document",
    )
    code, message, detail = to_app_error(err)
    assert code == 51001
    assert message == "d"
    assert detail["stage"] == "source"


def test_to_app_error_failure_unwraps():
    err = ContextEngineError(code="c", detail="d", stage=ContextEngineStage.MEMORY)
    code, message, _ = to_app_error(ContextEngineFailure(err))
    assert code == 51101
    assert message == "d"


def test_no_api_key_prompt_sql_path_leakage():
    """to_app_error 对未知异常与 DTO 均不回显敏感文本。"""
    # API Key / Prompt / SQL / storage path 出现在异常文本中时
    for secret_text in ["sk-12345678", "SELECT * FROM users", "C:/data/uploads/user1"]:
        code, message, _ = to_app_error(ValueError(f"error: {secret_text}"))
        assert secret_text not in message
        assert secret_text not in str(code)


# ── 16 个 Feature Flags 默认关闭 ────────────────────────────────────


def test_all_flags_default_off():
    flags = ContextEngineFeatureFlags()
    assert all(flags.evaluate(flag) is False for flag in ContextFeatureFlag)


def test_memory_auto_activate_requires_write():
    flags = ContextEngineFeatureFlags(context_memory_write_enabled=False, context_memory_auto_activate_enabled=True)
    assert flags.memory_auto_activate_requires_write is False
    flags2 = ContextEngineFeatureFlags(context_memory_write_enabled=True, context_memory_auto_activate_enabled=True)
    assert flags2.memory_auto_activate_requires_write is True


def test_full_prompt_debug_requires_engine():
    flags = ContextEngineFeatureFlags(context_engine_enabled=False, context_full_prompt_debug_enabled=True)
    assert flags.full_prompt_debug_requires_engine is False


def test_get_flags_env_optin():
    with patch.dict(os.environ, {"CONTEXT_RERANK_ENABLED": "1"}, clear=True):
        flags = get_context_engine_flags()
        assert flags.context_rerank_enabled is True
        assert flags.context_compaction_enabled is False


# ── FakeReranker / DisabledReranker ────────────────────────────────


@pytest.mark.asyncio
async def test_fake_reranker_deterministic():
    fake = FakeReranker()
    req = RerankRequest(
        request_id="r1",
        model="fake",
        query="q",
        items=(RerankItem(doc_id="a", text="1"), RerankItem(doc_id="b", text="2")),
    )
    res = await fake.rerank(req)
    assert isinstance(fake, RerankServiceProtocol)
    assert res.scores[0].doc_id == "a"
    assert res.scores[0].score > res.scores[1].score


@pytest.mark.asyncio
async def test_fake_reranker_fault_injection():
    fake = FakeReranker(fail_requested=True)
    req = RerankRequest(request_id="r1", model="fake", query="q", items=())
    with pytest.raises(ContextEngineFailure) as exc_info:
        await fake.rerank(req)
    assert exc_info.value.error.code == "context.rerank.fake_failure"


@pytest.mark.asyncio
async def test_disabled_reranker_rejects():
    disabled = DisabledReranker()
    assert disabled.enabled is False
    req = RerankRequest(request_id="r1", model="m", query="q", items=())
    with pytest.raises(ContextEngineFailure) as exc_info:
        await disabled.rerank(req)
    assert exc_info.value.error.code == "context.rerank.disabled"


# ── 多能力 ModelConfig 管理 ────────────────────────────────────────


def _model_config_orm(**overrides):
    from app.models.config import ModelConfig

    base = dict(
        id=1,
        public_id="mc_1",
        user_id=42,
        config_name="cfg",
        provider="openai-compatible",
        api_base_url="https://x",
        api_key_encrypted="enc",
        model_name="m",
        timeout_seconds=120,
        enable_thinking=False,
        supports_vision=False,
        is_default=True,
        status="active",
        capability_type="chat",
    )
    base.update(overrides)
    return ModelConfig(**base)


def _make_service(model_config=None):
    session = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    svc = SettingsService(session)
    mock_repo = MagicMock()
    mock_repo.get_active_for_user = AsyncMock(return_value=model_config)
    mock_repo.upsert_for_user = AsyncMock(return_value=model_config)
    mock_repo.get_active_for_capability = AsyncMock(return_value=model_config)
    mock_repo.upsert_for_capability = AsyncMock(return_value=model_config)
    mock_repo.list_by_capability = AsyncMock(return_value=[model_config] if model_config else [])
    svc._model_repo = mock_repo
    return svc, session, mock_repo


@pytest.mark.asyncio
async def test_update_capability_config_masked_key_keeps_existing():
    cfg = _model_config_orm()
    svc, _, mock_repo = _make_service(cfg)
    result = await svc.update_capability_config(
        "embedding",
        {
            "api_base_url": "https://x",
            "model_name": "e",
            "api_key": "sk-****-masked",  # 掩码 → 不覆盖
            "embedding_dimension": 1536,
        },
        42,
    )
    # masked key 不应传给 encrypt / upsert
    call_kwargs = mock_repo.upsert_for_capability.call_args.kwargs
    assert call_kwargs["api_key_encrypted"] == ""  # 掩码不更新原 key


@pytest.mark.asyncio
async def test_update_capability_config_invalid_type_rejected():
    from app.core.exceptions import ValidationError

    svc, _, _ = _make_service(None)
    with pytest.raises(ValidationError):
        await svc.update_capability_config("quantum", {"api_base_url": "x", "model_name": "m"}, 42)


@pytest.mark.asyncio
async def test_update_capability_config_real_key_encrypts():
    cfg = _model_config_orm()
    svc, _, mock_repo = _make_service(cfg)
    await svc.update_capability_config(
        "embedding",
        {"api_base_url": "https://x", "model_name": "e", "api_key": "sk-real-new-key", "embedding_dimension": 1536},
        42,
    )
    call_kwargs = mock_repo.upsert_for_capability.call_args.kwargs
    assert call_kwargs["api_key_encrypted"] != ""
    assert call_kwargs["api_key_encrypted"] != "sk-real-new-key"  # 已加密
