from __future__ import annotations

import pytest

from tests.help_test_support import write_help_fixture


def test_help_search_supports_chinese_and_weighted_title_matches(tmp_path):
    from app.services.help_center_service import HelpCenterService

    service = HelpCenterService(write_help_fixture(tmp_path))
    result = service.search("模板", limit=20)

    assert result.query == "模板"
    assert result.items[0].slug == "template-driven"
    assert "模板" in result.items[0].snippet
    assert result.items[0].score >= 60


def test_help_search_empty_query_and_limit(tmp_path):
    from app.services.help_center_service import HelpCenterService

    service = HelpCenterService(write_help_fixture(tmp_path))
    assert service.search("   ", limit=20).items == []
    assert len(service.search("测试", limit=1).items) == 1


def test_help_search_rejects_overlong_query(tmp_path):
    from app.services.help_center_service import HelpCenterService, HelpSearchError

    service = HelpCenterService(write_help_fixture(tmp_path))
    with pytest.raises(HelpSearchError):
        service.search("模" * 101, limit=20)
