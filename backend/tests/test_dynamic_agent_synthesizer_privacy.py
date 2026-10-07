from app.agent_runtime.dynamic_agent.synthesizer import DynamicSynthesizer


def test_synthesizer_filters_internal_runtime_status_from_user_answer() -> None:
    answer = DynamicSynthesizer().synthesize(
        {
            "analysis_results": {
                "step_4": (
                    "这是用户应该看到的分析结论。\n\n"
                    "注意：当前任务状态为 `running`，节点 `unknown`。"
                    "若需自动流转至下一节点，请指定具体分析产出物类型。"
                )
            }
        }
    )

    assert "这是用户应该看到的分析结论" in answer
    assert "当前任务状态" not in answer
    assert "unknown" not in answer
    assert "自动流转" not in answer
