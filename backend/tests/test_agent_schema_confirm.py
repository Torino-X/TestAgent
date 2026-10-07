from app.schemas.agent import ConfirmSectionsRequest


def test_confirm_sections_request_accepts_canonical_sections_payload() -> None:
    request = ConfirmSectionsRequest.model_validate(
        {"sections": [{"section_id": "sec-1", "action": "keep_template"}]}
    )

    assert request.sections[0].section_id == "sec-1"
    assert request.sections[0].action == "keep_template"


def test_confirm_sections_request_accepts_legacy_response_wrapped_payload() -> None:
    request = ConfirmSectionsRequest.model_validate(
        {
            "confirmation_type": "section_generation_config",
            "confirmation_id": "confirm-1",
            "response": {
                "sections": [
                    {"section_id": "sec-1", "action": "ai_generate"},
                ]
            },
        }
    )

    assert [section.section_id for section in request.sections] == ["sec-1"]
