from __future__ import annotations

import re

import pytest

from tests.help_test_support import write_help_fixture


def test_help_manifest_loads_sorted_catalog(tmp_path):
    from app.services.help_center_service import HelpCenterService

    catalog = HelpCenterService(write_help_fixture(tmp_path)).get_catalog()
    assert catalog.site.title == "帮助中心"
    assert [section.id for section in catalog.sections] == ["getting-started", "concepts"]
    assert catalog.sections[0].articles[0].reading_time_minutes == 1


def test_help_manifest_rejects_duplicate_slugs(tmp_path):
    from app.services.help_center_service import HelpCenterService, HelpManifestError

    root = write_help_fixture(tmp_path)
    manifest = (root / "manifest.yaml").read_text(encoding="utf-8")
    manifest = manifest.replace("slug: template-driven", "slug: quick-start")
    (root / "manifest.yaml").write_text(manifest, encoding="utf-8")

    with pytest.raises(HelpManifestError, match="duplicate slug"):
        HelpCenterService(root).get_catalog()


def test_packaged_help_catalog_contains_required_launch_content():
    from app.services.help_center_service import HelpCenterService

    catalog = HelpCenterService().get_catalog()
    section_ids = [section.id for section in catalog.sections]
    slugs = {
        article.slug
        for section in catalog.sections
        for article in section.articles
    }

    assert catalog.site.title == "TestAgent 帮助中心"
    assert section_ids == [
        "getting-started",
        "concepts",
        "test-plan",
        "templates",
        "projects",
        "library",
        "knowledge",
        "best-practices",
        "faq",
    ]
    assert len(slugs) == 36
    assert {
        "quick-start",
        "prepare-requirement",
        "template-driven-generation",
        "automatic-review-repair",
        "template-design",
        "project-sources",
        "use-knowledge",
        "how-generation-works",
        "faq",
    } <= slugs

    linked_slugs: set[str] = set()
    service = HelpCenterService()
    for article_slug in slugs:
        article = service.get_article(article_slug)
        linked_slugs.update(re.findall(r"/help/article/([a-z0-9-]+)", article.markdown))
    assert linked_slugs <= slugs


@pytest.mark.parametrize(
    "replacement, expected",
    [
        ("../outside.md", "path escapes"),
        ("articles/missing.md", "does not exist"),
        ("articles/template.txt", "must be a .md"),
    ],
)
def test_help_manifest_rejects_unsafe_or_missing_article_files(tmp_path, replacement, expected):
    from app.services.help_center_service import HelpCenterService, HelpManifestError

    root = write_help_fixture(tmp_path)
    manifest = (root / "manifest.yaml").read_text(encoding="utf-8")
    manifest = manifest.replace("articles/template.md", replacement)
    (root / "manifest.yaml").write_text(manifest, encoding="utf-8")

    with pytest.raises(HelpManifestError, match=expected):
        HelpCenterService(root).get_catalog()
