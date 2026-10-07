"""OCR worker 子进程客户端（主进程侧）。

2026-07-14（路线 B 复刻老项目）：
  - ``OcrProcessClient`` 是主进程侧的 IPC 客户端，集中所有子进程细节
  - 与 worker ``app.common.ocr_worker`` 通过 stdin/stdout JSON-line 协议通信
  - 核心 API：``ensure_ready()`` / ``predict(image_path) -> str`` / ``close()``
  - 内部：spawn 锁防并发启动、fingerprint 自动 restart、predict 死 worker 自愈、
    三阶段 shutdown 优雅退出、stderr 后台线程归并回主进程 logger
"""

from __future__ import annotations

import atexit
import concurrent.futures
import json
import logging
import os
import subprocess
import sys
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)


class OcrProcessError(RuntimeError):
    """主进程侧 IPC 异常：worker 崩溃、JSON 解析失败、超时等。"""


# ── 防御 GBK 编码错误的 StreamHandler ────────────────────────────────
#
# 2026-07-14：worker stderr 输出含 PaddleOCR 的非 UTF-8 字节（典型：Windows
# 平台下 cpp_extension warning 含一些 PaddlePaddle 私有编码的字符）。主进程的
# logging.StreamHandler 默认走 sys.stderr，Windows 上是 GBK 编码，emit 时会
# 抛 UnicodeEncodeError 写不进日志。这里给 StderrRelayThread 用的归并 logger
# 装一个自定义 handler，在 emit 阶段主动用 'replace' 策略，丢掉最多少量字符，
# 但保证 stderr 归并链路永不抛异常。
class _GbkSafeStreamHandler(logging.StreamHandler):
    """Windows-friendly StreamHandler：emit 时主动替换不可编码字符。"""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            msg = self.format(record)
            stream = self.stream
            if stream is None:
                return
            # 试默认编码（通常是 GBK on Windows）
            enc = getattr(stream, "encoding", None) or "utf-8"
            try:
                stream.write(msg + self.terminator)
            except UnicodeEncodeError:
                # 不可编码字符 → 'replace'（Windows GBK 下变成 '?'）
                safe = msg.encode(enc, "replace").decode(enc, "replace")
                stream.write(safe + self.terminator)
            self.flush()
        except Exception:
            # 绝对不能让 relay thread 崩 logging 主流程
            try:
                sys.stderr.write("[ocr-worker-relay] emit failed\n")
            except Exception:
                pass


# ── 常量 ──────────────────────────────────────────────────────

# 文本协议相关
_PROTOCOL_MAX_LINE_BYTES = 2 * 1024 * 1024  # 2MB，单行 JSON 上限（防恶意/异常输入撑爆 pipe）

# 默认 shutdown 三阶段超时
_SHUTDOWN_GRACEFUL_SEC = 2.0
_SHUTDOWN_TERMINATE_SEC = 1.0
_SHUTDOWN_KILL_SEC = 0.5

# 默认 subprocess 的超时（若 settings 没配，用这个）
_DEFAULT_SUBPROCESS_TIMEOUT = 30.0


# ── 子进程 stderr → 主进程 logger 的归并线程 ──────────────────────


class StderrRelayThread(threading.Thread):
    """把 worker 的 stderr 行按行读出来，加前缀 re-log 到主进程 logger。

    守护线程：worker 死了 → pipe EOF → 线程自然 return。
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        *,
        prefix: str = "[ocr-worker]",
        logger_name: str = "app.common.ocr_service.worker",
    ) -> None:
        super().__init__(daemon=True, name="OcrStderrRelay")
        self._proc = proc
        self._prefix = prefix
        self._relay_logger = logging.getLogger(logger_name)
        # 2026-07-14：归并 logger 走 _GbkSafeStreamHandler（emit 阶段
        # 把不可 GBK 编码的字符安全替换为 '?'，避免 StreamHandler 抛
        # UnicodeEncodeError 写不进主进程 stdout）。
        self._ensure_gbk_safe_handler()

    def _ensure_gbk_safe_handler(self) -> None:
        """若归并 logger 还没装 _GbkSafeStreamHandler，补一个。"""
        for h in self._relay_logger.handlers:
            if isinstance(h, _GbkSafeStreamHandler):
                return
        h = _GbkSafeStreamHandler(stream=sys.stdout)
        h.setLevel(logging.INFO)
        # 复用根 logger 的 formatter（如果存在）
        root = logging.getLogger()
        if root.handlers:
            h.setFormatter(root.handlers[0].formatter)
        self._relay_logger.addHandler(h)
        # 不 propagate，避免根 logger 再发一份导致重复行
        self._relay_logger.propagate = False

    def run(self) -> None:
        try:
            stream = self._proc.stderr
            if stream is None:
                return
            for raw in iter(stream.readline, b""):
                try:
                    line = raw.decode("utf-8", "replace").rstrip()
                except Exception:
                    continue
                if line:
                    # emit 阶段由 _GbkSafeStreamHandler 把不可 GBK 编码的
                    # 字符替换为 '?'，这里不抛异常。
                    self._relay_logger.info("%s %s", self._prefix, line)
        except Exception:
            try:
                self._relay_logger.exception("StderrRelayThread 异常退出")
            except Exception:
                pass
        finally:
            try:
                if self._proc.stderr:
                    self._proc.stderr.close()
            except Exception:
                pass


# ── 子进程 stdout → dispatch 到 futures / ready event ────────────────


class StdoutReaderThread(threading.Thread):
    """把 worker 的 stdout 行按 JSON 解析后 dispatch 到对应的 Future。

    - ``ready`` 单条消息：触发 ``ready_event``，让 ``ensure_ready`` 解除阻塞
    - ``result`` / ``error`` / ``pong`` / ``ack``：按 ``id`` 字段派发给对应 Future
    - stdout EOF（worker 死 / pipe 关闭）：把所有未完成 futures set_exception

    守护线程；异常情况下用 finally 清空 futures，避免上层永久阻塞。
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        futures: dict[str, concurrent.futures.Future],
        ready_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name="OcrStdoutReader")
        self._proc = proc
        self._futures = futures
        self._ready_event = ready_event

    def run(self) -> None:
        stream = self._proc.stdout
        if stream is None:
            return

        try:
            for raw in iter(stream.readline, b""):
                if not raw:
                    break
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    logger.warning("OCR worker stdout JSON 解析失败: %s", exc)
                    continue

                if not isinstance(msg, dict):
                    continue

                action = msg.get("action")

                # ready：仅第一条
                if action == "ready":
                    if not self._ready_event.is_set():
                        self._ready_event.set()
                    continue

                req_id = msg.get("id")
                fut = self._futures.get(req_id) if req_id else None
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except Exception:
            logger.exception("StdoutReaderThread 异常退出")
        finally:
            # worker 死 / pipe 关闭：把所有未完成 futures 标失败，让上层重试或降级
            for fut in list(self._futures.values()):
                if not fut.done():
                    fut.set_exception(BrokenPipeError("ocr worker stdout closed"))
            self._futures.clear()
            try:
                if self._proc.stdout:
                    self._proc.stdout.close()
            except Exception:
                pass


# ── 客户端主体 ──────────────────────────────────────────────────


class OcrProcessClient:
    """OCR worker 子进程客户端（主进程侧）。

    调用方只需要 ``client.ensure_ready()`` 和 ``client.predict(path)``，不感知
    子进程的存在。worker 崩溃 / 配置变更 / 退出 等所有异常路径都对调用方透明
    （predict 返回 ``""`` 并 raise ``OcrProcessError`` 由 facade 兜底）。

    线程安全：
      - ``ensure_ready`` 与 ``predict`` 都用 ``_spawn_lock`` 双重检查
      - ``predict`` 用 ``_id_lock`` 生成唯一 request id
      - ``_futures`` 由 ``StdoutReaderThread`` 单线程写、predict 线程读；dict 操作
        在 CPython 上本身 GIL-safe，但保留 dict 引用不变性，避免 race
    """

    def __init__(
        self,
        settings: Any,
        *,
        worker_module: str = "app.common.ocr_worker",
    ) -> None:
        self._settings = settings
        self._worker_module = worker_module

        self._proc: Optional[subprocess.Popen] = None
        self._fingerprint: Optional[int] = None

        self._spawn_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._id_counter = 0

        self._ready_event = threading.Event()
        self._futures: dict[str, concurrent.futures.Future] = {}

        self._stdout_thread: Optional[StdoutReaderThread] = None
        self._stderr_thread: Optional[StderrRelayThread] = None

        # 保证进程退出时优雅关 worker
        atexit.register(self.close)

    # ── 公开 API ───────────────────────────────────────────────

    def ensure_ready(self) -> None:
        """幂等：worker 未启或配置变了 → spawn；已启且指纹一致 → 直接返回。

        Raises:
            OcrProcessError: spawn 失败或 ready 超时
        """
        target = self._compute_fingerprint()
        if (
            self._proc is not None
            and self._proc.poll() is None
            and self._fingerprint == target
        ):
            return

        with self._spawn_lock:
            # 二次检查
            if (
                self._proc is not None
                and self._proc.poll() is None
                and self._fingerprint == target
            ):
                return

            # 需要重启：先优雅关掉旧 worker（如果有且活着）
            if self._proc is not None and self._proc.poll() is None:
                logger.info("OCR worker 进程配置变化，正在重启…")
                self._shutdown_worker(graceful=True)

            self._spawn(target)

    def predict(self, image_path: str) -> str:
        """调 worker 跑一次 OCR，返回识别文字。

        自动重试 1 次：首次请求若撞 BrokenPipe（worker 没来得及准备好就死了），
        ``ensure_ready`` 会再 spawn 一次，自动重发同一请求。

        Args:
            image_path: 绝对路径

        Returns:
            识别出的文字，每行用换行符分隔。

        Raises:
            FileNotFoundError: worker 报告图片不存在（向 facade 透传）
            OcrProcessError: predict 失败（worker 错误码）或 IPC 错误
        """
        self.ensure_ready()

        last_exc: Optional[BaseException] = None
        for attempt in (1, 2):
            # 进 predict 前 worker 已死？自动再 spawn
            if self._proc is None or self._proc.poll() is not None:
                with self._spawn_lock:
                    if self._proc is None or self._proc.poll() is not None:
                        try:
                            self._spawn(self._compute_fingerprint())
                        except OcrProcessError as exc:
                            last_exc = exc
                            continue  # 第二次循环还要再尝试一次

            req_id = self._next_id()
            fut: concurrent.futures.Future = concurrent.futures.Future()
            self._futures[req_id] = fut
            try:
                line = (
                    json.dumps(
                        {
                            "action": "predict",
                            "id": req_id,
                            "image_path": image_path,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8")
                # 主进程 → worker
                try:
                    if self._proc is None or self._proc.stdin is None:
                        raise BrokenPipeError("ocr worker stdin not available")
                    self._proc.stdin.write(line)
                    self._proc.stdin.flush()
                except (BrokenPipeError, OSError) as exc:
                    raise OcrProcessError(f"写入 worker stdin 失败: {exc}") from exc

                # 等响应
                timeout = float(
                    getattr(self._settings, "ocr_subprocess_timeout", _DEFAULT_SUBPROCESS_TIMEOUT)
                )
                try:
                    resp = fut.result(timeout=timeout)
                except concurrent.futures.TimeoutError as exc:
                    raise OcrProcessError(
                        f"OCR worker predict 超时（{timeout}s）: {image_path}"
                    ) from exc
                except BrokenPipeError as exc:
                    # worker 死 / pipe 关：抛 OcrProcessError 让外层重试
                    raise OcrProcessError(f"OCR worker 已死: {exc}") from exc

                if resp.get("ok"):
                    return resp.get("text", "")

                code = resp.get("code", "INTERNAL")
                message = resp.get("message", "")
                if code == "IMAGE_NOT_FOUND":
                    raise FileNotFoundError(image_path)
                # 业务错误：透传
                raise OcrProcessError(f"[{code}] {message}")

            except (BrokenPipeError, OcrProcessError) as exc:
                last_exc = exc
                if attempt == 2:
                    # 第二次仍失败 → 抛
                    # 把 worker 标记为死，下一次会 respawn
                    self._proc = None
                    raise
                # 第一次失败：让循环重试
                logger.warning("OCR predict 第 %d 次失败，自愈重试: %s", attempt, exc)
                continue
            except FileNotFoundError:
                # 文件不存在：worker 已通过 IMAGE_NOT_FOUND 上报，facade 不需要再处理
                self._futures.pop(req_id, None)
                raise
            except Exception as exc:  # noqa: BLE001
                self._futures.pop(req_id, None)
                raise OcrProcessError(f"predict 未知异常: {exc}") from exc
            finally:
                self._futures.pop(req_id, None)

        # 不可达
        if last_exc is not None:
            raise last_exc
        raise OcrProcessError("OCR predict 失败且无可重试信息")

    def close(self) -> None:
        """优雅关闭 worker 子进程。三阶段：graceful → terminate → kill。"""
        with self._spawn_lock:
            if self._proc is None:
                return
            try:
                self._shutdown_worker(graceful=True)
            except Exception:
                logger.exception("OCR worker 优雅退出失败，强制 kill")
                try:
                    if self._proc is not None and self._proc.poll() is None:
                        self._proc.kill()
                except Exception:
                    pass
            self._proc = None
            self._fingerprint = None
            self._ready_event.clear()

    # ── 内部：spawn / shutdown / 状态 ─────────────────────────

    def _compute_fingerprint(self) -> int:
        """根据当前 settings 算 fingerprint，配置变则 spawn 新的。"""
        profile = str(getattr(self._settings, "ocr_profile", "auto"))
        threads = int(getattr(self._settings, "ocr_threads", 2))
        return hash(("ocr-worker", profile, threads))

    def _next_id(self) -> str:
        with self._id_lock:
            self._id_counter += 1
            return f"req-{self._id_counter}-{os.getpid()}"

    def _build_env(self) -> dict[str, str]:
        """主进程 → worker spawn 时注入的 env。

        - BLAS 线程数：永远以 ``Settings.ocr_threads`` 为准（覆盖主进程 env）
        - PaddleOCR 反 CDN 检查：以 settings 为准（生产禁用）
        - PYTHON* 编码：保留主进程的设置
        """
        threads = str(int(getattr(self._settings, "ocr_threads", 2)))
        env = dict(os.environ)
        env["OMP_NUM_THREADS"] = threads
        env["OPENBLAS_NUM_THREADS"] = threads
        env["MKL_NUM_THREADS"] = threads
        env["VECLIB_MAXIMUM_THREADS"] = threads
        env["NUMEXPR_NUM_THREADS"] = threads
        env.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    def _spawn(self, fingerprint: int) -> None:
        """实际 spawn worker 子进程。必须在持有 ``_spawn_lock`` 时调用。

        Raises:
            OcrProcessError: spawn 失败或 ready 超时
        """
        args = [
            sys.executable,
            "-u",  # unbuffered
            "-m",
            self._worker_module,
            "--profile",
            str(getattr(self._settings, "ocr_profile", "auto")),
            "--ocr-threads",
            str(int(getattr(self._settings, "ocr_threads", 2))),
            "--model-version",
            "paddleocr-unknown",
        ]
        env = self._build_env()
        log_prefix = str(getattr(self._settings, "ocr_stderr_prefix", "[ocr-worker]"))

        # 用 bufsize=0 让 stdin/stdout 不带 buffer；windows 下也需要 raw 字节流
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=env,
                cwd=os.getcwd(),
            )
        except OSError as exc:
            raise OcrProcessError(f"spawn worker 失败: {exc}") from exc

        self._proc = proc
        self._fingerprint = fingerprint
        self._ready_event = threading.Event()
        self._futures = {}
        self._stdout_thread = StdoutReaderThread(proc, self._futures, self._ready_event)
        self._stderr_thread = StderrRelayThread(proc, prefix=log_prefix)
        self._stdout_thread.start()
        self._stderr_thread.start()

        # 等 worker 发第一条 ready 消息
        timeout = float(
            getattr(self._settings, "ocr_subprocess_timeout", _DEFAULT_SUBPROCESS_TIMEOUT)
        )
        if not self._ready_event.wait(timeout=timeout):
            # ready 超时：worker 启动 30s 还没发出 ready（可能 CDN 拉模型卡了）
            logger.error("OCR worker ready 超时（%ss），强制 kill", timeout)
            self._shutdown_worker(graceful=False)
            self._proc = None
            self._fingerprint = None
            raise OcrProcessError(f"OCR worker ready 超时（{timeout}s）")

        logger.info(
            "OCR worker spawn 成功 | pid=%s | fingerprint=%s",
            proc.pid,
            fingerprint,
        )

    def _shutdown_worker(self, *, graceful: bool) -> None:
        """三阶段 shutdown：先发 shutdown 消息等 2s、terminate 等 1s、kill 等 0.5s。"""
        if self._proc is None or self._proc.poll() is not None:
            return

        if graceful:
            try:
                if self._proc.stdin is not None:
                    shutdown_line = (
                        json.dumps({"action": "shutdown", "id": "_shutdown"}, ensure_ascii=False)
                        + "\n"
                    ).encode("utf-8")
                    self._proc.stdin.write(shutdown_line)
                    self._proc.stdin.flush()
                    try:
                        self._proc.wait(timeout=_SHUTDOWN_GRACEFUL_SEC)
                    except subprocess.TimeoutExpired:
                        pass
            except (BrokenPipeError, OSError):
                # 写不进 → 直接走 terminate
                pass

        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=_SHUTDOWN_TERMINATE_SEC)
                except subprocess.TimeoutExpired:
                    pass
            except (OSError, ProcessLookupError):
                pass

        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=_SHUTDOWN_KILL_SEC)
                except subprocess.TimeoutExpired:
                    pass
            except (OSError, ProcessLookupError):
                pass

        # 清理线程
        if self._stdout_thread and self._stdout_thread.is_alive():
            # reader 是守护线程，等 worker pipe 关闭自然退出
            pass

    # ── 测试钩子 ─────────────────────────────────────────────

    def _is_alive(self) -> bool:
        """供测试用：worker 进程是否活着。"""
        return self._proc is not None and self._proc.poll() is None


# 模块定位:OCR worker 子进程客户端(主进程侧)
#
# 2026-07-14(路线 B):
#   - 本类是主进程侧 IPC 客户端,集中所有子进程细节;
#   - 与 worker `app.common.ocr_worker` 通过 stdin/stdout JSON-line 通讯;
#   - API:`ensure_ready() / predict(image_path) -> str / close()`。
#
# 链路:
#   ocr_service.extract_text → OcrProcessClient.predict → child process
#     → JSON reply → retry / 错误处理 → str 返回给调用方
#
# 关键约束:
#   - 复用进程(Pool),predict 必须 sync 等结果;
#   - 失败 3 次指数退避,超过则 raise OCRUnavailable;
#   - 任何 spawn 都要 cleanup(资源耗尽风险);
#   - 测试必须 mock subprocess,不允许 real subprocess。
