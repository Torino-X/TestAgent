"""OCR 图片文字提取服务（facade 模式）。

2026-07-14（路线 B 复刻老项目）：
  - 本文件只保留 facade 公开接口：``extract_text / _ensure_ocr / _ensure_rgb /
    _predict / available / _profile``
  - PaddleOCR 实际加载、模型权重下载、JSON-line IPC 全部下沉到
    ``app.common.ocr_worker`` 子进程
  - 主进程侧只通过 ``app.common.ocr_process_client.OcrProcessClient`` IPC
  - facade 还负责 RGB 预处理（必须在主进程做，传 RGB 路径给 worker）和
    失败降级（FileNotFoundError 之外的异常 → 返回空串 + logger.exception）

调用方契约（保持兼容）：
  - ``ocr_service.extract_text(image_path: Path) -> str`` 同步阻塞
  - ``ocr_service._ensure_ocr()`` 同步，被 main.py lifespan 用作预热
  - ``ocr_service._predict(rgb_path: str) -> str`` 委托 client，但保留为可
    patch 兼容点（test_ocr_service_rgb_guard 用 ``svc._predict = fake_predict``）

向后兼容的测试 mock 边界：
  - ``is_paddleocr_available()`` 模块级函数（test_common_modules patch）
  - ``svc._predict`` 实例属性（test_ocr_service_rgb_guard patch）
  - ``svc._client`` 实例属性（test_ocr_service_concurrency 新边界）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── PaddleOCR 可用性探测（轻量）─────────────────────────────────────
# 注意：facade 不再缓存探测结果，因为主进程不再持 PaddleOCR，缓存意义不大；
# 这里的 ``import paddleocr`` 只用于"环境是否能 spawn worker"的探测，廉价。


def is_paddleocr_available() -> bool:
    """检测 PaddleOCR 是否可用（无缓存版本）。"""
    try:
        import paddleocr  # noqa: F401
        return True
    except ImportError:
        return False


# ── OCR 服务（facade）─────────────────────────────────────────────


class OcrService:
    """OCR 图片文字提取服务（facade）。

    用法::

        ocr = OcrService()
        text = ocr.extract_text(Path("/path/to/image.png"))
        print(text)  # "识别的文字行1\\n识别的文字行2"

    对外接口保持稳定（与 main + image_understanding_orchestrator 的契约）：
      - ``available``：bool
      - ``_profile``：str
      - ``extract_text(image_path) -> str``
      - ``_ensure_ocr()``：启动预热入口（lifespan 用）
      - ``_ensure_rgb(image_path) -> Path``：静态方法，RGB 预处理
      - ``_predict(rgb_path: str) -> str``：委托 client（保留可 patch）
    """

    def __init__(self, settings: Optional[object] = None) -> None:
        # settings 可选：缺省从 app.core.config.get_settings() 取
        if settings is None:
            from app.core.config import get_settings

            settings = get_settings()
        self._settings = settings

        try:
            from app.common.ocr_process_client import OcrProcessClient

            self._client: Optional[OcrProcessClient] = OcrProcessClient(settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "OcrProcessClient 初始化失败，OCR 功能降级为空结果 | err=%s", exc,
            )
            self._client = None

        # facade 自己的 profile 字段（与旧版对齐，仍叫 _profile）
        from app.common.ocr_worker import _resolve_ocr_profile

        self._profile: str = _resolve_ocr_profile(
            str(getattr(settings, "ocr_profile", "auto"))
        )

    @property
    def available(self) -> bool:
        return self._client is not None and is_paddleocr_available()

    def extract_text(self, image_path: Path) -> str:
        """从图片文件中提取文字。

        Returns:
            识别出的文字，每行用换行符分隔。当 OCR 不可用时返回空字符串。

        Raises:
            FileNotFoundError: 图片文件不存在（保留旧契约，test_common_modules 用）
        """
        if not self.available:
            logger.warning("OCR 不可用，跳过图片 %s", image_path)
            return ""

        if not image_path.exists():
            raise FileNotFoundError(f"图片文件不存在：{image_path}")

        # 2026-07-14：PaddleOCR 的 text_detection 模型只接受 3 通道 RGB 输入，
        # 灰度图（mode='L'）或单通道 PNG 会触发 oneDNN kernel 崩溃。
        # Word 文档里嵌入的 logo、签名、印章常常是单通道图，所以必须预处理。
        rgb_path = self._ensure_rgb(image_path)

        try:
            return self._predict(str(rgb_path))
        except FileNotFoundError:
            raise
        except Exception as exc:
            logger.exception("OCR 提取文字失败：%s", image_path)
            return ""
        finally:
            if rgb_path != image_path:
                try:
                    rgb_path.unlink(missing_ok=True)
                except Exception:
                    pass

    # ── 启动预热入口（lifespan 用） ────────────────────────────

    def _ensure_ocr(self) -> None:
        """触发 worker spawn + 等 ready。被 lifespan 用作预热。

        子进程架构下等价于 ``self._client.ensure_ready()``；保留方法名是为了
        不破坏现有 main.py 调用点（``ocr_service._ensure_ocr``）。
        """
        if self._client is None:
            return
        try:
            self._client.ensure_ready()
        except Exception as exc:  # noqa: BLE001 — 预热失败不应阻塞启动
            logger.warning("OCR worker 启动预热失败，按需懒加载兜底 | err=%s", exc)

    # ── RGB 预处理（主进程做，传 RGB 路径给 worker） ─────────────

    @staticmethod
    def _ensure_rgb(image_path: Path) -> Path:
        """Return a 3-channel RGB copy of ``image_path`` if necessary.

        PaddleOCR 的 text_detection 模型只接受 RGB 输入。灰度图（mode='L'）、
        调色板图（mode='P'）、带 alpha 通道图（mode='RGBA'）都会触发
        ``Axis > max_dim`` 错误。处理方式：转 RGB 写到同名 ``.rgb.png``
        临时文件，调用方负责清理。
        """
        try:
            from PIL import Image
        except ImportError:
            return image_path

        try:
            with Image.open(str(image_path)) as img:
                if img.mode == "RGB":
                    return image_path
                rgb_img = img.convert("RGB")
                rgb_path = image_path.with_suffix(".rgb.png")
                rgb_img.save(str(rgb_path), "PNG")
                return rgb_path
        except Exception as exc:
            logger.warning(
                "OCR 预处理（转 RGB）失败: %s | %s",
                image_path, exc,
            )
            return image_path

    # ── 委托方法（保留为可 patch 兼容点） ────────────────────────

    def _predict(self, rgb_path: str) -> str:
        """委托给 OcrProcessClient.predict。这是 facade 的核心边界。

        保留为可 patch 兼容点：test_ocr_service_rgb_guard 用
        ``svc._predict = fake_predict`` 跳过真实 PaddleOCR，子进程架构下依然
        有效（mock 整个方法不会再进 IPC）。
        """
        if self._client is None:
            logger.warning("OcrProcessClient 不可用，_predict 走空降级")
            return ""
        return self._client.predict(rgb_path)


# 向后兼容：保留这些导出（其他模块可能 import）
__all__ = [
    "OcrService",
    "ocr_service",
    "is_paddleocr_available",
    # 老版常量（虽然没用，但保留以防被 import）
    "OCR_PROFILE_LIGHTWEIGHT",
    "OCR_PROFILE_HIGH_ACCURACY",
    "OCR_MODEL_PRESETS",
]

# ── 兼容别名：从 worker 模块 re-export，避免破坏其他模块 ──────────
# 这些常量在老代码中被当作 enum 用，子进程架构下沉到 worker，
# 这里只做 import-time re-export。
from app.common.ocr_worker import (  # noqa: E402, F401
    OCR_PROFILE_LIGHTWEIGHT,
    OCR_PROFILE_HIGH_ACCURACY,
    OCR_MODEL_PRESETS,
)


# ── module-level 单例 ──────────────────────────────────────────
# 保留同名 ``ocr_service``，下游 import 不需要改
ocr_service: OcrService = OcrService()  # type: ignore[assignment]


# 模块定位:OCR 服务 facade(主进程侧)
#
# 2026-07-14(路线 B 复刻老项目):
#   - 本文件只留 facade 公开接口:`extract_text / _ensure_ocr / _ensure_rgb / _predict / available / _profile`;
#   - **真实 PaddleOCR 加载、模型权重下载、JSON-line IPC 全下沉到 ocr_worker 子进程**;
#   - 主进程通过 OcrProcessClient 与 worker 通信。
#
# 链路:
#   ImageUnderstandingOrchestrator.understand_one(image) →
#     ocr_service.extract_text(image_path) → OcrProcessClient.predict → worker
#
# 关键约束:
#   - 不要在这里 import PaddleOCR / 解析库(子进程隔离);
#   - OCR 模型加载在 worker 内 5~10s,主进程不能重复付出这个成本;
#   - worker 异常时返回 degraded payload 而非 throw。
