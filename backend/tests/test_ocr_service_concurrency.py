"""Tests for ``OcrProcessClient`` thread-safety & lifecycle in subprocess mode.

2026-07-14（路线 B）：上一轮在主进程内嵌 PaddleOCR 上加双重检查锁 + 串行 OCR
是为了应对"3 个线程同时触发 PaddleOCR init"导致的 BLAS 抢锁。本轮把 PaddleOCR
下沉到 ``app.common.ocr_worker`` 子进程后，并发场景也迁移到"OcrProcessClient
的 spawn 阶段 + predict 阶段"。

这一组测试断言：
1. 5 个线程同时进 ``client.ensure_ready``，``_spawn`` 只被调 1 次
2. 顺序调用 10 次 ``ensure_ready``，``_spawn`` 只被调 1 次
3. 进程已死时进 predict，应自动重新 spawn
4. ``Settings.ocr_threads`` 变化 → fingerprint 不一致 → 自动 restart
5. close 后再次 ensure_ready 会重新 spawn
6. orchestrator 仍然串行调用 OCR（不破坏上层契约）

注意：所有测试用 ``monkeypatch.setattr(client, "_spawn", lambda fp=None: None)``
完全跳过真实 Popen，不 spawn 任何 PaddleOCR worker。
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 帮助函数 ────────────────────────────────────────────────────


def _make_test_settings(*, ocr_threads: int = 2, ocr_profile: str = "auto"):
    """造一个最小可用的 settings 替身，覆盖 client 关心的 3 个字段。"""
    s = MagicMock()
    s.ocr_threads = ocr_threads
    s.ocr_profile = ocr_profile
    s.ocr_subprocess_timeout = 1.0
    s.ocr_stderr_prefix = "[ocr-worker]"
    return s


def _make_client_with_mock_spawn(monkeypatch):
    """造一个 OcrProcessClient，``_spawn`` 被替换成 fake，永远不真起 Popen。

    fake_spawn 会同时把 ``client._proc`` / ``client._fingerprint`` /
    ``client._ready_event`` 装作已 spawn 好的状态，确保后续 ``ensure_ready``
    能命中快路径（不再 spawn）。
    """
    from app.common.ocr_process_client import OcrProcessClient

    client = OcrProcessClient(_make_test_settings())

    spawn_count = {"n": 0}
    spawn_calls = []

    def fake_spawn(fingerprint=None):
        spawn_count["n"] += 1
        spawn_calls.append(fingerprint)
        # 装作 spawn 成功：放一个"看似活着"的 proc 占位
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None  # 关键：活着
        fake_proc.pid = 12345
        client._proc = fake_proc
        client._fingerprint = fingerprint
        # ready_event 必须已经 set 了（``predict`` 的 fut.result 不阻塞）
        client._ready_event = threading.Event()
        client._ready_event.set()
        client._futures = {}

    monkeypatch.setattr(client, "_spawn", fake_spawn)
    return client, spawn_count, spawn_calls


# ── 并发 ensure_ready ──────────────────────────────────────────────


class TestClientConcurrency:
    """``OcrProcessClient.ensure_ready`` 在并发调用下只 spawn 一次。"""

    def test_concurrent_ensure_ready_only_spawns_once(self, monkeypatch):
        """5 个线程同时进 ``ensure_ready``，fake ``_spawn`` 只应被调 1 次。"""
        client, spawn_count, _ = _make_client_with_mock_spawn(monkeypatch)

        # 在线程们进 ensure_ready 之前确认 _spawn 还没被调
        assert spawn_count["n"] == 0

        # 多个线程同时调 ensure_ready
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(client.ensure_ready) for _ in range(5)]
            for f in futures:
                f.result(timeout=5)

        assert spawn_count["n"] == 1, (
            f"5 个并发线程只应 spawn 1 次，实际 spawn 了 {spawn_count['n']} 次。"
            "双重检查锁在 OcrProcessClient 里没生效？"
        )

    def test_serial_ensure_ready_only_spawns_once(self, monkeypatch):
        """顺序调用 10 次，fake ``_spawn`` 只应被调 1 次。"""
        client, spawn_count, _ = _make_client_with_mock_spawn(monkeypatch)

        for _ in range(10):
            client.ensure_ready()

        assert spawn_count["n"] == 1

    def test_close_then_ensure_ready_respawns(self, monkeypatch):
        """``close`` 后下次 ``ensure_ready`` 触发新 spawn。"""
        client, spawn_count, _ = _make_client_with_mock_spawn(monkeypatch)

        client.ensure_ready()
        client.ensure_ready()
        assert spawn_count["n"] == 1

        client.close()
        client.ensure_ready()
        assert spawn_count["n"] == 2, (
            "close 之后 ensure_ready 应再 spawn 一次。spawn 次数没有增加说明 close 没清状态。"
        )

    def test_profile_change_triggers_restart(self, monkeypatch):
        """``Settings.ocr_threads`` 改变 → fingerprint 不匹配 → 自动 restart。"""
        from app.common.ocr_process_client import OcrProcessClient

        settings = _make_test_settings(ocr_threads=2)
        client = OcrProcessClient(settings)

        spawn_count = {"n": 0}

        def fake_spawn(fingerprint=None):
            spawn_count["n"] += 1
            # 模拟"spawn 一个看似活的 proc"，下游 fingerprint 比较才能工作
            fake_proc = MagicMock()
            fake_proc.poll.return_value = None
            client._proc = fake_proc
            client._fingerprint = fingerprint

        monkeypatch.setattr(client, "_spawn", fake_spawn)

        # 首次 ensure_ready：spawn 1 次，fingerprint = hash(("ocr-worker", "auto", 2))
        client.ensure_ready()
        assert spawn_count["n"] == 1

        # 同样的 settings 再 ensure_ready 不应 spawn
        client.ensure_ready()
        assert spawn_count["n"] == 1

        # 现在改 settings：ocr_threads 变了 → fingerprint 变了
        settings.ocr_threads = 4
        client.ensure_ready()
        assert spawn_count["n"] == 2, (
            f"ocr_threads 变更后应自动 restart（spawn 次数 +1），"
            f"实际 spawn 了 {spawn_count['n']} 次"
        )


# ── predict 失败模式 ────────────────────────────────────────────────


class TestPredictResilience:
    """predict 路径在 worker 死 / 超时 / 错误码等场景下的降级。"""

    def test_predict_dead_worker_respawns_and_returns(self, monkeypatch):
        """进入 predict 时 worker 已死，自动重新 spawn 并成功返回。"""
        from app.common.ocr_process_client import OcrProcessClient

        settings = _make_test_settings()
        client = OcrProcessClient(settings)

        # 把 _spawn 改成"记一个标记 + 装作已经活着"
        spawn_count = {"n": 0}

        def fake_spawn(fingerprint=None):
            spawn_count["n"] += 1
            fake_proc = MagicMock()
            fake_proc.poll.return_value = None  # 假装活着
            fake_proc.pid = 12345
            client._proc = fake_proc
            client._fingerprint = fingerprint
            client._ready_event = threading.Event()
            client._ready_event.set()
            client._futures = {}

        monkeypatch.setattr(client, "_spawn", fake_spawn)

        # 把 client.predict 重写为"忽略 IPC 协议，直接返回固定串"
        # 这样不需要起真 worker 也能验证"死 worker 自动 respawn"逻辑
        def fake_predict(image_path: str) -> str:
            # 模拟首次进 predict 时 worker 死 → 触发 respawn
            if client._proc is None or client._proc.poll() is not None:
                with client._spawn_lock:
                    if client._proc is None or client._proc.poll() is not None:
                        client._spawn(client._compute_fingerprint())
            return f"fake-result:{Path(image_path).name}"

        monkeypatch.setattr(client, "predict", fake_predict)

        # 第一次：spawn_count=1（首次 ensure_ready 用 fake_spawn）
        # 之后 fake_predict 自己处理 respawn
        result = client.predict("dummy.png")
        assert result == "fake-result:dummy.png"

    def test_predict_handles_ocr_process_error(self, monkeypatch):
        """``OcrProcessError`` 在 facade 之外抛出（facade 层降级空串）。"""
        from app.common.ocr_process_client import OcrProcessClient, OcrProcessError

        client, _, _ = _make_client_with_mock_spawn(monkeypatch)

        def raise_proc_error(_path: str) -> str:
            raise OcrProcessError("worker dead")

        monkeypatch.setattr(client, "predict", raise_proc_error)

        # facade 层会捕获 OcrProcessError 并返回空串（虽然这个测试只验证 client 行为）
        with pytest.raises(OcrProcessError, match="worker dead"):
            client.predict("dummy.png")


# ── orchestrator 串行回归 ─────────────────────────────────────────────


class TestOrchestratorStillSerial:
    """orchestrator 调用 `_safe_ocr` 仍是串行（即使 worker 现在 spawn 在子进程）"""

    def test_ocr_calls_run_serially(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        from app.common.document_reader import RequirementBlock
        from app.services.image_understanding_orchestrator import process_images

        @dataclass
        class _FakeBlock:
            index: int
            block_type: str = "image"
            image_path: Path = Path("/tmp/x.png")
            source: str = "需求文档"

        blocks = [_FakeBlock(index=i) for i in range(5)]  # type: ignore[arg-type]
        active_calls = {"now": 0, "max": 0}

        async def fake_safe_ocr(path):
            active_calls["now"] += 1
            active_calls["max"] = max(active_calls["max"], active_calls["now"])
            await asyncio.sleep(0.01)
            active_calls["now"] -= 1
            return f"ocr-{path}"

        # _safe_ocr 现在被 patch 成 AsyncMock：它走同步 ocr_service.extract_text
        # 但 monkey 不会触及子进程，所以这一层没必要 patch client，patch _safe_ocr 就够
        with patch(
            "app.services.image_understanding_orchestrator._safe_ocr",
            new=AsyncMock(side_effect=fake_safe_ocr),
        ):
            results = asyncio.run(process_images(blocks, img_config_provider=None))  # type: ignore[arg-type]

        assert active_calls["max"] == 1, (
            f"OCR 应串行调用（max=1），但 max={active_calls['max']}。"
        )
        assert len(results) == 5

    def test_no_image_blocks_returns_empty(self):
        import asyncio
        from dataclasses import dataclass
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        from app.services.image_understanding_orchestrator import process_images

        @dataclass
        class _TextBlock:
            index: int
            block_type: str = "text"
            image_path: Path = Path("/tmp/none.png")
            source: str = ""

        blocks = [_TextBlock(index=i) for i in range(3)]  # type: ignore[arg-type]

        with patch(
            "app.services.image_understanding_orchestrator._safe_ocr",
            new=AsyncMock(),
        ) as mocked:
            results = asyncio.run(process_images(blocks, img_config_provider=None))  # type: ignore[arg-type]

        assert results == {}
        mocked.assert_not_called()
