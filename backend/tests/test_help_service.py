from __future__ import annotations

import pytest

from tests.help_test_support import write_help_fixture


def test_help_article_returns_stable_headings_and_neighbors(tmp_path):
    from app.services.help_center_service import HelpCenterService

    service = HelpCenterService(write_help_fixture(tmp_path))
    first = service.get_article("quick-start")
    second = service.get_article("template-driven")

    assert [heading.anchor for heading in first.headings] == ["准备文件", "准备文件-2"]
    assert first.previous is None
    assert first.next.slug == "template-driven"
    assert second.previous.slug == "quick-start"
    assert second.next is None
    assert first.reading_time_minutes == 1


def test_help_article_missing_slug_is_not_found(tmp_path):
    from app.services.help_center_service import HelpArticleNotFound, HelpCenterService

    service = HelpCenterService(write_help_fixture(tmp_path))
    with pytest.raises(HelpArticleNotFound):
        service.get_article("missing")
