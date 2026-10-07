#!/usr/bin/env python3
"""Run real API E2E checks for every development fault-injection scenario.

The script deliberately exercises the same public HTTP APIs as the frontend:

    login -> create conversation -> upload documents -> confirm file types
    -> start test-plan task -> answer pending confirmations -> wait for terminal

It never imports repositories or writes the application database.  Conversations
and their artifacts are intentionally retained for manual inspection.  The only
local configuration it changes is the documented fault-injection block in
``backend/.env``; the original file is restored in ``finally``.

Important: the fault-injection settings are read from ``os.environ`` by the
backend process.  Therefore every scenario must restart the backend after the
``.env`` update.  By default this script owns that lifecycle.  Pass
``--no-manage-backend`` only when an operator will restart the service between
scenarios themselves.

Credentials are intentionally not read from ``.env`` or logged.  Set these in
the shell that runs the script:

    $env:TESTAGENT_E2E_ACCOUNT = "your-account"
    $env:TESTAGENT_E2E_PASSWORD = "your-password"

Optional environment variables:

    TESTAGENT_E2E_BASE_URL       default: http://127.0.0.1:8003
    TESTAGENT_E2E_TIMEOUT_S      default: 1500 seconds per scenario
    TESTAGENT_E2E_ARTIFACT_ROOT  default: backend/tmp/repair_fault_e2e

Examples:

    python scripts/e2e_repair_fault_injection.py
    python scripts/e2e_repair_fault_injection.py --scenario result_review.json_truncate
    python scripts/e2e_repair_fault_injection.py --reset-progress
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_ROOT / "backend"
ENV_PATH = BACKEND_DIR / ".env"
REQUIREMENT_DOCUMENT = (
    PROJECT_ROOT / "docs" / "素材库" / "01_智慧校园预约与签到系统_需求说明书.docx"
)
TEMPLATE_DOCUMENT = (
    PROJECT_ROOT / "docs" / "素材库" / "TestAgent_测试方案模板_V1.docx"
)

SCENARIOS: tuple[str, ...] = (
    "result_review.schema_header_mismatch",
    "result_review.missing_section",
    "result_review.json_truncate",
    "result_review.empty_section_content",
    "result_review.section_invalid_value",
    "result_review.extra_table_without_placeholder",
    "result_review.multiple_tables_for_single_placeholder",
    "result_review.missing_table_for_placeholder",
    "result_review.table_row_not_object",
    "format_loss.bookmark_simulate",
)

ENV_KEYS = (
    "FAULT_INJECTION_ENABLED",
    "FAULT_INJECTION_ALLOWED_ENV",
    "FAULT_INJECTION_TASK_IDS",
    "FAULT_INJECTION_SCENARIOS",
)
TOKEN_PATTERN = re.compile(r"(?i)(bearer\s+)[^\s\"']+|\bsk-[A-Za-z0-9_-]{8,}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_text(value: Any, *, limit: int = 8_000) -> str:
    """Bound and redact strings before writing diagnostic artifacts."""
    text = str(value)
    text = TOKEN_PATTERN.sub(lambda match: f"{match.group(1) if match.group(1) else ''}[REDACTED]", text)
    return text[:limit]


def json_safe(value: Any) -> Any:
    """Make response/event objects safe and serialisable for the run log."""
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, str):
        return safe_text(value)
    return value


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    write_json(temporary, payload)
    temporary.replace(path)


def environment_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}。该脚本不会从 .env 读取测试账号或密码。")
    return value


@dataclass
class ScenarioRecord:
    scenario: str
    attempt: int = 1
    injection_evidence_version: int = 2
    started_at: str = field(default_factory=utc_now)
    completed_at: str | None = None
    status: str = "running"
    conversation_id: str | None = None
    task_id: str | None = None
    run_dir: str | None = None
    failure: str | None = None
    terminal_status: str | None = None
    fault_triggered: bool = False


class E2EApi:
    """Small HTTP-only client for the public frontend APIs."""

    def __init__(self, base_url: str, *, log: list[dict[str, Any]]) -> None:
        self.base_url = base_url.rstrip("/")
        self._log = log
        self._client = httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))
        self._token = ""

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._client.request(method, f"{self.base_url}{path}", headers=self._headers(), **kwargs)
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = {"raw_text": safe_text(response.text, limit=2_000)}
        self._log.append({
            "at": utc_now(),
            "method": method,
            "path": path,
            "status_code": response.status_code,
            "response": json_safe(body),
        })
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code} {method} {path}: {safe_text(body, limit=2_000)}")
        if not isinstance(body, dict):
            raise RuntimeError(f"{method} {path} returned a non-object response")
        if body.get("code", 0) != 0:
            raise RuntimeError(f"API error {method} {path}: {safe_text(body, limit=2_000)}")
        return body

    def login(self, account: str, password: str) -> None:
        body = self._request("POST", "/api/auth/login", json={"username": account, "password": password})
        data = body.get("data") or {}
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("登录响应缺少 token")
        self._token = token

    def create_conversation(self, title: str) -> str:
        data = self._request("POST", "/api/conversations", json={"title": title}).get("data") or {}
        conversation_id = data.get("id") or data.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise RuntimeError(f"创建会话响应缺少 id: {safe_text(data)}")
        return conversation_id

    def upload_file(self, conversation_id: str, file_path: Path) -> str:
        with file_path.open("rb") as stream:
            data = self._request(
                "POST",
                "/api/files/upload",
                data={"conversation_id": conversation_id},
                files={"file": (file_path.name, stream, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            ).get("data") or {}
        file_id = data.get("id") or data.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            raise RuntimeError(f"上传 {file_path.name} 后缺少 file id: {safe_text(data)}")
        return file_id

    def confirm_file_type(self, file_id: str, file_type: str) -> None:
        self._request("POST", f"/api/files/{file_id}/confirm-type", json={"file_type": file_type})

    def start_task(self, conversation_id: str, file_ids: list[str]) -> str:
        body = self._request(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            json={
                "content": "请根据已上传的需求文档和测试方案模板生成完整测试方案。",
                "attached_file_ids": file_ids,
            },
        )
        data = body.get("data") or {}
        task = data.get("agent_task") or {}
        task_id = task.get("task_id") or data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise RuntimeError(f"生成任务未创建: {safe_text(data, limit=4_000)}")
        return task_id

    def task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/agent/tasks/{task_id}").get("data") or {}

    def events(self, task_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/api/agent/tasks/{task_id}/event-list?limit=100").get("data") or {}
        events = data.get("events", []) if isinstance(data, dict) else []
        return [item for item in events if isinstance(item, dict)]

    def pending_confirmation(self, task_id: str) -> dict[str, Any] | None:
        response = self._client.get(f"{self.base_url}/api/agent/tasks/{task_id}/pending-confirmation", headers=self._headers())
        try:
            body: Any = response.json()
        except ValueError:
            body = {"raw_text": safe_text(response.text, limit=2_000)}
        self._log.append({
            "at": utc_now(), "method": "GET", "path": f"/api/agent/tasks/{task_id}/pending-confirmation",
            "status_code": response.status_code, "response": json_safe(body),
        })
        if response.status_code == 404 or (isinstance(body, dict) and body.get("code") == 40401):
            return None
        if response.status_code >= 400 or not isinstance(body, dict) or body.get("code", 0) != 0:
            raise RuntimeError(f"读取待确认信息失败: {safe_text(body, limit=2_000)}")
        data = body.get("data")
        return data if isinstance(data, dict) else None

    def submit_clarification(self, task_id: str, pending: dict[str, Any]) -> None:
        answers: dict[str, str] = {}
        for index, card in enumerate(pending.get("cards") or []):
            if not isinstance(card, dict):
                continue
            card_id = str(card.get("id") or card.get("key") or f"clarification_{index}")
            answers[card_id] = first_option_value(card) or "按默认建议执行"
        self._request(
            "POST",
            f"/api/agent/tasks/{task_id}/preparation-clarification",
            json={"answers": answers, "conservative_gap_ids": []},
        )

    def confirm_sections(self, task_id: str, pending: dict[str, Any]) -> None:
        sections: list[dict[str, str]] = []
        for item in pending.get("sections") or []:
            if not isinstance(item, dict):
                continue
            section_id = item.get("section_id")
            action = item.get("suggested_action") or item.get("action")
            if isinstance(section_id, str) and section_id and isinstance(action, str) and action:
                sections.append({"section_id": section_id, "action": action})
        if not sections:
            raise RuntimeError("章节确认卡片没有可提交的默认章节策略")
        self._request("POST", f"/api/agent/tasks/{task_id}/confirm", json={"sections": sections})

    def accept_format_loss(self, task_id: str) -> None:
        self._request(
            "POST",
            f"/api/agent/tasks/{task_id}/format-loss-decision",
            json={"decision": "accept", "note": "E2E default first decision"},
        )


def first_option_value(card: dict[str, Any]) -> str | None:
    """Choose the first displayed option without inventing a choice."""
    for key in ("options", "choices", "suggestions"):
        values = card.get(key)
        if not isinstance(values, list) or not values:
            continue
        first = values[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
        if isinstance(first, dict):
            for value_key in ("value", "label", "text", "id"):
                value = first.get(value_key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


class BackendLifecycle:
    """Safely replace the listener on the API port with a scenario-specific app."""

    def __init__(self, base_url: str, run_dir: Path, *, manage: bool) -> None:
        parsed = urlparse(base_url)
        if parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
            raise RuntimeError("自动重启仅支持本机带显式端口的 TESTAGENT_E2E_BASE_URL")
        self.host = parsed.hostname
        self.port = parsed.port
        self.run_dir = run_dir
        self.manage = manage
        self.process: subprocess.Popen[str] | None = None

    @staticmethod
    def _listener_pids(port: int) -> list[int]:
        result = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, check=False)
        pids: set[int] = set()
        suffix = f":{port}"
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) < 5 or fields[0].upper() != "TCP" or fields[3].upper() != "LISTENING":
                continue
            if fields[1].endswith(suffix):
                try:
                    pids.add(int(fields[-1]))
                except ValueError:
                    continue
        return sorted(pid for pid in pids if pid != os.getpid())

    def restart(self) -> None:
        if not self.manage:
            return
        self.stop_owned_process()
        stopped: list[dict[str, Any]] = []
        for pid in self._listener_pids(self.port):
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, check=False)
            stop_result = {
                "pid": pid,
                "returncode": result.returncode,
                "stdout": safe_text(result.stdout),
                "stderr": safe_text(result.stderr),
            }
            stopped.append(stop_result)
            if result.returncode != 0:
                write_json(
                    self.run_dir / "backend_restart.json",
                    {"at": utc_now(), "stopped_listeners": stopped},
                )
                raise RuntimeError(
                    "无法接管 E2E 后端端口 "
                    f"{self.host}:{self.port}: PID {pid} 未能终止。"
                    "请以相同权限运行控制器，或先停止该进程；不会继续创建混淆的测试会话。"
                )
        write_json(self.run_dir / "backend_restart.json", {"at": utc_now(), "stopped_listeners": stopped})
        log_file = (self.run_dir / "backend.log").open("w", encoding="utf-8")
        command = [sys.executable, "scripts/run_dev.py", "--host", self.host, "--port", str(self.port)]
        self.process = subprocess.Popen(command, cwd=BACKEND_DIR, stdout=log_file, stderr=subprocess.STDOUT, text=True)
        # The file handle is inherited by the child process and must remain open.
        setattr(self.process, "_e2e_log_file", log_file)
        write_json(
            self.run_dir / "backend_restart.json",
            {
                "at": utc_now(),
                "stopped_listeners": stopped,
                "launch_pid": self.process.pid,
                "command": command,
            },
        )

    def stop_owned_process(self) -> None:
        if self.process is None:
            return
        # On Windows run_dev.py owns a separate Uvicorn child.  Terminating
        # only the launcher leaks that child, so end the full tracked tree.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
        finally:
            log_file = getattr(self.process, "_e2e_log_file", None)
            if log_file is not None:
                log_file.close()


def patch_fault_env(original: str, scenario: str) -> str:
    desired = {
        "FAULT_INJECTION_ENABLED": "1",
        "FAULT_INJECTION_ALLOWED_ENV": "development,dev,local,test",
        "FAULT_INJECTION_TASK_IDS": "",
        "FAULT_INJECTION_SCENARIOS": scenario,
    }
    found: set[str] = set()
    lines: list[str] = []
    for line in original.splitlines(keepends=True):
        match = re.match(r"^(FAULT_INJECTION_[A-Z_]+)\s*=.*?(\r?\n)?$", line)
        if match and match.group(1) in desired:
            key = match.group(1)
            newline = match.group(2) or "\n"
            lines.append(f"{key}={desired[key]}{newline}")
            found.add(key)
        else:
            lines.append(line)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines.append("\n")
    for key in ENV_KEYS:
        if key not in found:
            lines.append(f"{key}={desired[key]}\n")
    return "".join(lines)


def wait_for_ready(base_url: str, lifecycle: BackendLifecycle, *, timeout_s: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error = "not attempted"
    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline:
            if lifecycle.process is not None and lifecycle.process.poll() is not None:
                raise RuntimeError(f"后端进程在就绪前退出，exit={lifecycle.process.returncode}；查看 {lifecycle.run_dir / 'backend.log'}")
            try:
                response = client.get(f"{base_url.rstrip('/')}/api/health/readiness")
                payload = response.json()
                if response.status_code == 200 and payload.get("code") == 0 and (payload.get("data") or {}).get("status") == "ready":
                    return
                last_error = safe_text(payload, limit=1_500)
            except Exception as exc:  # connection can legitimately fail during boot
                last_error = f"{type(exc).__name__}: {safe_text(exc)}"
            time.sleep(2)
    raise RuntimeError(f"后端在 {timeout_s:.0f}s 内未就绪: {last_error}")


def fault_triggered(backend_log: Path, scenario: str) -> bool:
    if not backend_log.exists():
        return False
    content = backend_log.read_text(encoding="utf-8", errors="replace")
    # ``TRIGGERED`` is emitted before scenario.apply().  A malformed upstream
    # model response can make apply() raise and fall back to the untouched
    # payload, which is not a valid fault-injection test.  Require the
    # post-apply evidence added by the development-only injection engine.
    return "FAULT_INJECTION_APPLIED" in content and scenario in content


def handle_pending_confirmation(api: E2EApi, task_id: str, task: dict[str, Any], actions: list[dict[str, Any]]) -> bool:
    status = str(task.get("status") or "")
    if status == "format_loss_review":
        api.accept_format_loss(task_id)
        actions.append({"at": utc_now(), "type": "format_loss", "decision": "accept"})
        return True
    pending = api.pending_confirmation(task_id)
    if not pending:
        return False
    kind = str(pending.get("confirmation_type") or "")
    if kind in {"preparation_clarification", "clarification"}:
        api.submit_clarification(task_id, pending)
        actions.append({"at": utc_now(), "type": kind, "decision": "first_option_or_default"})
        return True
    if kind in {"section_confirmation", "section_confirm", "sections"} or pending.get("sections"):
        api.confirm_sections(task_id, pending)
        actions.append({"at": utc_now(), "type": kind or "section_confirmation", "decision": "suggested_action"})
        return True
    raise RuntimeError(f"未知待确认类型，不能安全选择默认项: {safe_text(pending, limit=4_000)}")


def run_scenario(
    scenario: str,
    *,
    base_url: str,
    account: str,
    password: str,
    run_root: Path,
    original_env: str,
    timeout_s: float,
    manage_backend: bool,
    attempt: int = 1,
) -> ScenarioRecord:
    slug = scenario.replace(".", "_")
    run_dir = run_root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}"
    run_dir.mkdir(parents=True, exist_ok=False)
    record = ScenarioRecord(scenario=scenario, attempt=attempt, run_dir=str(run_dir))
    requests_log: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    lifecycle = BackendLifecycle(base_url, run_dir, manage=manage_backend)
    api = E2EApi(base_url, log=requests_log)
    try:
        ENV_PATH.write_text(patch_fault_env(original_env, scenario), encoding="utf-8")
        lifecycle.restart()
        wait_for_ready(base_url, lifecycle)

        api.login(account, password)
        record.conversation_id = api.create_conversation(f"RepairAgent 故障注入 E2E - {scenario} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        requirement_id = api.upload_file(record.conversation_id, REQUIREMENT_DOCUMENT)
        template_id = api.upload_file(record.conversation_id, TEMPLATE_DOCUMENT)
        api.confirm_file_type(requirement_id, "requirement_doc")
        api.confirm_file_type(template_id, "test_plan_template")
        record.task_id = api.start_task(record.conversation_id, [requirement_id, template_id])

        deadline = time.monotonic() + timeout_s
        last_event_signature: tuple[str, ...] = ()
        while time.monotonic() < deadline:
            task = api.task(record.task_id)
            events = api.events(record.task_id)
            signature = tuple(str(event.get("event_id") or event.get("canonical_order") or index) for index, event in enumerate(events))
            if signature != last_event_signature:
                write_json(run_dir / "events.json", {"task_id": record.task_id, "events": events})
                last_event_signature = signature
            status = str(task.get("status") or "")
            if status in {"completed", "failed", "cancelled"}:
                record.terminal_status = status
                break
            if status in {"waiting_user_confirm", "format_loss_review"}:
                if handle_pending_confirmation(api, record.task_id, task, actions):
                    write_json(run_dir / "human_actions.json", {"task_id": record.task_id, "actions": actions})
                    time.sleep(1)
                    continue
            time.sleep(3)
        else:
            raise TimeoutError(f"任务在 {timeout_s:.0f}s 内未进入终态")

        if record.terminal_status != "completed":
            raise RuntimeError(f"任务终态为 {record.terminal_status}，不是 completed")
        if manage_backend and not fault_triggered(run_dir / "backend.log", scenario):
            raise RuntimeError("任务完成但后端日志未出现该场景的 FAULT_INJECTION_APPLIED 证据")
        record.status = "passed"
    except Exception as exc:  # preserve full diagnostic package, then let caller stop/resume safely
        record.status = "failed"
        record.failure = f"{type(exc).__name__}: {safe_text(exc, limit=4_000)}"
        (run_dir / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        record.completed_at = utc_now()
        # A model-generation failure can occur before the post-generation
        # injection hook.  Preserve that real run, but flag it as inconclusive.
        record.fault_triggered = (not manage_backend) or fault_triggered(run_dir / "backend.log", scenario)
        write_json(run_dir / "requests.json", {"requests": requests_log})
        write_json(run_dir / "events.json", {"task_id": record.task_id, "events": events})
        write_json(run_dir / "human_actions.json", {"task_id": record.task_id, "actions": actions})
        write_json(run_dir / "scenario_result.json", record.__dict__)
        lifecycle.stop_owned_process()
        api.close()
    return record


def load_progress(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "updated_at": utc_now(), "scenarios": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"进度文件损坏，不能安全决定跳过哪些场景: {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("scenarios"), dict):
        raise RuntimeError(f"进度文件结构无效: {path}")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", action="append", choices=SCENARIOS, help="only run one named scenario; may be repeated")
    parser.add_argument("--reset-progress", action="store_true", help="forget only script progress; retained conversations are untouched")
    parser.add_argument("--no-manage-backend", action="store_true", help="do not stop/start backend; operator must restart it after every .env change")
    parser.add_argument("--timeout-s", type=float, default=float(os.getenv("TESTAGENT_E2E_TIMEOUT_S", "1500")))
    parser.add_argument(
        "--max-inconclusive-attempts",
        type=int,
        default=int(os.getenv("TESTAGENT_E2E_MAX_INCONCLUSIVE_ATTEMPTS", "3")),
        help="fresh-session retries when a run ends before its fault injection hook triggers (default: 3)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_inconclusive_attempts < 1:
        raise ValueError("--max-inconclusive-attempts must be at least 1")
    account = environment_value("TESTAGENT_E2E_ACCOUNT")
    password = environment_value("TESTAGENT_E2E_PASSWORD")
    if not ENV_PATH.exists():
        raise RuntimeError(f"找不到故障注入配置文件: {ENV_PATH}")
    for file_path in (REQUIREMENT_DOCUMENT, TEMPLATE_DOCUMENT):
        if not file_path.exists():
            raise RuntimeError(f"找不到真实测试素材: {file_path}")
    base_url = os.getenv("TESTAGENT_E2E_BASE_URL", "http://127.0.0.1:8003").rstrip("/")
    run_root = Path(os.getenv("TESTAGENT_E2E_ARTIFACT_ROOT", str(BACKEND_DIR / "tmp" / "repair_fault_e2e")))
    run_root.mkdir(parents=True, exist_ok=True)
    progress_path = run_root / "progress.json"
    if args.reset_progress and progress_path.exists():
        progress_path.unlink()
    progress = load_progress(progress_path)
    selected = args.scenario or list(SCENARIOS)
    original_env = ENV_PATH.read_text(encoding="utf-8")
    original_backup = run_root / f"env-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    original_backup.write_text(original_env, encoding="utf-8")
    print(f"真实 RepairAgent 故障注入 E2E：{len(selected)} 个场景；日志目录：{run_root}")
    print("会话和产物会保留；脚本仅通过 HTTP API 创建业务数据，不直接访问数据库。")
    try:
        for scenario in selected:
            existing = progress["scenarios"].get(scenario)
            if (
                isinstance(existing, dict)
                and existing.get("status") == "passed"
                and existing.get("injection_evidence_version") == 2
                and existing.get("fault_triggered") is True
            ):
                print(f"[SKIP] {scenario} 已通过，保留既有会话 {existing.get('conversation_id', '')}")
                continue
            history = progress.setdefault("attempt_history", {}).setdefault(scenario, [])
            if isinstance(existing, dict) and not history:
                prior = dict(existing)
                prior["fault_triggered"] = fault_triggered(Path(str(prior.get("run_dir") or "")) / "backend.log", scenario)
                history.append(prior)
            for attempt in range(1, args.max_inconclusive_attempts + 1):
                print(f"[RUN ] {scenario} (attempt {attempt}/{args.max_inconclusive_attempts})")
                record = run_scenario(
                    scenario,
                    base_url=base_url,
                    account=account,
                    password=password,
                    run_root=run_root,
                    original_env=original_env,
                    timeout_s=args.timeout_s,
                    manage_backend=not args.no_manage_backend,
                    attempt=attempt,
                )
                record_payload = dict(record.__dict__)
                history.append(record_payload)
                progress["scenarios"][scenario] = record_payload
                progress["updated_at"] = utc_now()
                atomic_write_json(progress_path, progress)
                print(f"[{record.status.upper():5}] {scenario} | conversation={record.conversation_id or '-'} | task={record.task_id or '-'}")
                if record.status == "passed":
                    break
                if record.conversation_id is None:
                    print("[BLOCKED] scenario never reached conversation creation; stop without consuming fault-injection retries")
                    print(f"Failure details were written to: {record.run_dir}")
                    return 1
                if not record.fault_triggered and attempt < args.max_inconclusive_attempts:
                    print("[RETRY] fault injection did not trigger; preserving this run and retrying in a fresh conversation")
                    continue
                print(f"Failure details were written to: {record.run_dir}")
                return 1
        print("所有选定的故障注入场景均已通过。")
        return 0
    finally:
        # Exact original content is restored even on errors or Ctrl+C.
        ENV_PATH.write_text(original_env, encoding="utf-8")
        if not args.no_manage_backend:
            restore_dir = run_root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-restore-default-env"
            restore_dir.mkdir(parents=True, exist_ok=True)
            restorer = BackendLifecycle(base_url, restore_dir, manage=True)
            try:
                restorer.restart()
                wait_for_ready(base_url, restorer)
                print("已按恢复后的 .env 重启后端。")
            except Exception as exc:  # Never hide the primary scenario error.
                write_json(restore_dir / "restore_failure.json", {
                    "at": utc_now(), "error": f"{type(exc).__name__}: {safe_text(exc)}",
                })
                restorer.stop_owned_process()
                print(f"警告：.env 已恢复，但恢复后的后端未能就绪；查看 {restore_dir}", file=sys.stderr)
        print("已恢复 backend/.env 的原始故障注入配置。")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("已中断；进度文件保留，下一次会从首个未通过场景继续。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"E2E 控制器无法启动：{type(exc).__name__}: {safe_text(exc)}", file=sys.stderr)
        raise SystemExit(2)
