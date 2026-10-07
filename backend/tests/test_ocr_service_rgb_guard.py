"""Tests for the RGB-conversion guard in ``OcrService.extract_text``.

2026-07-14 真实日志报：

    InvalidArgumentError: Axis should be less than or equal to 1,
    but received axis is 2.

根因：PaddleOCR 的 text_detection 模型只接受 3 通道 RGB 输入。
Word 文档里嵌入的 logo / 签名 / 印章常常是 ``mode='L'``（灰度）、
``mode='P'``（调色板）或 ``mode='RGBA'``（带 alpha），都会触发
``max_dim=1`` 报错。

修复：在 ``extract_text`` 之前用 ``PIL.Image.convert("RGB")``
预处理，把图写到同名 ``.rgb.png`` 临时文件。

测试覆盖：
1. 已是 RGB 的 PNG：原路径透传，不写临时文件
2. 灰度图（``mode='L'``）：转 RGB 后路径变成 ``.rgb.png``，调用后被清理
3. PIL 不可用：降级到原路径
4. ``_ensure_rgb`` 本身失败（如损坏的 PNG）：返回原路径让上层 try/except 兜
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image


# ── Helpers ─────────────────────────────────────────────────────────


def _make_png(path: Path, *, mode: str, color: int | tuple = 128) -> None:
    """Create a small ``mode``-mode PNG at ``path``."""
    if mode == "RGB":
        img = Image.new("RGB", (32, 32), (color, color, color) if isinstance(color, int) else color)
    elif mode == "L":
        img = Image.new("L", (32, 32), color if isinstance(color, int) else color[0])
    elif mode == "RGBA":
        img = Image.new("RGBA", (32, 32), (color, color, color, 255) if isinstance(color, int) else color)
    elif mode == "P":
        img = Image.new("P", (32, 32), color if isinstance(color, int) else color[0])
    else:
        raise ValueError(f"unsupported mode: {mode}")
    img.save(str(path), "PNG")


# ── _ensure_rgb 单元测试 ──────────────────────────────────────────


class TestEnsureRgb:
    """``_ensure_rgb`` 是修复的核心函数。"""

    def test_rgb_image_returns_original_path(self, tmp_path: Path):
        from app.common.ocr_service import OcrService

        src = tmp_path / "rgb.png"
        _make_png(src, mode="RGB", color=(255, 100, 50))

        out = OcrService._ensure_rgb(src)
        assert out == src, "RGB 输入应原样返回，不写临时文件"
        assert not (tmp_path / "rgb.rgb.png").exists(), (
            "RGB 直通路径不应产生临时文件"
        )

    def test_grayscale_image_is_converted(self, tmp_path: Path):
        from app.common.ocr_service import OcrService

        src = tmp_path / "gray.png"
        _make_png(src, mode="L", color=128)

        out = OcrService._ensure_rgb(src)
        assert out != src, "灰度图应被转换到新路径"
        assert out.name == "gray.rgb.png"
        # 验证临时文件确实是 3 通道 RGB
        with Image.open(str(out)) as img:
            assert img.mode == "RGB"

    def test_rgba_image_is_converted(self, tmp_path: Path):
        from app.common.ocr_service import OcrService

        src = tmp_path / "rgba.png"
        _make_png(src, mode="RGBA", color=(10, 20, 30))

        out = OcrService._ensure_rgb(src)
        assert out != src
        assert out.name == "rgba.rgb.png"
        with Image.open(str(out)) as img:
            assert img.mode == "RGB"

    def test_palette_image_is_converted(self, tmp_path: Path):
        from app.common.ocr_service import OcrService

        src = tmp_path / "palette.png"
        _make_png(src, mode="P", color=128)

        out = OcrService._ensure_rgb(src)
        assert out != src
        assert out.name == "palette.rgb.png"
        with Image.open(str(out)) as img:
            assert img.mode == "RGB"

    def test_corrupt_png_falls_back_to_original(self, tmp_path: Path):
        from app.common.ocr_service import OcrService

        src = tmp_path / "broken.png"
        src.write_bytes(b"not actually a png")

        # 损坏文件让 PIL 抛异常；函数应降级返回原路径，让上层 try/except 兜
        out = OcrService._ensure_rgb(src)
        assert out == src

    def test_pil_unavailable_falls_back(self, tmp_path: Path):
        from app.common.ocr_service import OcrService

        src = tmp_path / "any.png"
        _make_png(src, mode="L", color=64)

        # ``_ensure_rgb`` 内部 ``from PIL import Image`` 是局部导入，
        # 需要在 builtin ``__import__`` 层拦截。直接让 import 抛
        # ImportError，等同于 PIL 未安装的场景。
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "PIL.Image" or name.startswith("PIL"):
                raise ImportError("simulated PIL unavailable")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            out = OcrService._ensure_rgb(src)

        # 降级返回原路径
        assert out == src


# ── extract_text 集成测试 ──────────────────────────────────────────


class TestExtractTextWithRgbGuard:
    """``extract_text`` 应在 OCR 调用前自动走 RGB 转换，且清理临时文件。"""

    def test_grayscale_image_does_not_crash_ocr(self, tmp_path: Path):
        """Regression: 之前灰度图会触发 axis=2 > max_dim=1 崩溃。

        现在：extract_text 内部转 RGB 后传给 PaddleOCR，不应再崩溃；
        OCR 调用本身（mock 掉）只需要确认我们传给它的路径是 RGB 文件。
        """
        from app.common.ocr_service import OcrService

        src = tmp_path / "logo_gray.png"
        _make_png(src, mode="L", color=200)

        svc = OcrService()

        captured: dict[str, str] = {}

        def fake_predict(path: str) -> str:
            """路线 B 后 ``_predict`` 直接返回 OCR 文字串（不再返回 PaddleOCR result list）。"""
            captured["path"] = path
            # 验证传入路径确实是 3 通道 RGB
            with Image.open(path) as img:
                assert img.mode == "RGB", (
                    f"extract_text 必须把图转成 RGB 后再调 OCR，"
                    f"但传入的是 {img.mode}"
                )
            return ""

        # 不初始化 _ocr（避免真实加载 PaddleOCR 模型），直接替换 _predict
        svc._predict = fake_predict  # type: ignore[assignment]

        result = svc.extract_text(src)

        assert captured["path"], "_predict 应该被调用"
        assert result == "", "没有文字的图应返回空字符串"
        # 临时文件已被清理
        assert not (tmp_path / "logo_gray.rgb.png").exists(), (
            "extract_text 应在 finally 清理临时 .rgb.png 文件"
        )

    def test_rgb_image_does_not_create_temp(self, tmp_path: Path):
        """RGB 输入走快路径：不应写临时文件。"""
        from app.common.ocr_service import OcrService

        src = tmp_path / "rgb_input.png"
        _make_png(src, mode="RGB", color=(10, 20, 30))

        svc = OcrService()
        seen: list[str] = []

        def fake_predict(path: str):
            seen.append(path)
            return []

        svc._predict = fake_predict  # type: ignore[assignment]
        svc.extract_text(src)

        assert seen == [str(src)], (
            f"RGB 输入应直接传给 OCR，但传给的是 {seen!r}"
        )
        assert not (tmp_path / "rgb_input.rgb.png").exists()

    def test_temp_file_cleaned_even_when_ocr_raises(self, tmp_path: Path):
        """OCR 抛异常时 finally 也应清理临时文件。"""
        from app.common.ocr_service import OcrService

        src = tmp_path / "boom.png"
        _make_png(src, mode="L", color=128)

        svc = OcrService()

        def boom(_path: str):
            raise RuntimeError("OCR kernel crash")

        svc._predict = boom  # type: ignore[assignment]
        # 即使 OCR 内部崩，extract_text 不应向上抛（已 try/except）
        result = svc.extract_text(src)

        assert result == "", "OCR 崩溃应降级为空字符串"
        assert not (tmp_path / "boom.rgb.png").exists(), (
            "OCR 崩溃后 finally 仍应清理临时文件，不能泄漏"
        )


# ── 路线 B 后 spy 测试 ────────────────────────────────────────────────
#
# 2026-07-14：把 OCR 拆成 OcrService + OcrProcessClient + ocr_worker 子进程后，
# `_predict` 仍是 facade 上的可 patch 兼容点（test/test 演进不破坏 RGB guard
# 测试）。但有风险——以后有人误改成"extract_text 先调 client.ensure_ready
# 再调 _predict"，会让所有 patch 点失效。这里加 spy 测试守住兼容边界。


class TestFacadeClientDelegation:
    """`_predict` 必须仍是 facade 上的可 patch 边界；mock 它不应触发 client。"""

    def test_predict_mock_does_not_trigger_client(self, tmp_path: Path, monkeypatch):
        """在 OcrService 实例上把 ``_predict`` 替换成 fake，调用 ``extract_text``
        时不应走到 ``OcrProcessClient.predict``。
        """
        from app.common import ocr_service as ocr_service_mod

        src = tmp_path / "rgb.png"
        _make_png(src, mode="RGB", color=(10, 20, 30))

        svc = ocr_service_mod.OcrService()

        # spy：client.predict 千万不能被走到
        client_predict_called = {"n": 0}

        if svc._client is not None:
            original_client_predict = svc._client.predict

            def spy_client_predict(path):
                client_predict_called["n"] += 1
                return original_client_predict(path)

            monkeypatch.setattr(svc._client, "predict", spy_client_predict)

        # 把 facade 的 _predict 替换成 fake
        def fake_facade_predict(rgb_path: str) -> str:
            return "fake-ocr-result"

        monkeypatch.setattr(svc, "_predict", fake_facade_predict)

        result = svc.extract_text(src)

        assert result == "fake-ocr-result", (
            "monkeypatch 替换 _predict 后，extract_text 应返回 fake 的结果，"
            f"实际拿到 {result!r}"
        )
        assert client_predict_called["n"] == 0, (
            "mock 掉 _predict 后不应再调到 OcrProcessClient.predict，"
            f"实际调了 {client_predict_called['n']} 次"
        )

    def test_predict_default_delegates_to_client(self, tmp_path: Path, monkeypatch):
        """不 mock _predict 时，extract_text 的 predict 路径会委托到 client。
        这里把 client.predict monkeypatch 成返回固定字符串，验证委托链通。
        """
        from app.common import ocr_service as ocr_service_mod

        src = tmp_path / "rgb2.png"
        _make_png(src, mode="RGB", color=(50, 50, 50))

        svc = ocr_service_mod.OcrService()
        assert svc._client is not None

        def fake_client_predict(path: str) -> str:
            return f"client-returned:{path}"

        monkeypatch.setattr(svc._client, "predict", fake_client_predict)

        result = svc.extract_text(src)

        assert result.startswith("client-returned:"), (
            f"未 mock _predict 时应走 client 委托，实际返回 {result!r}"
        )