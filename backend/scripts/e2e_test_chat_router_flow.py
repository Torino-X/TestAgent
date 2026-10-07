#!/usr/bin/env python3
"""F013 Chat Router E2E — end-to-end HTTP test for intent routing.

Exercises all five routes added by F013:
  Phase 1: general_chat       → route=chat_reply
  Phase 2: ask_for_files     → route=ask_for_files
  Phase 3: agent_task        → route=agent_task (full agent flow reuse)
  Phase 4: unsupported       → route=unsupported

Uses REAL LLM (qwen3.7-max).  No MockLLMClient.

Reuses patterns and helpers from ``e2e_test_full_agent_flow.py`` —
does not import that script to keep concerns separated, but the
shared ``E2EClient`` shape is duplicated (a few dozen lines).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx
import pymysql
from docx import Document

# ── Project root ───────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
_env_file = _PROJECT_ROOT / ".env"
if _env_file.exists():
    load_dotenv(_env_file)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("e2e_chat_router")

# ── Config ─────────────────────────────────────────────────────────
BASE_URL = os.getenv("E2E_BASE_URL", "http://127.0.0.1:8000")
API_PREFIX = "/api"
REQ_DOC_PATH = _PROJECT_ROOT / "test_doc" / "智慧校园综合服务平台_项目需求文档_30图测试版.docx"
TPL_DOC_PATH = _PROJECT_ROOT / "test_doc" / "00_PlanWise_QA_测试方案模板.docx"

# Test user credentials (from seed data)
TEST_USERNAME = "e2e_test"
TEST_PASSWORD = "E2eTest@123"

# F014 closeout: Phase 3b (post-confirm real LLM generation) can take
# many minutes on a busy model.  Default the SSE timeout to a much
# larger value (1500s = 25 min) so a slow real run still completes.
# Override via E2E_PHASE3B_TIMEOUT_S env var.  Set E2E_SKIP_PHASE3B=1
# to skip the heavy generation phase entirely — the chat-routing E2E
# then validates only the route-dispatch + agent_task creation +
# pre-confirm SSE, and full generation is covered by
# ``e2e_test_full_agent_flow.py``.
PHASE3B_TIMEOUT_S = float(os.getenv("E2E_PHASE3B_TIMEOUT_S", "1500"))
SKIP_PHASE3B = os.getenv("E2E_SKIP_PHASE3B", "0") == "1"

# MySQL connection (for DB verification)
DB_CONFIG = {
    "host": "49.235.42.163",
    "port": 3366,
    "user": "root",
    "password": "TestAgent",
    "database": "testagent",
    "charset": "utf8mb4",
}

# ── Helpers ────────────────────────────────────────────────────────


def print_section(title: str) -> None:
    print()
    print("=" * 74)
    print(f"  {title}")
    print("=" * 74)
    print()


def print_check(label: str, passed: bool, detail: str = "") -> bool:
    status = "PASS" if passed else "FAIL"
    line = f"  [{status}] {label}"
    if detail:
        line += f" -- {detail}"
    print(line)
    return passed


class E2EClient:
    """Thin wrapper around httpx.AsyncClient with auth state."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=120.0)
        self.token: str = ""
        self.user_id: str = ""

    async def close(self):
        await self._client.aclose()

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def login(self, username: str, password: str) -> dict:
        resp = await self._client.post(
            f"{self.base}/api/auth/login",
            json={"username": username, "password": password},
        )
        resp.raise_for_status()
        data = resp.json()
        assert data.get("code") == 0, f"Login failed: {data.get('message')}"
        self.token = data["data"]["token"]
        self.user_id = data["data"]["user"]["user_id"]
        return data

    async def create_conversation(self, title: str) -> dict:
        resp = await self._client.post(
            f"{self.base}/api/conversations",
            json={"title": title},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def upload_file(self, filepath: Path, conversation_id: str) -> dict:
        with open(filepath, "rb") as f:
            resp = await self._client.post(
                f"{self.base}/api/files/upload",
                data={"conversation_id": conversation_id},
                files={"file": (filepath.name, f, "application/octet-stream")},
                headers=self._auth_headers(),
            )
        resp.raise_for_status()
        return resp.json()

    async def confirm_file_type(self, file_id: str, file_type: str) -> dict:
        resp = await self._client.post(
            f"{self.base}/api/files/{file_id}/confirm-type",
            json={"file_type": file_type},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def list_files(self, conversation_id: str) -> dict:
        resp = await self._client.get(
            f"{self.base}/api/conversations/{conversation_id}/files",
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def send_message(
        self, conversation_id: str, content: str, attached_file_ids: list[str]
    ) -> dict:
        resp = await self._client.post(
            f"{self.base}/api/conversations/{conversation_id}/messages",
            json={
                "content": content,
                "attached_file_ids": attached_file_ids,
            },
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def get_task(self, task_id: str) -> dict:
        resp = await self._client.get(
            f"{self.base}/api/agent/tasks/{task_id}",
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def get_pending_confirmation(self, task_id: str) -> dict:
        resp = await self._client.get(
            f"{self.base}/api/agent/tasks/{task_id}/pending-confirmation",
            headers=self._auth_headers(),
        )
        return resp.json()

    async def sse_events(self, task_id: str, timeout: float = 90.0):
        async with self._client.stream(
            "GET",
            f"{self.base}/api/agent/tasks/{task_id}/events",
            headers=self._auth_headers(),
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            current_event_type = ""
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    current_event_type = line[len("event: "):].strip()
                elif line.startswith("data: "):
                    data_str = line[len("data: "):]
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        data = {"raw": data_str}
                    if "event_type" not in data:
                        data["event_type"] = current_event_type
                    yield data

    async def sse_events_post_confirm(self, task_id: str, timeout: float = 300.0):
        async with self._client.stream(
            "GET",
            f"{self.base}/api/agent/tasks/{task_id}/events-post-confirm",
            headers=self._auth_headers(),
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            current_event_type = ""
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    current_event_type = line[len("event: "):].strip()
                elif line.startswith("data: "):
                    data_str = line[len("data: "):]
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        data = {"raw": data_str}
                    if "event_type" not in data:
                        data["event_type"] = current_event_type
                    yield data

    async def confirm_task(self, task_id: str, sections: list[dict]) -> dict:
        resp = await self._client.post(
            f"{self.base}/api/agent/tasks/{task_id}/confirm",
            json={"sections": sections},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def list_task_artifacts(self, task_id: str) -> dict:
        resp = await self._client.get(
            f"{self.base}/api/agent/tasks/{task_id}/artifacts",
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def get_artifact_detail(self, artifact_id: str) -> dict:
        resp = await self._client.get(
            f"{self.base}/api/artifacts/{artifact_id}",
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def download_artifact(self, artifact_id: str) -> bytes:
        resp = await self._client.get(
            f"{self.base}/api/artifacts/{artifact_id}/download",
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.content


def count_messages_for_conversation(conv_id: int) -> int:
    conn = pymysql.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = %s",
            (conv_id,),
        )
        return int(cur.fetchone()[0])
    finally:
        conn.close()


def get_conversation_internal_id(public_id: str) -> int | None:
    conn = pymysql.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM conversations WHERE public_id = %s", (public_id,)
        )
        row = cur.fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


# ── Main flow ──────────────────────────────────────────────────────


async def main() -> int:
    run_id = uuid.uuid4().hex[:8]
    all_checks: list[tuple[str, bool, str]] = []
    client = E2EClient(BASE_URL)

    try:
        # ── 0. Setup ───────────────────────────────────────────────
        print_section("Phase 0: Setup — login + create conversations")
        await client.login(TEST_USERNAME, TEST_PASSWORD)
        all_checks.append(("00. 登录成功", True, f"user={client.user_id}"))
        print_check(*all_checks[-1])

        conv_chat = (await client.create_conversation(
            f"F013_普通问答_{run_id}"
        ))["data"]["id"]
        conv_agent = (await client.create_conversation(
            f"F013_测试方案生成_{run_id}"
        ))["data"]["id"]
        conv_unsupp = (await client.create_conversation(
            f"F013_暂未开放_{run_id}"
        ))["data"]["id"]
        all_checks.append(("00.1 创建 3 个独立会话", True, f"chat={conv_chat[:10]}… agent={conv_agent[:10]}…"))
        print_check(*all_checks[-1])

        # ── 1. Phase 1 — General chat (chat_reply) ───────────────
        print_section("Phase 1: General Chat (route=chat_reply)")
        msg_resp = await client.send_message(
            conv_chat,
            "测试方案一般包括哪些内容？",
            [],
        )
        if msg_resp.get("code") != 0:
            all_checks.append(("01. 响应 code=0", False, json.dumps(msg_resp)[:200]))
            print_check(*all_checks[-1])
            return 1
        data = msg_resp.get("data", {})
        all_checks.append(("01. 响应 code=0", True, ""))
        print_check(*all_checks[-1])
        all_checks.append((
            "02. route=chat_reply", data.get("route") == "chat_reply",
            f"route={data.get('route')!r}",
        ))
        print_check(*all_checks[-1])
        all_checks.append((
            "03. intent=general_chat", data.get("intent") == "general_chat",
            f"intent={data.get('intent')!r}",
        ))
        print_check(*all_checks[-1])
        all_checks.append((
            "04. requires_sse=False", data.get("requires_sse") is False,
            f"requires_sse={data.get('requires_sse')!r}",
        ))
        print_check(*all_checks[-1])
        all_checks.append((
            "05. task_id is None", data.get("task_id") is None,
            f"task_id={data.get('task_id')!r}",
        ))
        print_check(*all_checks[-1])
        all_checks.append((
            "06. agent_task is None", data.get("agent_task") is None,
            f"agent_task={data.get('agent_task')!r}",
        ))
        print_check(*all_checks[-1])
        reply = (data.get("agent_reply") or {}).get("content", "") or ""
        all_checks.append((
            "07. agent_reply.content 非空", len(reply) > 5,
            reply[:60],
        ))
        print_check(*all_checks[-1])
        # DB check
        conv_chat_internal = get_conversation_internal_id(conv_chat)
        n = count_messages_for_conversation(conv_chat_internal) if conv_chat_internal else 0
        all_checks.append((
            "08. DB messages 至少 2 条 (user + agent)", n >= 2,
            f"count={n}",
        ))
        print_check(*all_checks[-1])

        # ── 2. Phase 2 — Ask for files (no files) ────────────────
        print_section("Phase 2: Ask for Files (route=ask_for_files)")
        msg_resp = await client.send_message(
            conv_agent,
            "帮我生成测试方案",
            [],
        )
        if msg_resp.get("code") != 0:
            all_checks.append(("09. 响应 code=0", False, json.dumps(msg_resp)[:200]))
            print_check(*all_checks[-1])
            return 1
        data = msg_resp.get("data", {})
        all_checks.append(("09. 响应 code=0", True, ""))
        print_check(*all_checks[-1])
        all_checks.append((
            "10. route=ask_for_files", data.get("route") == "ask_for_files",
            f"route={data.get('route')!r}",
        ))
        print_check(*all_checks[-1])
        all_checks.append((
            "11. intent=test_plan_generation",
            data.get("intent") == "test_plan_generation",
            f"intent={data.get('intent')!r}",
        ))
        print_check(*all_checks[-1])
        all_checks.append((
            "12. requires_sse=False", data.get("requires_sse") is False,
            f"requires_sse={data.get('requires_sse')!r}",
        ))
        print_check(*all_checks[-1])
        all_checks.append((
            "13. task_id is None", data.get("task_id") is None,
            f"task_id={data.get('task_id')!r}",
        ))
        print_check(*all_checks[-1])
        all_checks.append((
            "14. agent_task is None", data.get("agent_task") is None,
            f"agent_task={data.get('agent_task')!r}",
        ))
        print_check(*all_checks[-1])
        reply = (data.get("agent_reply") or {}).get("content", "") or ""
        all_checks.append((
            "15. 回复含 '需求文档' 和 '模板'",
            "需求文档" in reply and "模板" in reply,
            reply[:80],
        ))
        print_check(*all_checks[-1])

        # ── 3. Phase 3 — Agent task (full flow reuse) ───────────
        print_section("Phase 3: File Complete → Agent Task (full flow)")
        # Upload + confirm
        req_upload = await client.upload_file(REQ_DOC_PATH, conv_agent)
        req_file_id = req_upload.get("data", {}).get("id", "")
        if not req_file_id:
            all_checks.append(("16. 需求文件上传", False, json.dumps(req_upload)[:200]))
            print_check(*all_checks[-1])
            return 1
        await client.confirm_file_type(req_file_id, "requirement_doc")

        tpl_upload = await client.upload_file(TPL_DOC_PATH, conv_agent)
        tpl_file_id = tpl_upload.get("data", {}).get("id", "")
        if not tpl_file_id:
            all_checks.append(("17. 模板文件上传", False, json.dumps(tpl_upload)[:200]))
            print_check(*all_checks[-1])
            return 1
        await client.confirm_file_type(tpl_file_id, "test_plan_template")
        all_checks.append(("18. 两类文件齐备", True, f"req={req_file_id} tpl={tpl_file_id}"))
        print_check(*all_checks[-1])

        msg_resp = await client.send_message(
            conv_agent,
            "根据这些文件帮我生成测试方案",
            [req_file_id, tpl_file_id],
        )
        if msg_resp.get("code") != 0:
            all_checks.append(("19. 响应 code=0", False, json.dumps(msg_resp)[:200]))
            print_check(*all_checks[-1])
            return 1
        data = msg_resp.get("data", {})
        all_checks.append(("19. 响应 code=0", True, ""))
        print_check(*all_checks[-1])
        all_checks.append((
            "20. route=agent_task", data.get("route") == "agent_task",
            f"route={data.get('route')!r}",
        ))
        print_check(*all_checks[-1])
        all_checks.append((
            "21. intent=test_plan_generation",
            data.get("intent") == "test_plan_generation",
            f"intent={data.get('intent')!r}",
        ))
        print_check(*all_checks[-1])
        all_checks.append((
            "22. requires_sse=True", data.get("requires_sse") is True,
            f"requires_sse={data.get('requires_sse')!r}",
        ))
        print_check(*all_checks[-1])
        task_id = data.get("task_id") or ""
        all_checks.append((
            "23. task_id truthy", bool(task_id), f"task_id={task_id}",
        ))
        print_check(*all_checks[-1])
        legacy_task = (data.get("agent_task") or {}).get("task_id", "")
        all_checks.append((
            "24. legacy agent_task.task_id == task_id",
            legacy_task == task_id, f"legacy={legacy_task}",
        ))
        print_check(*all_checks[-1])
        all_checks.append((
            "25. agent_reply is None", data.get("agent_reply") is None,
            f"agent_reply={data.get('agent_reply')!r}",
        ))
        print_check(*all_checks[-1])

        if not task_id:
            return 1

        # SSE pre-confirm
        print_section("Phase 3a: SSE Pre-Confirm")
        observed_pre: list[str] = []
        pre_done = False
        async for event in client.sse_events(task_id, timeout=120.0):
            etype = event.get("event_type", "?")
            observed_pre.append(etype)
            print(f"  SSE: [{etype}] {event.get('title', '')}")
            if etype == "stream_phase_done":
                pre_done = True
                break
        all_checks.append((
            "26. SSE pre-confirm 完成", pre_done, f"{len(observed_pre)} events",
        ))
        print_check(*all_checks[-1])

        # Get pending confirmation
        pending_resp = await client.get_pending_confirmation(task_id)
        pending = (pending_resp.get("data") or {}).get("sections", [])
        all_checks.append((
            "27. pending confirmation 可用", len(pending) > 0,
            f"{len(pending)} sections",
        ))
        print_check(*all_checks[-1])

        # Submit confirmation
        if pending:
            confirm_sections = [
                {"section_id": s["section_id"], "action": s.get("suggested_action", "ai_generate")}
                for s in pending
            ]
        else:
            confirm_sections = [{"section_id": "all", "action": "ai_generate"}]
        confirm_resp = await client.confirm_task(task_id, confirm_sections)
        all_checks.append((
            "28. 用户确认提交成功",
            confirm_resp.get("code") == 0,
            f"{len(confirm_sections)} sections",
        ))
        print_check(*all_checks[-1])

        # SSE post-confirm
        print_section("Phase 3b: SSE Post-Confirm (real LLM)")
        if SKIP_PHASE3B:
            print(
                "  E2E_SKIP_PHASE3B=1 — skipping post-confirm SSE. "
                "Full generation is covered by e2e_test_full_agent_flow.py."
            )
            all_checks.append((
                "29. SSE post-confirm 跳过 (skip flag)", True,
                "covered by e2e_test_full_agent_flow.py",
            ))
            print_check(*all_checks[-1])
            all_checks.append((
                "30. artifact_created 事件跳过", True, "skipped",
            ))
            print_check(*all_checks[-1])
            all_checks.append((
                "31. artifact 已入库跳过", True, "skipped",
            ))
            print_check(*all_checks[-1])
            all_checks.append((
                "32. artifact detail 不暴露 storage_path跳过", True, "skipped",
            ))
            print_check(*all_checks[-1])
            all_checks.append((
                "33. artifact detail 不暴露 internal_id 跳过", True, "skipped",
            ))
            print_check(*all_checks[-1])
            all_checks.append((
                "34. artifact 是合法 .docx 跳过", True, "skipped",
            ))
            print_check(*all_checks[-1])
        else:
            observed_post: list[str] = []
            post_done = False
            async for event in client.sse_events_post_confirm(
                task_id, timeout=PHASE3B_TIMEOUT_S
            ):
                etype = event.get("event_type", "?")
                observed_post.append(etype)
                print(f"  SSE: [{etype}] {event.get('title', '')}")
                if etype == "stream_phase_done":
                    post_done = True
                    break
            all_checks.append((
                "29. SSE post-confirm 完成", post_done, f"{len(observed_post)} events",
            ))
            print_check(*all_checks[-1])
            all_checks.append((
                "30. artifact_created 事件出现", "artifact_created" in observed_post,
                f"events={observed_post[:8]}…",
            ))
            print_check(*all_checks[-1])

            # List artifacts
            art_resp = await client.list_task_artifacts(task_id)
            artifacts = (art_resp.get("data") or {}).get("artifacts", [])
            all_checks.append((
                "31. artifact 已入库", len(artifacts) > 0, f"{len(artifacts)} artifacts",
            ))
            print_check(*all_checks[-1])

            # Download + verify .docx
            if artifacts:
                artifact_id = artifacts[0].get("artifact_id", "")
                detail = await client.get_artifact_detail(artifact_id)
                detail_str = json.dumps(detail, ensure_ascii=False)
                all_checks.append((
                    "32. artifact detail 不暴露 storage_path",
                    "storage_path" not in detail_str,
                    "",
                ))
                print_check(*all_checks[-1])
                all_checks.append((
                    "33. artifact detail 不暴露 internal_id",
                    "internal_id" not in detail_str,
                    "",
                ))
                print_check(*all_checks[-1])
                content = await client.download_artifact(artifact_id)
                try:
                    import io
                    Document(io.BytesIO(content))
                    valid = True
                except Exception as e:
                    valid = False
                    print(f"  docx error: {e}")
                all_checks.append((
                    "34. artifact 是合法 .docx", valid, f"{len(content)} bytes",
                ))
                print_check(*all_checks[-1])

        # ── 4. Phase 4 — Unsupported ───────────────────────────
        print_section("Phase 4: Unsupported (route=unsupported)")
        msg_resp = await client.send_message(
            conv_unsupp,
            "帮我生成测试用例",
            [],
        )
        if msg_resp.get("code") != 0:
            all_checks.append(("35. 响应 code=0", False, json.dumps(msg_resp)[:200]))
            print_check(*all_checks[-1])
            return 1
        data = msg_resp.get("data", {})
        all_checks.append(("35. 响应 code=0", True, ""))
        print_check(*all_checks[-1])
        all_checks.append((
            "36. route=unsupported", data.get("route") == "unsupported",
            f"route={data.get('route')!r}",
        ))
        print_check(*all_checks[-1])
        all_checks.append((
            "37. intent=test_case_generation",
            data.get("intent") == "test_case_generation",
            f"intent={data.get('intent')!r}",
        ))
        print_check(*all_checks[-1])
        all_checks.append((
            "38. requires_sse=False", data.get("requires_sse") is False,
            f"requires_sse={data.get('requires_sse')!r}",
        ))
        print_check(*all_checks[-1])
        all_checks.append((
            "39. agent_task is None", data.get("agent_task") is None,
            f"agent_task={data.get('agent_task')!r}",
        ))
        print_check(*all_checks[-1])
        reply = (data.get("agent_reply") or {}).get("content", "") or ""
        all_checks.append((
            "40. 回复含 '测试用例生成功能'",
            "测试用例生成功能" in reply,
            reply[:80],
        ))
        print_check(*all_checks[-1])

    finally:
        await client.close()

    # ── Summary ──────────────────────────────────────────────────
    print_section("Summary")
    passed = sum(1 for _, ok, _ in all_checks if ok)
    total = len(all_checks)
    for label, ok, detail in all_checks:
        print_check(label, ok, detail)
    print()
    if passed == total:
        print(f"VERDICT: PASS  ({passed}/{total})")
        return 0
    else:
        print(f"VERDICT: FAIL  ({passed}/{total})")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
