"""Authenticated, production-facing Phase 2 API evidence collector.

The runner deliberately keeps credentials in process memory only.  It creates
uniquely marked test records, records safe API evidence, and removes the
workspace records it can safely remove.  The required ``forget`` operation
leaves a tombstone by product design, so it is never hidden by cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from phase2_lct_runtime import LctRun, cli_failure, repository_root


class ApiFailure(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: Any) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} returned HTTP {status}")


class ApiClient:
    def __init__(self, base_url: str, *, timeout_seconds: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        self.token: str | None = None
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = self.base_url + path
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=payload, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                decoded = json.loads(raw) if raw else None
                self.calls.append({"method": method, "path": path, "status": response.status, "request_id": _request_id(decoded)})
                return decoded
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                decoded = {"non_json_body": raw[:500]}
            self.calls.append({"method": method, "path": path, "status": exc.code, "request_id": _request_id(decoded)})
            raise ApiFailure(method, path, exc.code, decoded) from exc

    def login(self, username: str, password: str) -> dict[str, Any]:
        response = self.request("POST", "/auth/login", {"username": username, "password": password})
        data = _data(response)
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise RuntimeError("login response contained no token")
        self.token = str(token)
        # Do not retain or serialize the raw login response/token.
        return {"authenticated": True, "request_id": _request_id(response)}


def _data(response: Any) -> Any:
    if isinstance(response, dict) and "code" in response:
        if response.get("code") != 0:
            raise RuntimeError(f"API returned application error: {response.get('message')}")
        return response.get("data")
    return response


def _request_id(response: Any) -> str | None:
    return response.get("request_id") if isinstance(response, dict) else None


def _now_marker() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _memory_ids(rows: Any) -> set[str]:
    if not isinstance(rows, list):
        return set()
    return {str(row.get("memory_public_id")) for row in rows if isinstance(row, dict)}


def _create_memory(client: ApiClient, *, scope_type: str, workspace_key: str | None, marker: str) -> str:
    body = {"scope_type": scope_type, "workspace_key": workspace_key, "memory_type": "fact", "content": marker, "title": marker}
    data = _data(client.request("POST", "/context/memory", body))
    memory_id = data.get("memory_public_id") if isinstance(data, dict) else None
    if not memory_id:
        raise RuntimeError("create-memory response contained no memory_public_id")
    return str(memory_id)


def _activate(client: ApiClient, memory_id: str) -> dict[str, Any]:
    data = _data(client.request("POST", f"/context/memory/{urllib.parse.quote(memory_id)}/activate"))
    if not isinstance(data, dict) or data.get("status") != "active":
        raise RuntimeError(f"activate did not return active: {data}")
    return data


def _create_conversation(client: ApiClient, title: str) -> str:
    data = _data(client.request("POST", "/conversations", {"title": title}))
    conversation_id = data.get("id") if isinstance(data, dict) else None
    if not conversation_id:
        raise RuntimeError("create-conversation response contained no id")
    return str(conversation_id)


def _run_live_api(args: argparse.Namespace, run: LctRun) -> str:
    username = os.environ.get("PHASE2_E2E_USERNAME", "")
    password = os.environ.get("PHASE2_E2E_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("PHASE2_E2E_USERNAME and PHASE2_E2E_PASSWORD must be set by the PowerShell wrapper")

    client = ApiClient(args.base_url)
    created_workspace_memories: list[str] = []
    created_conversations: list[str] = []
    marker = _now_marker()
    evidence: dict[str, Any] = {"marker": marker, "base_url": args.base_url, "api_calls": []}
    try:
        run.log("LIVE-E2E: authenticate using in-memory credentials")
        auth_step = run.step("authenticated live API session", component="Auth API", expected="login and owner identity succeed")
        try:
            auth = client.login(username, password)
        except Exception as exc:
            # Deliberately do not include the account, password, cookie, token,
            # or raw login body in the evidence bundle.
            run.check(
                auth_step,
                False,
                actual=f"login rejected by {args.base_url}/auth/login: {type(exc).__name__}: {exc}",
                evidence={"api_calls": list(client.calls)},
            )
            raise  # pragma: no cover - check always raises
        me = _data(client.request("GET", "/auth/me"))
        run.check(auth_step, bool(auth["authenticated"]) and isinstance(me, dict), actual=f"authenticated={auth['authenticated']}, identity_returned={isinstance(me, dict)}")
        evidence["auth"] = {"authenticated": True, "request_id": auth["request_id"], "user_id": me.get("user_id") if isinstance(me, dict) else None}

        # LCT-03: actual HTTP lifecycle.  The public contract deliberately
        # hides forgotten content/evidence, so that check stays classified as
        # an observability limitation rather than inferred success.
        lct03_marker = f"PHASE2_E2E_LCT03_{marker}"
        lct03_memory = _create_memory(client, scope_type="user", workspace_key=None, marker=lct03_marker)
        _activate(client, lct03_memory)
        active_before = _data(client.request("GET", "/context/memory?scope_type=user&status=active"))
        forget_result = _data(client.request("POST", f"/context/memory/{urllib.parse.quote(lct03_memory)}/forget"))
        active_after = _data(client.request("GET", "/context/memory?scope_type=user&status=active"))
        evidence["lct03"] = {"memory_public_id": lct03_memory, "active_before_contains": lct03_memory in _memory_ids(active_before), "forget_status": forget_result.get("status") if isinstance(forget_result, dict) else None, "active_after_contains": lct03_memory in _memory_ids(active_after)}
        step = run.step("LCT-03 real API lifecycle", component="Context Memory API", expected="candidate create, activate, then forget removes it from active listing")
        run.check(step, evidence["lct03"]["active_before_contains"] and evidence["lct03"]["forget_status"] == "forgotten" and not evidence["lct03"]["active_after_contains"], actual=json.dumps(evidence["lct03"], ensure_ascii=False))

        # LCT-04: use server-generated conversation public IDs to satisfy the
        # workspace-key format validator.  We verify real owner-scoped writes
        # and visible separation; composition itself is intentionally not
        # inferred from this management endpoint.
        conversation_a = _create_conversation(client, f"Phase2 E2E workspace A {marker}")
        conversation_b = _create_conversation(client, f"Phase2 E2E workspace B {marker}")
        created_conversations.extend([conversation_a, conversation_b])
        mem_a = _create_memory(client, scope_type="workspace", workspace_key=f"conversation:{conversation_a}", marker=f"PHASE2_E2E_LCT04_A_{marker}")
        mem_b = _create_memory(client, scope_type="workspace", workspace_key=f"conversation:{conversation_b}", marker=f"PHASE2_E2E_LCT04_B_{marker}")
        created_workspace_memories.extend([mem_a, mem_b])
        _activate(client, mem_a)
        _activate(client, mem_b)
        workspace_rows = _data(client.request("GET", "/context/memory?scope_type=workspace&status=active"))
        by_id = {str(row.get("memory_public_id")): row for row in workspace_rows if isinstance(row, dict)} if isinstance(workspace_rows, list) else {}
        evidence["lct04"] = {"conversation_a": conversation_a, "conversation_b": conversation_b, "memory_a": mem_a, "memory_b": mem_b, "workspace_a": by_id.get(mem_a, {}).get("workspace_key"), "workspace_b": by_id.get(mem_b, {}).get("workspace_key")}
        step = run.step("LCT-04 real API workspace records", component="Conversation + Context Memory API", expected="two distinct owner-scoped workspace memory records")
        run.check(step, evidence["lct04"]["workspace_a"] == f"conversation:{conversation_a}" and evidence["lct04"]["workspace_b"] == f"conversation:{conversation_b}" and conversation_a != conversation_b, actual=json.dumps(evidence["lct04"], ensure_ascii=False))

        compaction_conversation_id = args.conversation_id
        if args.seed_live_conversation:
            if not args.enable_live_compaction:
                raise RuntimeError("--seed-live-conversation requires --enable-live-compaction")
            compaction_conversation_id = _create_conversation(client, f"Phase2 E2E compaction {marker}")
            created_conversations.append(compaction_conversation_id)
            seed_messages = [
                f"PHASE2_E2E_OLD_FACT_{marker}: initial requirement is A.",
                f"PHASE2_E2E_OLD_FACT_{marker}: discussion still references A.",
                f"PHASE2_E2E_NEW_FACT_{marker}: requirement is now B; A is superseded.",
                f"PHASE2_E2E_CURRENT_GOAL_{marker}: retain B and verify compaction evidence.",
            ]
            seed_responses = []
            for content in seed_messages:
                response = _data(client.request("POST", f"/conversations/{urllib.parse.quote(compaction_conversation_id)}/messages", {"content": content, "attached_file_ids": [], "knowledge_mode_snapshot": "AUTO"}))
                seed_responses.append({"response_type": type(response).__name__})
            evidence["seeded_compaction_conversation"] = {"conversation_id": compaction_conversation_id, "message_count": len(seed_messages), "responses": seed_responses}

        snapshots = _data(client.request("GET", "/context/audit/snapshots?limit=5"))
        retrieval = _data(client.request("GET", "/context/audit/retrieval?limit=5"))
        compaction = _data(client.request("GET", "/context/audit/compaction?limit=5"))
        evidence["audit_probe"] = {"snapshot_items": len(snapshots.get("items", [])) if isinstance(snapshots, dict) else None, "retrieval_items": len(retrieval.get("items", [])) if isinstance(retrieval, dict) else None, "compaction_items": len(compaction.get("items", [])) if isinstance(compaction, dict) else None}
        step = run.step("owner-scoped audit endpoints reachable", component="Context Audit API", expected="snapshot, retrieval and compaction audit lists return safely")
        run.check(step, all(isinstance(value, dict) for value in (snapshots, retrieval, compaction)), actual=json.dumps(evidence["audit_probe"], ensure_ascii=False))

        if args.enable_live_compaction:
            if not compaction_conversation_id:
                raise RuntimeError("--conversation-id or --seed-live-conversation is required with --enable-live-compaction")
            before = _data(client.request("GET", "/context/audit/compaction?limit=20"))
            compact_result = _data(client.request("POST", f"/conversations/{urllib.parse.quote(compaction_conversation_id)}/context/compact"))
            after = _data(client.request("GET", "/context/audit/compaction?limit=20"))
            run_id = compact_result.get("run_public_id") if isinstance(compact_result, dict) else None
            before_items = before.get("items", []) if isinstance(before, dict) else []
            after_items = after.get("items", []) if isinstance(after, dict) else []
            before_ids = {
                str(item.get("public_id"))
                for item in before_items
                if isinstance(item, dict) and item.get("public_id")
            }
            new_audit_items = [
                item
                for item in after_items
                if isinstance(item, dict) and str(item.get("public_id")) not in before_ids
            ]
            observed_run_id = run_id or (
                new_audit_items[0].get("public_id") if len(new_audit_items) == 1 else None
            )
            detail = (
                _data(client.request("GET", f"/context/audit/compaction/{urllib.parse.quote(str(observed_run_id))}"))
                if observed_run_id
                else None
            )
            evidence["lct01_lct02_live_compaction"] = {
                "conversation_id": compaction_conversation_id,
                "before_count": len(before_items),
                "result": compact_result,
                "after_count": len(after_items),
                "new_audit_items": new_audit_items,
                "observed_run_id": observed_run_id,
                "audit_detail": detail,
            }
            compaction_evidence_path = run.write_evidence(
                "live-compaction-evidence.json",
                evidence["lct01_lct02_live_compaction"],
            )
            step = run.step("LCT-01/LCT-02 opt-in live compaction", component="Conversation Compaction API + Audit", expected="manual compaction returns a persisted run and audit detail")
            run.check(
                step,
                bool(run_id) and isinstance(detail, dict),
                actual=json.dumps(
                    {
                        "run_public_id": run_id,
                        "observed_run_id": observed_run_id,
                        "new_audit_runs": len(new_audit_items),
                        "detail_returned": isinstance(detail, dict),
                    },
                    ensure_ascii=False,
                ),
                evidence={"live_compaction_evidence": compaction_evidence_path},
            )
        else:
            run.notes.append("OBSERVABILITY_GAP: live LCT-01/LCT-02 compaction was not invoked. Pass -EnableLiveCompaction and a dedicated populated -ConversationId to collect its API/audit evidence.")

        evidence["api_calls"] = client.calls
        path = run.write_evidence("live-api-evidence.json", evidence)
        for step in run.steps:
            step.evidence.setdefault("live_api_evidence", path)
        run.notes.append("LCT-03 public API proves lifecycle/listing behavior, but public endpoints do not expose forgotten content/evidence for a direct sanitization assertion.")
        run.notes.append("LCT-04 public API proves distinct owned workspace records; actual prompt composition isolation requires a provider-backed chat/context snapshot run.")
        return "LCT_PARTIAL_PASS"
    finally:
        cleanup: list[dict[str, Any]] = []
        if not args.keep_workspace_artifacts:
            for memory_id in created_workspace_memories:
                try:
                    _data(client.request("DELETE", f"/context/memory/{urllib.parse.quote(memory_id)}"))
                    cleanup.append({"kind": "workspace_memory", "id": memory_id, "deleted": True})
                except Exception as exc:  # cleanup evidence must not mask the test result
                    cleanup.append({"kind": "workspace_memory", "id": memory_id, "deleted": False, "error": type(exc).__name__})
            for conversation_id in created_conversations:
                try:
                    _data(client.request("DELETE", f"/conversations/{urllib.parse.quote(conversation_id)}"))
                    cleanup.append({"kind": "conversation", "id": conversation_id, "deleted": True})
                except Exception as exc:
                    cleanup.append({"kind": "conversation", "id": conversation_id, "deleted": False, "error": type(exc).__name__})
        if cleanup:
            run.write_evidence("cleanup.json", cleanup)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect authenticated Phase 2 live API evidence")
    parser.add_argument("--base-url", default="http://localhost:8000/api")
    parser.add_argument("--enable-live-compaction", action="store_true")
    parser.add_argument("--conversation-id", default="")
    parser.add_argument("--seed-live-conversation", action="store_true")
    parser.add_argument("--keep-workspace-artifacts", action="store_true")
    args = parser.parse_args()
    run = LctRun(lct="LIVE-E2E", repo_root=repository_root(), execution_path="LIVE_API + PARTIAL_OBSERVABILITY")
    try:
        verdict = _run_live_api(args, run)
        return run.finish(lct_verdict=verdict, classification="authenticated live API evidence collection")
    except Exception as exc:
        return cli_failure(run, exc, classification="authenticated live API evidence collection")


if __name__ == "__main__":
    raise SystemExit(main())
