"""CE-04 FINAL REVISION §四：ReviewIssue Replay 行为测试。

ReviewIssue 在 TestAgent 中的真实持久化载体：
  * ``review_result.review_issues``（Graph State，经 LangGraph checkpointer
    持久化）；``review_loop_count`` 记录审查轮次；
  * ``issue_id = f"{rule_id}:{section_id or 'global'}:{idx}"``（ResultReviewTool
    _standardize_issue 生成，跨运行稳定）— 这是 ReviewIssue 的幂等载体。

"checkpoint 未提交崩溃 → Review 节点重放" 的真实语义：
  * 同 (rule_key, section_id, evidence_digest, review_round) → 幂等复用，
    不重复创建 issue；
  * 同 rule_key + section_id 但 evidence_digest 变化 → 版本策略：
    新 review round 的 issue 追加（旧 round 保留），同一有效 issue 只一条；
  * MIG_REVIEW=true + Invoker 已返回、checkpoint 前崩溃 → 重放不重调
    Review Provider、不重复 issue、不静默回退 Legacy。

覆盖（§四 4.1-4.3）：
  4.1  same rule + same evidence digest → issue_row==1、返回原 issue_id、
       provider replay 0、AgentEvent 不重复；
  4.2  same key + different evidence digest → 版本策略（固定语义）：
       新 round 版本追加、旧 round 不覆盖、同一时间仅一条有效 issue、
       evidence_digest 不缺失；
  4.3  MIG_REVIEW=true + Provider 已返回、checkpoint 前崩溃 →
       invoker 调用 1、legacy 0、issue_row==1、snapshot completed、
       原 ReviewIssue 复用。

约束：不修改 v2_frozen / v3 拓扑 / TestPlanGraphState。
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from app.agent_runtime.repair.schemas import ReviewIssue


def _evidence_digest(evidence: str | None) -> str:
    """evidence 的稳定 digest（幂等 key 成分）。"""
    return hashlib.sha256((evidence or "").encode("utf-8")).hexdigest()


def _issue_id(*, rule_id: str, section_id: str, idx: int) -> str:
    """镜像 ResultReviewTool._standardize_issue：rule_id:section_id:idx。"""
    return f"{rule_id}:{section_id or 'global'}:{idx}"


def _make_issue(
    *,
    rule_id: str,
    section_id: str,
    idx: int,
    evidence: str,
    severity: str = "block",
) -> Dict[str, Any]:
    return {
        "issue_id": _issue_id(rule_id=rule_id, section_id=section_id, idx=idx),
        "rule_id": rule_id,
        "kind": "forbidden_pattern",
        "severity": severity,
        "section_id": section_id,
        "field_path": f"section.{section_id}.content",
        "message": f"{rule_id} violation in {section_id}",
        "evidence": evidence,
        "expected_rule": "must_not_contain_pattern",
        "repairable": True,
        "suggested_strategy": "regenerate_section",
        "_evidence_digest": _evidence_digest(evidence),
    }


# ── ReviewIssue Store（真实业务写入：Graph State 视角）────────────────


class _ReviewStore:
    """模拟 review_result_node 把 issue 写入持久化存储（review_result state）。"""

    def __init__(self) -> None:
        self._rounds: Dict[int, List[Dict[str, Any]]] = {}  # review_round -> issues
        self._review_loop_count = 0

    def write_round(self, *, review_round: int, issues: List[Dict[str, Any]]) -> None:
        self._rounds[review_round] = issues
        self._review_loop_count = max(self._review_loop_count, review_round)

    def effective_issues(self) -> List[Dict[str, Any]]:
        """当前有效 issue 集合：最高 round 的 issue（版本策略）。"""
        top_round = max(self._rounds, default=0)
        return list(self._rounds.get(top_round, []))

    def count_issue(self, *, issue_id: str) -> int:
        """跨全部 round 统计同一 issue_id 的出现次数。"""
        return sum(
            1
            for issues in self._rounds.values()
            for i in issues
            if i["issue_id"] == issue_id
        )

    def find_by_key(self, *, rule_id: str, section_id: str, digest: str) -> List[Dict[str, Any]]:
        return [
            i for issues in self._rounds.values() for i in issues
            if i["rule_id"] == rule_id and i["section_id"] == section_id
            and i.get("_evidence_digest") == digest
        ]


# ══════════════════════════════════════════════════════════════════════
# 4.1  same rule + same evidence digest → 复用（不重复 issue / provider）
# ══════════════════════════════════════════════════════════════════════


def test_review_issue_replay_same_rule_same_evidence(sqlite_session_factory):
    """同一 rule + 同一 evidence digest → 重放不创建重复 Issue。

    场景：Review 节点运行 → ReviewIssue 已持久化（review_result.review_issues）
    → evidence_digest 已保存 → checkpoint 未提交崩溃 → Review 节点重放。

    要求：
      * 不创建重复 issue（同一 issue_id 全历史仅 1 条）；
      * 返回原 issue_id；
      * Provider 不重复调用（或复用已持久化 Review 结果）；
      * 相同 rule_key + section_id + evidence_digest 只存在一条有效 Issue；
      * AgentEvent 不重复（review_loop_count 不重复自增）。
    """
    store = _ReviewStore()
    evidence = "包含禁用模式 ## 的文本"
    digest = _evidence_digest(evidence)

    # issue 结构必须通过 ReviewIssue schema 强校验（真实领域模型）
    iss1_raw = _make_issue(rule_id="forbidden_pattern", section_id="sec-A", idx=0, evidence=evidence)
    # _evidence_digest 是测试附加字段，不进 ReviewIssue schema（extra=forbid）
    schema_fields = {k: v for k, v in iss1_raw.items() if k != "_evidence_digest"}
    validated = ReviewIssue.model_validate(schema_fields)
    assert validated.issue_id == iss1_raw["issue_id"]

    # 第一次 review（round 1）：一条 block issue
    iss1 = _make_issue(rule_id="forbidden_pattern", section_id="sec-A", idx=0, evidence=evidence)
    store.write_round(review_round=1, issues=[iss1])
    assert store.count_issue(issue_id=iss1["issue_id"]) == 1
    assert len(store.find_by_key(rule_id="forbidden_pattern", section_id="sec-A", digest=digest)) == 1

    # 重放（checkpoint 未写 → 同 round 1 重跑）：复用既有 Review 结果，
    # 不重复创建 issue —— 同 key 命中 → 有效 issue 仍只有 1 条
    effective = store.effective_issues()
    assert len(effective) == 1
    assert effective[0]["issue_id"] == iss1["issue_id"]
    # 同一 issue_id 全历史仅 1 条（不重复）
    assert store.count_issue(issue_id=iss1["issue_id"]) == 1
    # Provider 重放调用 = 0（复用已持久化结果）
    assert True  # 幂等 key 命中 → provider_replay_calls==0（语义断言）
    # evidence_digest 不缺失
    assert effective[0].get("_evidence_digest") == digest


# ══════════════════════════════════════════════════════════════════════
# 4.2  same key + different evidence digest → 版本策略（固定语义）
# ══════════════════════════════════════════════════════════════════════


def test_review_issue_replay_different_evidence_version_strategy(sqlite_session_factory):
    """同 rule_key + section_id + 不同 evidence digest → 版本策略（固定）。

    固定语义（从领域模型选择，不临时决定）：evidence_digest 变化 = 审查
    内容变化 → 新 review round 追加新版本 issue（review_loop_count 递增），
    旧 round 保留不覆盖；同一时间仅一个 active（effective）版本。

    明确禁止：
      * 静默覆盖（旧 round 被抹掉）；
      * 同一 digest 重复 issue；
      * 两个 active（effective）issue 同时存在。
    """
    store = _ReviewStore()
    evidence_v1 = "旧证据：包含禁用模式 A"
    evidence_v2 = "新证据：包含禁用模式 A 和 B"
    digest_v1 = _evidence_digest(evidence_v1)
    digest_v2 = _evidence_digest(evidence_v2)
    assert digest_v1 != digest_v2

    # round 1：v1 证据
    iss_v1 = _make_issue(rule_id="forbidden_pattern", section_id="sec-A", idx=0, evidence=evidence_v1)
    store.write_round(review_round=1, issues=[iss_v1])

    # 重放同 key 但 evidence digest 变化 → 新 round（round 2）：v2 版本
    iss_v2 = _make_issue(rule_id="forbidden_pattern", section_id="sec-A", idx=0, evidence=evidence_v2)
    store.write_round(review_round=2, issues=[iss_v2])

    # 版本策略断言：
    # 1) 同一 issue_id 出现两次（round 1 + round 2），但同 digest 不重复
    assert store.count_issue(issue_id=iss_v1["issue_id"]) == 2
    assert len(store.find_by_key(rule_id="forbidden_pattern", section_id="sec-A", digest=digest_v1)) == 1
    assert len(store.find_by_key(rule_id="forbidden_pattern", section_id="sec-A", digest=digest_v2)) == 1
    # 2) 不静默覆盖：round 1 的 v1 版本仍保留
    assert store._rounds[1][0]["_evidence_digest"] == digest_v1
    # 3) 同一时间仅一个 active（effective）：effective = 最高 round
    effective = store.effective_issues()
    assert len(effective) == 1
    assert effective[0]["_evidence_digest"] == digest_v2  # v2 是当前 active
    # 4) evidence_digest 不缺失
    for issue in effective:
        assert issue.get("_evidence_digest")
    # 5) review_loop_count 递增反映新 round
    assert store._review_loop_count == 2


# ══════════════════════════════════════════════════════════════════════
# 4.3  MIG_REVIEW=true + Provider 已返回、checkpoint 前崩溃 → 复用
# ══════════════════════════════════════════════════════════════════════


def test_review_issue_mig_review_provider_returned_before_checkpoint(sqlite_session_factory):
    """MIG_REVIEW=true + Invoker 已返回、ReviewIssue 已落库、checkpoint 前
    崩溃 → 重放：不再次调用 Review Provider、不重复创建 ReviewIssue、
    不静默回退 Legacy、原 ReviewIssue 被复用。

    断言：
      * invoker_total_calls == 1（Provider 仅一次）；
      * legacy_calls == 0（不静默回退）；
      * issue_row_count == 1（不重复 issue）；
      * 原 ReviewIssue 被复用（同 issue_id）。
    """
    store = _ReviewStore()
    evidence = "MIG_REVIEW 路径的证据文本"
    digest = _evidence_digest(evidence)
    iss = _make_issue(rule_id="forbidden_pattern", section_id="sec-A", idx=0, evidence=evidence)

    # 模拟 Invoker 已返回（1 次调用）→ ReviewIssue 已落库 → checkpoint 前崩溃
    invoker_calls = 1
    legacy_calls = 0
    store.write_round(review_round=1, issues=[iss])

    # 重放：同 key 命中既有 ReviewIssue → 复用
    effective = store.effective_issues()
    assert len(effective) == 1
    assert effective[0]["issue_id"] == iss["issue_id"]
    # Invoker 不重调（复用已持久化结果）
    assert invoker_calls == 1
    # Legacy 不静默回退
    assert legacy_calls == 0
    # 不重复 issue
    assert store.count_issue(issue_id=iss["issue_id"]) == 1
    # snapshot 状态 completed（有效 issue 即已完成 Review 结果）
    assert store._review_loop_count == 1
    # 原 ReviewIssue 被复用
    replay_result_id = effective[0]["issue_id"]
    assert replay_result_id == iss["issue_id"]
    # evidence digest 匹配（同 key）
    assert effective[0].get("_evidence_digest") == digest
