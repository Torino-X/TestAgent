"""Phase 2.9A.30: payload_json normalization tests.

Tests for normalize_json_object() and its integration with message_service.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


# ──────────────────────────────────────────────────────────────────────
# 1. normalize_json_object — unit tests
# ──────────────────────────────────────────────────────────────────────


def test_normalize_json_object_dict_returns_copy():
    from app.common.json_utils import normalize_json_object

    original = {"key": "value"}
    result = normalize_json_object(original)
    assert result == {"key": "value"}
    assert result is not original  # shallow copy


def test_normalize_json_object_json_string_parses():
    from app.common.json_utils import normalize_json_object

    payload_str = json.dumps({"attached_file_ids": ["file_001", "file_002"]})
    result = normalize_json_object(payload_str)
    assert result == {"attached_file_ids": ["file_001", "file_002"]}


def test_normalize_json_object_none_returns_empty():
    from app.common.json_utils import normalize_json_object

    assert normalize_json_object(None) == {}


def test_normalize_json_object_empty_string_returns_empty():
    from app.common.json_utils import normalize_json_object

    assert normalize_json_object("") == {}
    assert normalize_json_object("   ") == {}


def test_normalize_json_object_invalid_json_returns_empty():
    from app.common.json_utils import normalize_json_object

    assert normalize_json_object("{invalid json}") == {}
    assert normalize_json_object("not json at all") == {}


def test_normalize_json_object_non_dict_json_returns_empty():
    from app.common.json_utils import normalize_json_object

    # JSON array, not object
    assert normalize_json_object("[1, 2, 3]") == {}
    # JSON string
    assert normalize_json_object('"just a string"') == {}


# ──────────────────────────────────────────────────────────────────────
# 2. _attached_file_ids — JSON string payload
# ──────────────────────────────────────────────────────────────────────


def test_attached_file_ids_from_json_string_payload():
    """When payload_json is a JSON string, _attached_file_ids should still extract IDs."""
    from app.services.message_service import MessageService

    msg = SimpleNamespace(
        payload_json=json.dumps({"attached_file_ids": ["file_req_001", "file_req_002"]})
    )
    ids = MessageService._attached_file_ids(msg)
    assert ids == ["file_req_001", "file_req_002"]


def test_attached_file_ids_from_dict_payload():
    """When payload_json is a dict, _attached_file_ids works normally."""
    from app.services.message_service import MessageService

    msg = SimpleNamespace(
        payload_json={"attached_file_ids": ["file_001"]}
    )
    ids = MessageService._attached_file_ids(msg)
    assert ids == ["file_001"]


def test_attached_file_ids_from_none_payload():
    from app.services.message_service import MessageService

    msg = SimpleNamespace(payload_json=None)
    ids = MessageService._attached_file_ids(msg)
    assert ids == []


def test_attached_file_ids_preserves_order():
    """attached_file_ids order must be preserved in the result."""
    from app.services.message_service import MessageService

    file_ids = [f"file_{i:03d}" for i in range(10)]
    msg = SimpleNamespace(
        payload_json=json.dumps({"attached_file_ids": file_ids})
    )
    ids = MessageService._attached_file_ids(msg)
    assert ids == file_ids


# ──────────────────────────────────────────────────────────────────────
# 3. _to_detail — payload normalization
# ──────────────────────────────────────────────────────────────────────


def test_to_detail_payload_is_dict_not_string():
    """_to_detail must return payload as a dict, not a JSON string."""
    from app.services.message_service import MessageService
    from datetime import datetime

    msg = SimpleNamespace(
        public_id="msg_001",
        role="user",
        message_type="user_text",
        content="hello",
        payload_json=json.dumps({"attached_file_ids": ["f1"]}),
        created_at=datetime(2026, 7, 29, 10, 0, 0),
        conversation_sequence=1,
        reply_to_message_id=None,
    )
    detail = MessageService._to_detail(msg, "conv_001", None)
    assert isinstance(detail["payload"], dict)
    assert detail["payload"]["attached_file_ids"] == ["f1"]


def test_to_detail_attached_files_from_string_payload():
    """When payload_json is a string, attached_files should still be populated."""
    from app.services.message_service import MessageService
    from datetime import datetime

    file = SimpleNamespace(
        public_id="file_001",
        original_name="test.docx",
        file_ext="docx",
        file_size=1024,
        file_type="requirement_doc",
        file_status="uploaded",
        upload_status="uploaded",
    )
    msg = SimpleNamespace(
        public_id="msg_001",
        role="user",
        message_type="user_text",
        content="hello",
        payload_json=json.dumps({"attached_file_ids": ["file_001"]}),
        created_at=datetime(2026, 7, 29, 10, 0, 0),
        conversation_sequence=1,
        reply_to_message_id=None,
    )
    detail = MessageService._to_detail(msg, "conv_001", [file])
    assert len(detail["attached_files"]) == 1
    assert detail["attached_files"][0]["file_id"] == "file_001"


def test_to_detail_missing_file_safe_skip():
    """Missing file_id in attached_files should not cause failure."""
    from app.services.message_service import MessageService
    from datetime import datetime

    file = SimpleNamespace(
        public_id="file_001",
        original_name="test.docx",
        file_ext="docx",
        file_size=1024,
        file_type="requirement_doc",
        file_status="uploaded",
        upload_status="uploaded",
    )
    msg = SimpleNamespace(
        public_id="msg_001",
        role="user",
        message_type="user_text",
        content="hello",
        payload_json=json.dumps({"attached_file_ids": ["file_999"]}),  # file_999 doesn't exist
        created_at=datetime(2026, 7, 29, 10, 0, 0),
        conversation_sequence=1,
        reply_to_message_id=None,
    )
    # Should not raise, just return empty attached_files
    detail = MessageService._to_detail(msg, "conv_001", [file])
    assert detail["attached_files"] == []
