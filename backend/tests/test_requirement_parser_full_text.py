from app.tools.requirement_parser_tool import RequirementParserTool


def test_long_text_policy_keeps_the_complete_parsed_requirement():
    source = "HEAD-REQ\n" + ("business detail\n" * 30) + "TAIL-REQ"

    retained, warnings = RequirementParserTool._apply_text_length_policy(
        source,
        warning_threshold=40,
    )

    assert retained == source
    assert retained.endswith("TAIL-REQ")
    assert warnings == [
        "需求文档较长，已保留完整解析正文；生成前将按完整覆盖清单分块提取。"
    ]
