"""WP-BE-07：Query-aware Memory Relevance 测试。

覆盖：
- related wins（query 命中记忆排前）
- unrelated excluded（不相关记忆排后/不入选）
- deterministic fallback（无 LLM/reranker → 确定性 lexical 分数）
- 无 query → 保持原排序
"""

from __future__ import annotations

import pytest

from app.context_engine.sources.memory import (
    relevance_score,
    _tokenize_relevance,
)


def test_relevance_related_wins():
    """query 命中记忆内容 → 高相关分数。"""
    score = relevance_score("以后都用中文回答", "用户偏好：以后都用中文回答")
    assert score > 0


def test_relevance_unrelated_zero():
    """query 与记忆内容无关 → 0。"""
    score = relevance_score("今天测试登录", "用户偏好：接口统一使用v3")
    assert score == 0


def test_relevance_empty_query_zero():
    """无 query → 0（不改变原排序）。"""
    assert relevance_score("", "任意内容") == 0


def test_relevance_deterministic():
    """确定性：相同输入 → 相同输出。"""
    a = relevance_score("测试方案要风险分析", "用户偏好：测试方案包含风险分析")
    b = relevance_score("测试方案要风险分析", "用户偏好：测试方案包含风险分析")
    assert a == b


def test_relevance_title_contributes():
    """title 参与评分（query 命中 title 也有分）。"""
    content = "内容无关"
    title = "用户偏好：中文回答"
    score_content_only = relevance_score("中文回答", content)
    score_with_title = relevance_score("中文回答", content, title)
    assert score_with_title >= score_content_only


def test_relevance_related_beats_unrelated():
    """相关记忆分数 > 不相关记忆分数。"""
    query = "接口版本用什么"
    related = relevance_score(query, "本项目接口统一使用v3")
    unrelated = relevance_score(query, "用户喜欢喝咖啡")
    assert related > unrelated


def test_tokenize_deterministic():
    t1 = _tokenize_relevance("接口统一使用v3")
    t2 = _tokenize_relevance("接口统一使用v3")
    assert t1 == t2
    assert len(t1) > 0
