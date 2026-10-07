"""Phase 2.6 — SensitiveFieldFilter masks api_key/token/password values to ``***``.

覆盖三大场景:
1. 字符串 message 中 ``key=value`` / ``"key": "value"`` 命中 → 替换。
2. record.args tuple / dict 中命中 → 替换。
3. 命中非敏感字段(普通 key=value) → 不动。
"""

from __future__ import annotations

import logging
import pytest

from app.core.logging import SensitiveFieldFilter, _redact, _is_sensitive_key


def _make_record(msg: object, args: object = ()) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )
    return rec


def test_redact_masks_key_equals_value() -> None:
    out = _redact("user logged in with api_key=ABC123XYZ")
    assert "ABC123XYZ" not in out
    assert "***" in out


def test_redact_masks_quoted_json_value() -> None:
    out = _redact('payload={"api_key": "sk-abc-123", "ok": 1}')
    assert "sk-abc-123" not in out
    assert "***" in out


def test_redact_masks_token_and_password() -> None:
    out = _redact("token=xyz password=pw dashscope_api_key=KEY")
    assert "xyz" not in out
    assert "pw" not in out
    assert "KEY" not in out


def test_redact_leaves_normal_keys_untouched() -> None:
    text = "user_id=42 name=alice status=ok"
    out = _redact(text)
    assert out == text


def test_redact_empty_string_returns_empty() -> None:
    assert _redact("") == ""


def test_filter_applies_to_record_msg_and_args_tuple() -> None:
    rec = _make_record("call api api_key=%s", ("SECRET",))
    f = SensitiveFieldFilter()
    f.filter(rec)
    assert "SECRET" not in str(rec.msg)
    assert "***" in str(rec.msg)


def test_filter_applies_to_dict_args() -> None:
    # Python LogRecord 在 args 为 (dict,) 时会自动 unwrap 到 dict;
    # 整个 dict 中 x 这条 value 内的 api_key=ABC 被替换为 ***,other=ok 不动
    rec = _make_record("payload=%(x)s", ({"x": "api_key=ABC, other=ok"},))
    f = SensitiveFieldFilter()
    f.filter(rec)
    assert rec.args["x"] == "api_key=***, other=ok"


def test_filter_dict_args_with_sensitive_key_masks_value() -> None:
    rec = _make_record("called", ({"api_key": "ABC", "name": "alice"},))
    f = SensitiveFieldFilter()
    f.filter(rec)
    assert rec.args["api_key"] == "<redacted>"
    assert rec.args["name"] == "alice"


def test_filter_handles_exception_internally() -> None:
    """filter 必须永远返回 True,任何异常被吞掉。"""

    class _Boom:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    rec = _make_record(_Boom(), ())
    f = SensitiveFieldFilter()
    assert f.filter(rec) is True


def test_is_sensitive_key_handles_hyphens_and_case() -> None:
    assert _is_sensitive_key("X-Api-Key") is True
    assert _is_sensitive_key("authorization") is True
    assert _is_sensitive_key("idempotency-key") is True
    assert _is_sensitive_key("totally_normal") is False