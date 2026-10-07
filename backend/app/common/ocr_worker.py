"""OCR worker 子进程入口。

2026-07-14（路线 B 复刻老项目）：
  - OCR 服务从主进程内嵌改为子进程架构，主进程通过
    ``OcrProcessClient`` 走 stdin/stdout JSON-line 协议调用本 worker
  - 本进程负责 PaddleOCR 模型加载、文字预测、把结果 JSON-line 化回主进程
  - PaddleOCR 的 stdout 噪音（init 期间的 print）必须在本进程内吃掉，
    否则会污染 JSON 协议通道
  - BLAS env（OMP_NUM_THREADS 等）由主进程在 ``Popen(env=...)`` 时注入，
    不在本进程里 ``setdefault``

启动方式：``python -m app.common.ocr_worker --profile lightweight --ocr-threads 2``

协议（stdin/stdout JSON-line，每行一条 UTF-8 JSON + ``\\n``）：
  主→worker:
    {action:"predict", id:"<uuid>", image_path:"<abs>"}
    {action:"ping",    id:"<uuid>"}
    {action:"shutdown", id:"_s"}
  worker→主:
    {action:"ready", pid:N, profile, ocr_threads, model_version}           # 仅第一条
    {action:"result", id, ok:true, text, lines, elapsed_ms}
    {action:"error",  id, ok:false, code, message, elapsed_ms}              # code ∈ OCR_FAILED|IMAGE_NOT_FOUND|INTERNAL
    {action:"pong",   id}
    {action:"ack",    id}
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("app.common.ocr_service.worker")

OCR_PROFILE_LIGHTWEIGHT = "lightweight"
OCR_PROFILE_HIGH_ACCURACY = "high_accuracy"

OCR_MODEL_PRESETS = {
    OCR_PROFILE_LIGHTWEIGHT: {
        "text_detection_model_name": "PP-OCRv5_mobile_det",
        "text_recognition_model_name": "PP-OCRv5_mobile_rec",
    },
    OCR_PROFILE_HIGH_ACCURACY: {
        "text_detection_model_name": "PP-OCRv5_server_det",
        "text_recognition_model_name": "PP-OCRv5_server_rec",
    },
}


def _resolve_ocr_profile(profile: str = "auto") -> str:
    raw = (profile or "auto").strip().lower().replace("-", "_")
    if raw in {"auto", "default", ""}:
        return OCR_PROFILE_LIGHTWEIGHT
    if raw in {"high_accuracy", "high", "server", "accurate", "accuracy", "quality"}:
        return OCR_PROFILE_HIGH_ACCURACY
    return OCR_PROFILE_LIGHTWEIGHT


# ── stdout 重定向：抑制 PaddleOCR init 噪音 ───────────────────────────


class _redirect_stdout:
    """临时把 ``sys.stdout`` 替换成一个空 StringIO。

    PaddleOCR 初始化时会向 stdout 打 print（"download ..." / "loading ..."），
    这些噪音会污染我们的 JSON-line 协议通道。用 ``__enter__`` 把 stdout 替换
    成一个空 StringIO，``__exit__`` 时还原。
    """

    def __init__(self, buffer: io.StringIO) -> None:
        self._buffer = buffer
        self._original = sys.stdout

    def __enter__(self):
        sys.stdout = self._buffer
        return self

    def __exit__(self, *args):
        sys.stdout = self._original


# ── PaddleOCR 加载 / 预测 ──────────────────────────────────────────────


def _is_paddleocr_available() -> bool:
    try:
        import paddleocr  # noqa: F401
        return True
    except ImportError:
        return False


def _create_ocr(profile: str, paddle_ocr_cls, ocr_threads: int):
    """创建 PaddleOCR 实例（CPU-only，参数兼容新旧 API）。"""
    import inspect

    sig = inspect.signature(paddle_ocr_cls)
    params = sig.parameters
    preset = OCR_MODEL_PRESETS.get(profile, OCR_MODEL_PRESETS[OCR_PROFILE_LIGHTWEIGHT])

    if "use_doc_unwarping" in params:
        kwargs = {
            "lang": "ch",
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            **{k: v for k, v in preset.items() if k in params},
        }
        if "cpu_threads" in params:
            kwargs["cpu_threads"] = int(ocr_threads)
        return paddle_ocr_cls(**kwargs)

    kwargs = {"use_angle_cls": True, "lang": "ch", "show_log": False}
    kwargs.update({k: v for k, v in preset.items() if k in params})
    if "cpu_threads" in params:
        kwargs["cpu_threads"] = int(ocr_threads)
    return paddle_ocr_cls(**kwargs)


def _predict(ocr, image_path: str):
    """执行 OCR 预测，兼容新旧 PaddleOCR API。"""
    if hasattr(ocr, "predict"):
        return ocr.predict(image_path)
    return ocr.ocr(image_path, cls=True)


def _collect_text(value) -> List[str]:
    """从 PaddleOCR 的多种输出格式中收集文字（完整兼容旧项目逻辑）。

    支持：
    - dict with rec_texts / texts / text / transcription
    - PaddleResult/OCRPredictorResult（新版 API，通过 .json / .res 属性）
    - list of (bbox, (text, confidence))（旧版 API）
    """
    texts: List[str] = []

    def walk(item) -> None:
        if isinstance(item, dict):
            for key in ("rec_texts", "texts", "text", "transcription"):
                candidate = item.get(key)
                if isinstance(candidate, list):
                    for t in candidate:
                        if isinstance(t, str) and t.strip():
                            texts.append(t.strip())
                elif isinstance(candidate, str) and candidate.strip():
                    texts.append(candidate.strip())
            for child in item.values():
                if isinstance(child, (list, tuple, dict)):
                    walk(child)
            return
        if hasattr(item, "json"):
            try:
                walk(item.json)
                return
            except Exception:
                pass
        if hasattr(item, "res"):
            try:
                walk(item.res)
                return
            except Exception:
                pass
        if isinstance(item, (list, tuple)):
            if len(item) >= 2 and isinstance(item[1], (list, tuple)) and item[1]:
                candidate = item[1][0]
                if isinstance(candidate, str) and candidate.strip():
                    texts.append(candidate.strip())
                    return
            for child in item:
                walk(child)

    walk(value)

    unique: List[str] = []
    seen = set()
    for text in texts:
        if text not in seen:
            seen.add(text)
            unique.append(text)
    return unique


# ── 协议辅助 ────────────────────────────────────────────────────────


def _write_line(payload: dict) -> None:
    """序列化一行 JSON 到 stdout。"""
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


def _read_line() -> Optional[str]:
    """从 stdin 读一行。EOF 时返回 None。"""
    line = sys.stdin.readline()
    if not line:
        return None
    return line


# ── main 入口 ──────────────────────────────────────────────────────


def _load_paddleocr(profile: str, ocr_threads: int):
    """加载 PaddleOCR（首条 ready 消息发出前调用）。"""
    if not _is_paddleocr_available():
        logger.error("PaddleOCR 未安装，worker init 失败")
        return None

    # 配 BLAS env：主进程已经 inject 过，这里再 setdefault 兜底保证兼容性
    for key in (
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
    ):
        os.environ.setdefault(key, "True" if key == "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK" else "1")

    # 抑制 init 期间的 stdout print（必须）
    with io.StringIO() as buf, _redirect_stdout(buf):
        from paddleocr import PaddleOCR
        ocr = _create_ocr(profile, PaddleOCR, ocr_threads)

    logger.info("PaddleOCR 加载完成（profile=%s, threads=%s）", profile, ocr_threads)
    return ocr


def _run_predict(ocr, image_path: str) -> dict:
    """单张 predict，返回给主进程的响应 dict（不含 id/action，由调用方填）。"""
    started = time.monotonic()
    p = Path(image_path)
    if not p.exists():
        return {
            "ok": False,
            "code": "IMAGE_NOT_FOUND",
            "message": f"图片文件不存在：{image_path}",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    try:
        result = _predict(ocr, image_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("OCR predict 失败: %s", image_path)
        return {
            "ok": False,
            "code": "OCR_FAILED",
            "message": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    lines = _collect_text(result)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    # 限制单次返回文本大小（兜底，超过 1MB 就截断 + 标注 truncated）
    text = "\n".join(lines)
    truncated = False
    if len(text) > 1_000_000:
        text = text[:1_000_000]
        truncated = True

    return {
        "ok": True,
        "text": text,
        "lines": len(lines),
        "elapsed_ms": elapsed_ms,
        "truncated": truncated,
    }


def _on_sigterm(_signo, _frame):  # pragma: no cover — 信号回调测试不便覆盖
    """SIGTERM 走 graceful shutdown（与 stdin `shutdown` 一致）。"""
    _write_line({"action": "ack", "id": "_sigterm"})
    sys.exit(0)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="app.common.ocr_worker")
    parser.add_argument("--profile", default="auto", help="OCR profile: lightweight|high_accuracy|auto")
    parser.add_argument("--ocr-threads", type=int, default=2, help="BLAS CPU 线程数")
    parser.add_argument("--model-version", default="unknown", help="客户端传入的 PaddleOCR 版本标识")
    args = parser.parse_args(argv)

    profile = _resolve_ocr_profile(args.profile)
    ocr_threads = max(1, int(args.ocr_threads))

    # stderr 走默认 logging（主进程的 StderrRelayThread 会接管）
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    # 优雅退出：SIGTERM / SIGINT
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (AttributeError, ValueError):  # pragma: no cover — Windows 上无 SIGTERM
        pass

    # 加载 PaddleOCR（这条必须在第一条 ready 消息之前完成）
    ocr = _load_paddleocr(profile, ocr_threads)

    # 写第一条 ready 消息：主进程看到这条就把 self._ready_event.set()
    _write_line({
        "action": "ready",
        "pid": os.getpid(),
        "profile": profile,
        "ocr_threads": ocr_threads,
        "model_version": args.model_version,
        "ocr_loaded": ocr is not None,
    })

    if ocr is None:
        # 加载失败：worker 仍然能存活，但 predict 会全部走 INTERNAL 错误
        logger.error("PaddleOCR 未加载，worker 进入降级模式")

    # 主循环：stdin 读 JSON 请求 → 响应
    while True:
        raw = _read_line()
        if raw is None:  # EOF，主进程关闭了 stdin
            logger.info("收到 stdin EOF，worker 退出")
            return 0

        raw = raw.strip()
        if not raw:
            continue

        try:
            req = json.loads(raw)
        except json.JSONDecodeError as exc:
            _write_line({
                "action": "error",
                "id": "_parse",
                "ok": False,
                "code": "INTERNAL",
                "message": f"JSON 解析失败: {exc}",
            })
            continue

        action = req.get("action")
        req_id = req.get("id", "_unknown")

        if action == "shutdown":
            _write_line({"action": "ack", "id": req_id})
            logger.info("worker 收到 shutdown，退出")
            return 0

        if action == "ping":
            _write_line({"action": "pong", "id": req_id})
            continue

        if action == "predict":
            if ocr is None:
                _write_line({
                    "action": "error",
                    "id": req_id,
                    "ok": False,
                    "code": "OCR_FAILED",
                    "message": "PaddleOCR 未加载",
                    "elapsed_ms": 0,
                })
                continue
            image_path = req.get("image_path")
            if not image_path:
                _write_line({
                    "action": "error",
                    "id": req_id,
                    "ok": False,
                    "code": "INTERNAL",
                    "message": "缺少 image_path 字段",
                })
                continue

            resp = _run_predict(ocr, image_path)
            resp["action"] = "result" if resp.get("ok") else "error"
            resp["id"] = req_id
            _write_line(resp)
            continue

        # 未知 action
        _write_line({
            "action": "error",
            "id": req_id,
            "ok": False,
            "code": "INTERNAL",
            "message": f"未知 action: {action!r}",
        })


if __name__ == "__main__":
    raise SystemExit(main())


# 模块定位:OCR worker 子进程入口
#
# 2026-07-14(路线 B 复刻):
#   - OCR 服务从主进程内嵌 → **子进程架构**;
#   - 主进程通过 OcrProcessClient 走 stdin/stdout JSON-line 协议调本 worker;
#   - 本进程负责 PaddleOCR 模型加载、文字预测、回传结果。
#
# 链路:
#   master → OcrProcessClient.predict → stdin pipe(JSON) →
#     this worker → ocr.predict(image) → stdout pipe(JSON)
#
# 关键约束:
#   - 单进程单模型 / 多 worker → 多个 OCR 子进程;
#   - 不写本地日志(由 OcrProcessClient 决定);
#   - **不要在主进程中 import 本模块**(子进程独立启动);
#   - 任何崩溃 → 由 OcrProcessClient 重启子进程。
