"""F020 — VisionService parser tests."""

from __future__ import annotations

from app.services.vision_service import VisionResult, VisionService


def test_parses_strict_json():
    raw = '{"image_type":"流程图","summary":"登录流程","visible_text":"submit","business_flow":"点登录→校验→首页","ui_elements":["按钮"],"data_fields":["userId"],"test_points":["空密码"],"uncertainties":[]}'
    result = VisionService._parse_to_vision_result(raw, image_index=7, source="登录章节")
    assert result.ok is True
    assert result.image_type == "流程图"
    assert result.summary == "登录流程"
    assert result.business_flow.endswith("首页")
    assert result.ui_elements == ["按钮"]
    assert result.data_fields == ["userId"]
    assert result.test_points == ["空密码"]


def test_vision_result_keeps_non_snapshot_evidence_audit_metadata():
    raw = '{"image_type":"UI","summary":"s","visible_text":"","business_flow":"","ui_elements":[],"data_fields":[],"test_points":[],"uncertainties":[]}'
    result = VisionService._parse_to_vision_result(
        raw,
        image_index=3,
        source="requirements",
        audit_metadata={
            "evidence_contract_version": "vision_evidence:v1",
            "snapshot_mode": "not_applicable",
        },
    )

    assert result.ok is True
    assert result.audit_metadata["evidence_contract_version"] == "vision_evidence:v1"
    assert result.audit_metadata["snapshot_mode"] == "not_applicable"


def test_parses_json_with_surrounding_text():
    raw = '好的，这是分析结果：\n{"image_type":"状态机","summary":"订单状态机","visible_text":"","business_flow":"待支付→已支付→已完成","ui_elements":[],"data_fields":[],"test_points":[],"uncertainties":[]}\n完毕。'
    result = VisionService._parse_to_vision_result(raw, image_index=2, source="x")
    assert result.ok is True
    assert result.image_type == "状态机"
    assert result.business_flow.startswith("待支付")


def test_falls_back_to_error_envelope_on_invalid_json():
    raw = "抱歉，我无法识别这张图片。"
    result = VisionService._parse_to_vision_result(raw, image_index=1, source="?")
    assert not result.ok
    assert "vision response is not valid JSON" in result.error
    assert result.summary == ""


def test_empty_response_produces_error():
    result = VisionService._parse_to_vision_result("", image_index=1, source="?")
    assert not result.ok
    assert "empty" in result.error.lower()


def test_prompt_text_contains_all_structured_sections():
    result = VisionResult(
        image_type="UI 原型",
        summary="登录页原型",
        visible_text="手机号",
        business_flow="输入→登录",
        ui_elements=["输入框", "登录按钮"],
        data_fields=["mobile"],
        test_points=["格式校验"],
        uncertainties=["埋点待确认"],
    )
    prompt = result.to_prompt_text(image_index=5, source="登录页")
    assert "【图片 5 理解结果】" in prompt
    assert "UI 原型" in prompt
    assert "登录页原型" in prompt
    assert "输入框；登录按钮" in prompt
    assert "mobile" in prompt
    assert "格式校验" in prompt
    assert "埋点待确认" in prompt


def test_prompt_text_handles_error_state():
    result = VisionResult(error="vision_model_failed: timeout")
    prompt = result.to_prompt_text(image_index=9)
    assert "【图片 9 理解失败】" in prompt
    assert "vision_model_failed: timeout" in prompt
