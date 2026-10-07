"""Unit tests for token_estimator (F016)."""

from app.common.token_estimator import estimate_tokens


class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_none(self):
        assert estimate_tokens(None) == 0  # type: ignore[arg-type]

    def test_pure_english(self):
        # "hello world" = 11 chars → 11/4 ≈ 2
        result = estimate_tokens("hello world")
        assert 2 <= result <= 3

    def test_pure_chinese(self):
        # 10 Chinese chars → 10/1.5 ≈ 6
        result = estimate_tokens("一二三四五六七八九十")
        assert 6 <= result <= 7

    def test_mixed_text(self):
        text = "这是一个test混合message"
        result = estimate_tokens(text)
        assert result > 0

    def test_long_english(self):
        text = "a" * 400  # 400 chars → 100 tokens
        assert estimate_tokens(text) == 100

    def test_long_chinese(self):
        text = "你" * 150  # 150 chars → 100 tokens
        assert estimate_tokens(text) == 100
