from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
    _needs_generation_recovery,
)


def test_only_recoverable_json_parser_errors_enter_generation_recovery() -> None:
    assert _needs_generation_recovery({
        "success": False,
        "error": {"code": "JSON_VALIDATION_FAILED", "recoverable": True},
    })
    assert _needs_generation_recovery({
        "success": False,
        "error": {
            "code": "MIGRATION_INVOKER_EXCEPTION:LLMProfileParseError",
            "recoverable": True,
        },
    })


def test_authentication_and_nonrecoverable_errors_do_not_enter_generation_recovery() -> None:
    assert not _needs_generation_recovery({
        "success": False,
        "error": {"code": "MODEL_CONFIG_ERROR", "recoverable": True},
    })
    assert not _needs_generation_recovery({
        "success": False,
        "error": {"code": "JSON_VALIDATION_FAILED", "recoverable": False},
    })
