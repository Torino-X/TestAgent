"""Phase 2.8R-G Multi-worker E2E 测试公共 fixture。

设计目标(对应 docs/35 §7 + 验收七):
  * 真实跨进程 PG + Redis(由 docker-compose.test.yml 提供)
  * 每个 test 启动独立 uvicorn 子进程(模拟 Worker B)
  * 14 个 case 覆盖 docs/35 §7.2 列表

⚠️ 本环境无 docker 时,所有 E2E 测试自动 skip(用 ``pytest.skip`` 标记)。
  本地单测 / agent_runtime 单测仍可跑通。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """检测 host:port 是否可达(TCP 三次握手)。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def docker_available() -> bool:
    """检测 docker 是否可用(命令存在 + 端口可达)。"""
    try:
        r = subprocess.run(
            ["docker", "version"],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

    return (
        _port_open("127.0.0.1", 3307)
        or _port_open("127.0.0.1", 5433)
        or _port_open("127.0.0.1", 6380)
    )


# ── Pytest 标记 ──────────────────────────────────────────────────────────────

requires_docker = pytest.mark.skipif(
    not docker_available(),
    reason="docker / PG / Redis not available; skip multi-worker E2E",
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def backend_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def env_test(backend_root: Path) -> dict[str, str]:
    """加载 .env.test;文件不存在时用 .env.test.example fallback。"""
    env_file = backend_root / ".env.test"
    if not env_file.exists():
        env_file = backend_root / ".env.test.example"
    if not env_file.exists():
        return {}

    env = dict(os.environ)
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        env[key.strip()] = val.strip()
    return env


@pytest.fixture(scope="session", autouse=True)
def _require_docker_session(request) -> None:
    """session 级别 skip:若 docker 不可用,所有 multiworker 测试跳过。"""
    if not docker_available():
        pytest.skip(
            "Multi-worker E2E requires docker (docker-compose.test.yml)",
            allow_module_level=True,
        )


# ── 辅助:启动 / 停止 worker 子进程 ──────────────────────────────────────────


class WorkerProcess:
    """真实 uvicorn 子进程,绑定不同 port 模拟 Worker B。"""

    def __init__(self, port: int, env: dict[str, str], backend_root: Path) -> None:
        self.port = port
        self.env = env
        self.backend_root = backend_root
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn",
                "app.main:app",
                "--host", "127.0.0.1",
                "--port", str(self.port),
                "--log-level", "warning",
            ],
            cwd=str(self.backend_root),
            env={**self.env, "PORT": str(self.port)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # 等待端口可连(最多 30 秒)
        for _ in range(30):
            if _port_open("127.0.0.1", self.port, 0.5):
                return
            time.sleep(1)
        self.stop()
        raise RuntimeError(f"Worker did not start on port {self.port}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture
def worker_process_factory(env_test, backend_root):
    """工厂函数 — 测试可启动多个 worker(不同端口)。"""
    workers: list[WorkerProcess] = []

    def _factory(port: int) -> WorkerProcess:
        w = WorkerProcess(port, env_test, backend_root)
        w.start()
        workers.append(w)
        return w

    yield _factory

    for w in workers:
        w.stop()


__all__ = [
    "docker_available",
    "requires_docker",
    "WorkerProcess",
    "worker_process_factory",
]