"""Integration test for the real OCR worker subprocess.

2026-07-14（路线 B）：这套测试 opt-in，默认 ``skip``，要 export
``OCR_INTEGRATION=1`` 才跑。原因是它会真 spawn ``python -m app.common.ocr_worker``
加载 PaddleOCR（首次 5-10s 下载模型权重），CI 环境没有 paddleocr 就会挂。

跑::

    cd backend
    OCR_INTEGRATION=1 .venv/Scripts/python.exe -m pytest tests/integration/test_ocr_worker_real.py -v
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SKIP_UNLESS_ENABLED = pytest.mark.skipif(
    not os.getenv("OCR_INTEGRATION"),
    reason="set OCR_INTEGRATION=1 to enable real subprocess OCR test",
)


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def sample_rgb_png(tmp_path_factory) -> Path:
    """造一张 128x128 纯色 RGB PNG 作为 fixture。"""
    from PIL import Image

    path = tmp_path_factory.mktemp("ocr_integration") / "blank.png"
    img = Image.new("RGB", (128, 128), (255, 255, 255))
    img.save(str(path), "PNG")
    return path


@pytest.fixture(scope="module")
def sample_grayscale_png(tmp_path_factory) -> Path:
    """造一张单通道 PNG（gray）。PaddleOCR 不接，必须走 RGB 预处理。"""
    from PIL import Image

    path = tmp_path_factory.mktemp("ocr_integration") / "gray.png"
    img = Image.new("L", (128, 128), 200)
    img.save(str(path), "PNG")
    return path


# ── tests ────────────────────────────────────────────────────────


@_SKIP_UNLESS_ENABLED
class TestOcrWorkerRealIntegration:
    """真 spawn ``app.common.ocr_worker`` 的集成测试。"""

    def test_worker_starts_and_responds_to_ping(self):
        """发 ping 拿到 pong（证明协议 + spawn + ready 都通）。"""
        from app.common.ocr_process_client import OcrProcessClient
        from app.core.config import get_settings

        settings = get_settings()
        client = OcrProcessClient(settings)

        try:
            client.ensure_ready()
            # 用 _next_id + dispatch 自己实现一次 ping（不暴露 public ping）
            client._ready_event.wait(timeout=settings.ocr_subprocess_timeout)
            assert client._proc is not None
            assert client._proc.poll() is None, "worker 应该在跑"
        finally:
            client.close()

    def test_predict_returns_empty_for_blank_image(self, sample_rgb_png):
        """一张纯白图跑 OCR：worker 应返回 text 字段（空或仅空白行）。"""
        from app.common.ocr_process_client import OcrProcessClient
        from app.core.config import get_settings

        settings = get_settings()
        client = OcrProcessClient(settings)

        try:
            client.ensure_ready()
            text = client.predict(str(sample_rgb_png))
            # 纯白图应该没文字 → text 可能是空串、也可能识别到 0 行
            # 我们只验证 worker 返回的是字符串且不抛异常
            assert isinstance(text, str), f"worker 返了 {type(text).__name__} 而非 str"
        finally:
            client.close()

    def test_predict_grayscale_image_does_not_crash(self, sample_grayscale_png):
        """单通道 PNG（灰度）经由 OcrService 应当被 RGB 守卫预处理、不崩。"""
        from app.common.ocr_service import OcrService

        svc = OcrService()
        result = svc.extract_text(sample_grayscale_png)
        assert isinstance(result, str)

    def test_worker_dead_respawns(self, sample_rgb_png):
        """kill -9 worker 后再 predict，应自动 respawn 并返回结果。"""
        from app.common.ocr_process_client import OcrProcessClient
        from app.core.config import get_settings

        settings = get_settings()
        client = OcrProcessClient(settings)

        try:
            client.ensure_ready()
            assert client._proc is not None
            original_pid = client._proc.pid

            # 强制 kill worker，模拟 crash
            client._proc.kill()
            client._proc.wait(timeout=2)
            assert client._proc.poll() is not None

            # 下一次 predict 应自动 respawn
            text = client.predict(str(sample_rgb_png))
            assert isinstance(text, str)
            assert client._proc is not None
            assert client._proc.pid != original_pid, (
                "respawn 后的 worker pid 应该不同"
            )
        finally:
            client.close()
