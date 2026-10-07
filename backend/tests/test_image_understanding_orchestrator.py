"""F020 — orchestrator tests covering the three vision states + graceful degradation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.common.document_reader import RequirementBlock
from app.services.image_understanding_orchestrator import (
    ImageBlockResult,
    process_images,
)
from app.services.vision_service import VisionResult


def _img(idx: int, name: str = "image.png") -> RequirementBlock:
    return RequirementBlock(
        index=idx,
        block_type="image",
        text="",
        source=f"Section {idx}",
        image_path=Path(f"/tmp/{name}"),
    )


def _text(idx: int, text: str = "普通段落文字") -> RequirementBlock:
    return RequirementBlock(
        index=idx,
        block_type="text",
        text=text,
        source=f"Section {idx}",
    )


@pytest.mark.asyncio
async def test_ocr_only_when_no_provider():
    blocks = [_text(0, "介绍"), _img(1), _img(2), _text(3, "结尾")]

    with patch(
        "app.services.image_understanding_orchestrator._safe_ocr",
        side_effect=lambda p: f"OCR-for-{p.name}",
    ) as ocr_mock:
        results = await process_images(blocks, img_config_provider=None)

    assert ocr_mock.await_count == 2
    assert set(results.keys()) == {1, 2}
    assert all(isinstance(r, ImageBlockResult) for r in results.values())
    assert all(r.vision is None for r in results.values())
    assert all(r.vision_error == "" for r in results.values())
    assert results[1].ocr_text == "OCR-for-image.png"
    assert "OCR 识别文字" in results[1].to_prompt_text()
    # The image-text block must interleave with surrounding text:
    assert "【图片 1】" in results[1].to_prompt_text()


@pytest.mark.asyncio
async def test_vision_invoked_only_when_enabled():
    blocks = [_img(1)]
    provider_enabled = SimpleNamespace(
        api_url="http://x", api_key="k", model_name="qwen-vl-plus",
        timeout=30, enable_in_doc_parsing=True,
    )

    vision_result = VisionResult(
        image_type="flowchart",
        summary="login flow",
        business_flow="submit->validate->pay",
        visible_text="submit validate pay",
        ui_elements=["button"], data_fields=["orderId"], test_points=["boundary"],
    )

    call_state = {"n": 0}

    async def fake_analyze(self, **kwargs):
        call_state["n"] += 1
        return vision_result

    with patch(
        "app.services.image_understanding_orchestrator._safe_ocr",
        return_value="ocr-fallback",
    ), patch.object(
        __import__("app.services.image_understanding_orchestrator", fromlist=["VisionService"]).VisionService,
        "analyze_image",
        new=fake_analyze,
    ):
        results = await process_images(blocks, img_config_provider=provider_enabled)

    assert call_state["n"] == 1
    assert results[1].vision is vision_result
    assert results[1].vision_error == ""
    prompt = results[1].to_prompt_text()
    assert "【图片 1 理解结果】" in prompt
    assert "login flow" in prompt


@pytest.mark.asyncio
async def test_provider_configured_but_disabled_skips_vision():
    blocks = [_img(1)]
    provider_disabled = SimpleNamespace(
        api_url="http://x", api_key="k", model_name="qwen-vl-plus",
        timeout=30, enable_in_doc_parsing=False,
    )

    with patch(
        "app.services.image_understanding_orchestrator._safe_ocr",
        return_value="only-ocr",
    ) as ocr_mock:
        results = await process_images(blocks, img_config_provider=provider_disabled)

    assert ocr_mock.await_count == 1
    assert results[1].vision is None
    assert "only-ocr" in results[1].to_prompt_text()


@pytest.mark.asyncio
async def test_vision_failure_falls_back_to_ocr_only():
    blocks = [_img(1), _img(2)]
    provider = SimpleNamespace(
        api_url="http://x", api_key="k", model_name="qwen-vl-plus",
        timeout=30, enable_in_doc_parsing=True,
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("upstream 503")

    with patch(
        "app.services.image_understanding_orchestrator._safe_ocr",
        side_effect=lambda p: f"OCR-for-{p.name}",
    ), patch.object(
        __import__("app.services.image_understanding_orchestrator", fromlist=["VisionService"]).VisionService,
        "analyze_image",
        new=boom,
    ):
        results = await process_images(blocks, img_config_provider=provider)

    # All blocks produce a result, none crash the orchestrator.
    assert set(results.keys()) == {1, 2}
    for r in results.values():
        assert r.vision is None
        assert "upstream 503" in r.vision_error
        assert r.ocr_text.startswith("OCR-for-")
        assert "图片理解失败" in r.to_prompt_text()


@pytest.mark.asyncio
async def test_concurrent_ocr_over_many_images():
    blocks = [_img(i) for i in range(1, 6)]

    real_gather = asyncio.gather

    async def fake_gather(*aws, **kwargs):
        # Real concurrent dispatch; just verify multiple tasks run.
        return await real_gather(*aws)

    call_count = {"n": 0}

    async def slow_ocr(image_path: Path) -> str:
        call_count["n"] += 1
        await asyncio.sleep(0)
        return f"ocr-{image_path.name}-{call_count['n']}"

    with patch("asyncio.gather", new=fake_gather), patch(
        "app.services.image_understanding_orchestrator._safe_ocr",
        side_effect=slow_ocr,
    ):
        results = await process_images(blocks, img_config_provider=None)

    assert call_count["n"] == 5
    assert len(results) == 5
    assert all(r.ocr_text.startswith("ocr-image.png-") for r in results.values())


@pytest.mark.asyncio
async def test_no_image_blocks_returns_empty():
    blocks = [_text(0, "intro"), _text(1, "body")]
    results = await process_images(blocks, img_config_provider=None)
    assert results == {}


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner
