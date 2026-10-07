from __future__ import annotations

import pytest

from tests.help_test_support import write_help_fixture


def test_help_asset_resolution_allows_only_safe_assets(tmp_path):
    from app.services.help_center_service import HelpAssetNotFound, HelpCenterService

    service = HelpCenterService(write_help_fixture(tmp_path))
    asset = service.resolve_asset("diagram.png")
    assert asset.path.name == "diagram.png"
    assert asset.media_type == "image/png"

    with pytest.raises(HelpAssetNotFound):
        service.resolve_asset("../manifest.yaml")
    with pytest.raises(HelpAssetNotFound):
        service.resolve_asset("diagram.svg")
