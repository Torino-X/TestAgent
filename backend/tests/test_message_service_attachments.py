from types import SimpleNamespace

from app.services.message_service import MessageService


def make_file(public_id="file_req_001"):
    return SimpleNamespace(
        public_id=public_id,
        original_name="需求文档.docx",
        file_ext=".docx",
        file_size=204800,
        file_type="requirement_doc",
        upload_status="uploaded",
    )


def make_message(attached_file_ids=None):
    return SimpleNamespace(
        public_id="msg_001",
        role="user",
        message_type="user_text",
        content="根据这些文件生成测试方案",
        payload_json={"attached_file_ids": attached_file_ids or []},
        created_at=None,
    )


def test_message_detail_includes_attached_files_from_payload():
    detail = MessageService._to_detail(make_message(["file_req_001"]), "conv_001", [make_file()])

    assert detail["attached_files"] == [
        {
            "file_id": "file_req_001",
            "original_name": "需求文档.docx",
            "file_ext": ".docx",
            "file_size": 204800,
            "file_type": "requirement_doc",
            "upload_status": "uploaded",
        }
    ]


def test_message_detail_does_not_attach_unrelated_conversation_files():
    detail = MessageService._to_detail(make_message(["file_req_001"]), "conv_001", [make_file("file_other")])

    assert detail["attached_files"] == []
