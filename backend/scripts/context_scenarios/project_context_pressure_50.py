"""Real project scenario for Context Usage pressure and five-category coverage.

CTX-PROJECT-50 creates a genuine project workspace, uploads a varied document
corpus, installs project instructions and active user memories, then conducts
a long but meaningful release-readiness discussion through the normal chat
API.  It does not write application tables directly.

The scenario distinguishes two measurements which must not be conflated:

* ``raw_pressure_percent`` is retained as diagnostic preflight evidence;
* ``visible_percent`` is the effective working set shown by the UI card after
  a real send;
* ``raw_pressure_percent`` reaching 60% is the retention-transition trigger.

This pre-compaction observation scenario succeeds when a real request reaches
the configured target strictly below the 60% retention waterline, leaves a
safety margin for the assistant reply, and activates all five UI categories.
It must not trigger a conversation compaction run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = SCRIPTS_ROOT.parent
for path in (SCRIPTS_ROOT, BACKEND_ROOT, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from phase2_lct_runtime import LctRun, cli_failure, repository_root
from phase2_live_api_e2e import ApiClient, _data
from project_refund_long_memory import (
    MultipartApiClient,
    _delete_records,
    _require_id,
)


SCENARIO = "CTX-PROJECT-50"
PROJECT_TITLE_PREFIX = "CTX-PROJECT-50 跨境履约平台发布演练 "
CONVERSATION_TITLE_PREFIX = "CTX-PROJECT-50 发布指挥室 "
FILE_NAME_PREFIX = "CTX_PROJECT_50_"
STATE_NAME = "CTX-PROJECT-50-latest-retained.json"
LEGACY_STATE_NAME = "CTX-PROJECT-75-latest-retained.json"
LEGACY_SCENARIO = "CTX-PROJECT-75"
LEGACY_PROJECT_TITLE_PREFIX = "CTX-PROJECT-75 跨境履约平台发布演练 "
LEGACY_CONVERSATION_TITLE_PREFIX = "CTX-PROJECT-75 发布指挥室 "
UI_CATEGORIES = (
    "conversation_history",
    "project_documents",
    "task_context",
    "user_memory",
    "system_instructions",
)
CONVERSATION_COMPACTION_WATERLINE_PERCENT = 60.0
# The generated assistant reply is persisted after its request preview.  Keep
# two percentage points free so that its normal short answer cannot turn a 45%
# observation run into an unintended 60% compaction test.
PRE_COMPACTION_SAFETY_MARGIN_PERCENT = 2.0


DOMAIN_TOPICS: tuple[tuple[str, str, str], ...] = (
    ("订单状态机", "OMS", "创建、支付、审核、分仓、出库、签收与关闭的状态迁移"),
    ("库存预占", "INV", "预占、确认、释放、超卖保护与仓库切换"),
    ("支付对账", "PAY", "支付成功、异步回调、退款、拒付与日终对账"),
    ("风控审核", "RISK", "规则命中、人工复核、证据留存与超时降级"),
    ("仓内履约", "WMS", "波次、拣货、复核、打包、称重与出库"),
    ("承运商接入", "TMS", "面单、揽收、轨迹、异常件与签收回传"),
    ("关务申报", "CUS", "HS 编码、申报价值、税费、退单与改单"),
    ("售后逆向", "RMA", "退货授权、入库质检、退款与二次销售"),
    ("客户通知", "NTF", "站内信、邮件、短信、多语言与退订"),
    ("主数据治理", "MDM", "商品、仓库、承运商、币种与时区口径"),
    ("权限审计", "IAM", "最小权限、审批、临时授权与审计追踪"),
    ("隐私合规", "PII", "脱敏、加密、留存、删除与跨境传输"),
    ("可观测性", "OBS", "日志、指标、链路、告警、值班与故障归因"),
    ("容量性能", "PERF", "峰值流量、队列背压、限流、降级与恢复"),
    ("数据迁移", "MIG", "全量、增量、校验、双写、切换与回滚"),
    ("灾备演练", "DR", "故障域、RTO、RPO、切流与数据追平"),
    ("灰度发布", "REL", "租户分组、特性开关、观察窗与回滚门槛"),
    ("UAT 验收", "UAT", "范围、证据、缺陷分级、豁免与签字"),
    ("运营交接", "OPS", "监控看板、工单、SOP、培训与升级路径"),
    ("发布复盘", "PIR", "指标对比、事件时间线、行动项与责任闭环"),
)


DOCUMENT_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("业务需求基线", "requirement", ("订单生命周期", "跨境履约范围", "验收原则")),
    ("领域状态机规范", "technical_spec", ("状态与事件", "非法迁移", "补偿动作")),
    ("开放接口契约", "api_spec", ("幂等键", "错误码", "回调签名")),
    ("数据字典与口径", "design", ("核心实体", "时间与币种", "数据质量")),
    ("安全与隐私方案", "design", ("身份权限", "敏感数据", "审计留存")),
    ("容量与性能预算", "technical_spec", ("流量模型", "SLO", "降级策略")),
    ("迁移切换方案", "design", ("迁移批次", "双写校验", "回滚条件")),
    ("UAT 验收计划", "historical_test", ("测试范围", "证据要求", "退出标准")),
    ("发布运行手册", "other", ("灰度步骤", "监控告警", "应急联系人")),
    ("风险与决策台账", "other", ("已决事项", "开放风险", "责任与期限")),
)


STAKEHOLDER_NOTES: tuple[tuple[str, str], ...] = (
    ("产品负责人", "关注承诺范围是否与需求基线一致，以及例外是否经过正式批准"),
    ("测试负责人", "关注可复现步骤、判定口径、数据准备和证据是否足以支持签字"),
    ("研发负责人", "关注状态收敛、幂等、并发冲突以及补偿动作是否可以安全重放"),
    ("SRE 值班长", "关注告警能否在用户投诉前触发，回滚是否有明确观察窗和停止条件"),
    ("数据平台主管", "关注跨库对账、延迟数据、历史回灌和指标口径是否保持一致"),
    ("安全与合规负责人", "关注最小权限、敏感字段处理、审计链和跨境数据边界"),
    ("区域运营负责人", "关注时区、节假日、承运商差异和一线人工兜底是否可执行"),
    ("发布经理", "关注变更单、依赖顺序、责任人、沟通节奏和最终放行证据"),
)

REVIEW_LENSES: tuple[tuple[str, str, str], ...] = (
    ("入口契约", "请求字段、默认值和版本兼容是否明确", "契约快照、样例载荷与校验日志"),
    ("状态推进", "正常路径与拒绝路径能否形成可证明的有限状态迁移", "事件时间线与状态审计记录"),
    ("幂等与重放", "重复请求、乱序回调和消息重放是否只产生一次业务结果", "幂等键、事件序号与去重命中记录"),
    ("并发竞争", "同一订单被多端同时修改时是否存在覆盖或双重履约", "锁等待、版本号与冲突重试轨迹"),
    ("超时边界", "同步超时与后台继续执行之间是否会形成未知状态", "超时点、后台任务状态与最终通知"),
    ("补偿恢复", "部分成功后能否按依赖逆序恢复且不会扩大影响面", "补偿命令、执行人和恢复后核对结果"),
    ("数据一致性", "业务库、搜索索引、消息总线和报表之间何时达到一致", "对账批次、差异清单与收敛时刻"),
    ("权限隔离", "租户、区域和角色边界是否覆盖读写与导出链路", "授权决策、拒绝日志与临时授权单"),
    ("隐私处理", "日志、导出、通知和排障材料是否泄露敏感字段", "脱敏样本、密钥版本与访问审计"),
    ("容量峰值", "活动峰值与补偿流量叠加时是否触发背压或级联超时", "压测曲线、队列深度与限流命中"),
    ("降级策略", "外部依赖不可用时核心交易是否保持可解释的最小能力", "开关记录、降级响应与恢复顺序"),
    ("监控告警", "业务失败、技术失败和数据延迟能否被不同信号准确区分", "指标查询、告警路由与值班确认"),
    ("灰度观察", "灰度租户是否具有代表性且扩大范围有明确准入条件", "租户清单、观察窗报表与放量记录"),
    ("回滚决策", "回滚触发、审批、执行和恢复验证是否可以在时限内闭环", "CHG-260918、操作录屏与双人复核"),
    ("审计取证", "关键决定是否能关联到操作者、原因、输入和结果", "traceId、审批记录与不可变快照"),
    ("客户沟通", "异常通知是否准确说明影响、临时措施和下一更新时间", "通知模板、发送回执与客服口径"),
    ("区域差异", "币种、税制、语言、时区和承运商规则是否独立验证", "区域配置、样本订单与本地签字"),
    ("运行交接", "一线团队是否能根据 SOP 独立识别、升级和处置问题", "演练记录、工单样例与联系人表"),
    ("验收退出", "P0/P1 缺陷、豁免与遗留风险是否满足统一退出标准", "缺陷台账、豁免审批与验收签字"),
    ("复盘闭环", "行动项是否有责任人、期限和可验证的完成定义", "复盘纪要、跟踪看板与关闭证据"),
)

REVIEW_REGIONS: tuple[str, ...] = ("华东", "华南", "欧洲", "北美", "东南亚")
REVIEW_WAVES: tuple[str, ...] = ("基线联调", "峰值压测", "故障恢复演练")


class ScenarioTurn:
    """A short, chronological QA work item; it is never padded for tokens."""

    def __init__(
        self,
        phase: str,
        prompt: str,
        requires_project_retrieval: bool = False,
        required_any_groups: tuple[tuple[str, ...], ...] = (),
        forbidden_current_terms: tuple[str, ...] = (),
    ) -> None:
        self.phase = phase
        self.prompt = prompt
        self.requires_project_retrieval = requires_project_retrieval
        self.required_any_groups = required_any_groups
        self.forbidden_current_terms = forbidden_current_terms


# These are intentionally explicit, short business conversations rather than a
# parameterised block repeated until a requested character count is reached.
# The last ten turns exercise requirement evolution and delayed recall.
REALISTIC_TURNS: tuple[ScenarioTurn, ...] = (
    ScenarioTurn("A-understand", "我是本次跨境履约发布的测试负责人。请基于项目资料先确认本轮测试范围、关键业务链路和当前不能放行的风险；不要创建任务或文件。", True),
    ScenarioTurn("A-understand", "请从项目资料中核对订单状态机：哪些状态允许进入分仓和出库，哪些非法迁移必须被测试拦截？给出测试关注点。", True),
    ScenarioTurn("A-understand", "请依据接口契约说明支付回调的幂等键、重复回调和乱序回调分别应如何验证，并标出需要保留的审计证据。", True),
    ScenarioTurn("A-understand", "请结合上线窗口和 CHG-260918，列出发布前必须确认的两个门禁和一个待澄清事项；事实与推断要分开。"),
    ScenarioTurn("A-understand", "请评估库存预占、确认、释放与超卖保护之间的主要测试风险，特别说明何处需要验证补偿动作。"),
    ScenarioTurn("B-design", "现在开始设计正常路径：请为订单创建到签收的主链路给出覆盖维度、关键数据准备和可观察证据。"),
    ScenarioTurn("B-design", "请补充异常路径：支付成功但库存确认失败、库存释放晚到、承运商回调重复时，应验证哪些业务结果和事件顺序？"),
    ScenarioTurn("B-design", "请列出跨区域、跨时区和多币种下最容易遗漏的三个边界条件，并说明各自的判定口径。"),
    ScenarioTurn("B-design", "请设计同一订单被并发取消和发货时的测试思路，重点说明幂等、版本冲突和最终状态。"),
    ScenarioTurn("B-design", "请给出峰值期间队列积压、限流和降级的验收证据；不要把性能建议写成已经批准的发布决定。"),
    ScenarioTurn("B-design", "请基于项目资料确认日志、traceId、指标和告警应该如何串联，才能支持一次支付对账差异的定位。", True),
    ScenarioTurn("B-design", "请为 UAT 签字准备一份简短的证据清单：范围、缺陷分级、豁免、责任人与退出条件。"),
    ScenarioTurn("C-evolution", "需求变更 CR-001：绝对会话时长暂定为 8 小时。请说明它会影响哪些登录、续期、风控和回归测试，不要修改任何任务。"),
    ScenarioTurn("C-evolution", "针对 CR-001 的 8 小时绝对会话时长，请补充边界、并发登录和过期前后请求的测试数据设计。"),
    ScenarioTurn("C-evolution", "请回到最初的 P0 放行原则，结合目前支付对账和库存风险，说明 P0 未关闭时发布团队应做什么。"),
    ScenarioTurn("C-evolution", "请复核项目 API 资料中与幂等键、错误码和回调签名有关的测试点，指出哪些仍需要来源证据。", True),
    ScenarioTurn("C-evolution", "需求变更 CR-002 已批准：绝对会话时长改为 6 小时，并明确替代 CR-001 的 8 小时规则。请给出受影响回归范围和旧规则处置。"),
    ScenarioTurn("C-evolution", "请把 CR-001 与 CR-002 的差异整理为可执行的测试变更：当前规则、废止规则、保留的兼容性检查和所需证据。"),
    ScenarioTurn("C-evolution", "请重新从项目资料确认灰度发布的观察窗口、停止条件和回滚门槛，并说明它们如何影响 UAT 放行。", True),
    ScenarioTurn("C-evolution", "请评估数据迁移双写校验失败时的发布决策：哪些订单可继续、哪些必须冻结，以及验证恢复的证据。"),
    ScenarioTurn("D-recall", "请回忆本会话早期确认的 P0 放行原则：若 P0 未关闭，当前是否允许全量发布？请给出结论和依据。", required_any_groups=(("P0",), ("不允许", "不得", "禁止", "不能"), ("全量发布", "全量"))),
    ScenarioTurn("D-recall", "请回忆支付回调测试的关键约束，并结合项目资料说明重复或乱序回调时最重要的审计证据是什么。", True),
    ScenarioTurn("D-recall", "请核对库存预占场景中先前讨论的补偿边界；给出一个不应遗漏的异常恢复用例。"),
    ScenarioTurn("D-recall", "请说明 CR-002 之后绝对会话时长的当前值，并明确 8 小时规则的状态；不要把已替代规则当作当前规则。", required_any_groups=(("6小时", "6 h", "6h"), ("替代", "废止", "不再", "旧规则")), forbidden_current_terms=("当前规则是8小时", "当前绝对会话时长为8小时", "当前会话时长是8小时")),
    ScenarioTurn("D-recall", "请再次检索项目资料，确认 UAT 退出条件中 P0、豁免和签字三者的关系，并给出最终回归范围建议。", True),
    ScenarioTurn("D-recall", "请从安全与隐私资料中核对最小权限、敏感字段处理和跨境数据边界的发布前验证重点。", True),
    ScenarioTurn("D-recall", "请为发布经理生成简短风险台账：仍开放的风险、负责人、截止时间和触发回滚的信号。"),
    ScenarioTurn("D-recall", "请确认运行交接所需的监控面板、SOP、升级路径和一线人工兜底证据。"),
    ScenarioTurn("D-recall", "请做一次最终一致性复核：当前会话时长规则、P0 放行原则、支付对账门槛和项目资料中的退出条件是否冲突。", True),
    ScenarioTurn("D-recall", "作为发布前最后一次评审，请给出是否具备进入观察窗口的结论、未决项和下一步；不要创建任务或文件。"),
)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _state_path() -> Path:
    return repository_root() / "test-results" / "phase2-resource-pack" / STATE_NAME


def _state_paths() -> tuple[Path, ...]:
    root = repository_root() / "test-results" / "phase2-resource-pack"
    return (root / STATE_NAME, root / LEGACY_STATE_NAME)


def _write_state(payload: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_state() -> dict[str, Any] | None:
    for path in _state_paths():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("scenario") in {SCENARIO, LEGACY_SCENARIO}:
            return value
    return None


def _make_fixture_docs(directory: Path, marker: str) -> list[dict[str, Any]]:
    """Create ten real DOCX files with a coherent, non-repeated project corpus."""
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - runtime dependency contract
        raise RuntimeError(f"python-docx is required for {SCENARIO}") from exc

    results: list[dict[str, Any]] = []
    for document_index, (title, role, headings) in enumerate(DOCUMENT_SPECS, start=1):
        document = Document()
        document.add_heading(f"跨境履约平台 2.0｜{title}", level=0)
        document.add_paragraph(
            f"基线标识 CP50-{marker}-{document_index:02d}。目标上线窗口为 2026-11-18 02:00-05:00 UTC，"
            "本文档用于真实的发布准备、风险评审和验收取证。"
        )
        for section_index, heading in enumerate(headings, start=1):
            document.add_heading(f"{section_index}. {heading}", level=1)
            for rule_index in range(1, 9):
                code = f"{role.upper()}-{document_index:02d}-{section_index:02d}-{rule_index:02d}"
                document.add_paragraph(
                    f"{code}：{heading}采用可审计的前置条件、执行动作、预期结果和回滚信号。"
                    f"第 {rule_index} 条要求以订单号、租户、仓库、币种、事件时间和关联追踪号作为证据；"
                    f"若校验失败，责任组必须在 {10 + rule_index * 5} 分钟内确认影响面，并在变更单 CHG-260918 中记录处置。",
                    style="List Bullet",
                )
        filename = f"{FILE_NAME_PREFIX}{marker}_{document_index:02d}_{title}.docx"
        path = directory / filename
        document.save(path)
        results.append({
            "label": title,
            "filename": filename,
            "role": role,
            "path": path,
            "sha256_16": hashlib.sha256(path.read_bytes()).hexdigest()[:16],
        })
    return results


def _legacy_filler_work_packet(*, ordinal: int, target_chars: int) -> str:
    """Build one coherent release-review dossier without synthetic row spam."""
    title, code, scope = DOMAIN_TOPICS[(ordinal - 1) % len(DOMAIN_TOPICS)]
    lines = [
        f"第 {ordinal} 轮发布评审主题：{title}（{code}）。",
        f"本轮范围是{scope}。下面是会前访谈、联调观察与演练纪要的合并稿，不是批量造数。",
        "请先识别相互冲突的事实，再给出两个发布门禁、一个待确认项和对应证据；请不要创建任务或文件，回答控制在 180 字内。",
        f"本轮沿用发布窗口 2026-11-18 02:00—05:00 UTC 与变更单 CHG-260918；首条关联链路为 trace-{code.lower()}-{ordinal:02d}-brief。",
    ]
    note_number = 1
    for wave in REVIEW_WAVES:
        for region in REVIEW_REGIONS:
            for stakeholder, stakeholder_focus in STAKEHOLDER_NOTES:
                lens, question, evidence = REVIEW_LENSES[
                    (note_number + ordinal * 3) % len(REVIEW_LENSES)
                ]
                severity = ("P0", "P1", "P2")[(note_number + ordinal) % 3]
                sample_minutes = 10 + ((note_number * 7 + ordinal) % 45)
                lines.extend(
                    [
                    f"\n### {wave} · {region}评审纪要：{stakeholder} / {lens}",
                    (
                        f"{stakeholder}说明，{region}区域在{wave}期间的{title}链路上主要{stakeholder_focus}。现场围绕“{question}”复核了"
                        f"租户隔离、异步回调与人工兜底，发现现行文档对{scope}的描述可以支持主流程，"
                        f"但异常恢复的责任交接仍需结合当地运营时段确认。"
                    ),
                    (
                        f"已确认事实：{region}{wave}样本由业务订单、审计快照和消息轨迹三方互证，证据采用{evidence}；"
                        f"关联 trace-{code.lower()}-{ordinal:02d}-{note_number:03d}。当连续三个观察窗稳定且差异率低于"
                        f"0.{(note_number % 7) + 1}% 时才允许继续放量，任何 {severity} 未关闭都必须在变更单中显式记录。"
                    ),
                    (
                        f"尚待判断：如果{region}外部依赖在{wave}开始后第 {sample_minutes} 分钟退化，是立即回滚、冻结新增流量，"
                        f"还是保留已进入流程的订单并启用补偿。请把这一分歧与项目资料中的退出标准对照，"
                        f"不要把建议写成已经批准的决定。"
                    ),
                    ]
                )
                note_number += 1
                rendered = "\n".join(lines)
                if len(rendered) >= target_chars:
                    cut = rendered.rfind("。", max(0, target_chars - 120), target_chars + 1)
                    return rendered[: cut + 1 if cut >= target_chars - 120 else target_chars]
    raise ValueError(f"target_chars={target_chars} exceeds meaningful dossier capacity")


def _work_packet(*, ordinal: int, marker: str, max_prompt_chars: int) -> tuple[ScenarioTurn, str]:
    """Return a distinct release-review brief plus a coherent meeting dossier."""
    scenario = REALISTIC_TURNS[ordinal - 1]
    prompt = (
        f"[{SCENARIO}:{marker}:T{ordinal:02d}]\n"
        f"{scenario.prompt}\n"
        "请使用项目资料和已有对话作为依据；回答控制在 180 字以内。"
    )
    if len(prompt) >= max_prompt_chars:
        raise ValueError(
            f"realistic turn {ordinal} exceeds max_prompt_chars={max_prompt_chars}; "
            "increase the structured dossier limit"
        )
    dossier_header = "\n\n--- 发布评审会议材料（本轮专属）---\n"
    dossier = _legacy_filler_work_packet(
        ordinal=ordinal,
        target_chars=max_prompt_chars - len(prompt) - len(dossier_header),
    )
    prompt = f"{prompt}{dossier_header}{dossier}"
    return scenario, prompt


def _pre_compaction_work_packet(
    client: ApiClient,
    *,
    conversation_id: str,
    ordinal: int,
    marker: str,
    max_prompt_chars: int,
    target_percent: float,
) -> tuple[ScenarioTurn, str, dict[str, Any]]:
    """Choose the largest meaningful dossier that reaches the target safely.

    Preview is read-only, so binary-searching the final dossier size lets the
    scenario approach 45% without posting a request whose retained history can
    cross the 60% automatic-compaction policy.
    """
    safety_ceiling = (
        CONVERSATION_COMPACTION_WATERLINE_PERCENT
        - PRE_COMPACTION_SAFETY_MARGIN_PERCENT
    )

    def candidate_for(limit: int) -> tuple[ScenarioTurn, str, dict[str, Any]]:
        scenario, candidate = _work_packet(
            ordinal=ordinal,
            marker=marker,
            max_prompt_chars=limit,
        )
        preview = _pressure_metrics(_usage(client, conversation_id, draft=candidate))
        return scenario, candidate, preview

    full = candidate_for(max_prompt_chars)
    full_pressure = float(full[2].get("raw_pressure_percent") or 0.0)
    if full_pressure < target_percent:
        return full

    # 2,000 characters is also the CLI's minimum meaningful dossier size.  A
    # final pressure below target is preferable to violating the no-compaction
    # contract, and is reported as a normal scenario oracle failure.
    low, high = 2000, max_prompt_chars
    best_below_ceiling: tuple[ScenarioTurn, str, dict[str, Any]] | None = None
    best_at_target: tuple[ScenarioTurn, str, dict[str, Any]] | None = None
    while low <= high:
        limit = (low + high) // 2
        current = candidate_for(limit)
        pressure = float(current[2].get("raw_pressure_percent") or 0.0)
        if pressure >= safety_ceiling:
            high = limit - 1
            continue

        best_below_ceiling = current
        if pressure >= target_percent:
            best_at_target = current
            # Find the smallest dossier that still reaches the requested
            # observation point, leaving as much reply headroom as possible.
            high = limit - 1
        else:
            low = limit + 1

    return best_at_target or best_below_ceiling or full


def _inspection_probe() -> str:
    return (
        "请结合跨境履约项目资料、用户偏好、项目指令和刚才的发布讨论，给出上线前最终核对："
        "明确 2026-11-18 窗口、CHG-260918、回滚门槛、证据格式和责任升级路径。"
        "这是上下文统计检查草稿，只需简短回答。"
    )


def _usage(client: ApiClient, conversation_id: str, *, draft: str) -> dict[str, Any]:
    path = f"/conversations/{urllib.parse.quote(conversation_id)}/context-usage/preview"
    value = _data(client.request("POST", path, {"content": draft, "attached_file_ids": []}))
    return value if isinstance(value, dict) else {}


def _idle_usage(client: ApiClient, conversation_id: str) -> dict[str, Any]:
    path = f"/conversations/{urllib.parse.quote(conversation_id)}/context-usage"
    value = _data(client.request("GET", path))
    return value if isinstance(value, dict) else {}


def _pressure_metrics(usage: dict[str, Any]) -> dict[str, Any]:
    model = usage.get("model") if isinstance(usage.get("model"), dict) else {}
    visible = usage.get("usage") if isinstance(usage.get("usage"), dict) else {}
    preflight = usage.get("preflight") if isinstance(usage.get("preflight"), dict) else {}
    window = int(model.get("context_window_tokens") or 0)
    tokens_before = int(preflight.get("tokens_before") or visible.get("used_tokens") or 0)
    return {
        "window_tokens": window,
        "tokens_before": tokens_before,
        "tokens_after": int(preflight.get("tokens_after") or visible.get("used_tokens") or 0),
        "raw_pressure_percent": round(tokens_before / window * 100, 2) if window else None,
        "visible_percent": visible.get("percent"),
        "waterline": preflight.get("waterline"),
        "action": preflight.get("action"),
        "blocked_reason": preflight.get("blocked_reason"),
        "breakdown": usage.get("breakdown") or {},
    }


def _all_categories_active(metrics: dict[str, Any]) -> bool:
    breakdown = metrics.get("breakdown") or {}
    return all(int(breakdown.get(category) or 0) > 0 for category in UI_CATEGORIES)


def _assistant_reply_text(response: dict[str, Any]) -> str:
    reply = response.get("agent_reply")
    if isinstance(reply, dict):
        return str(reply.get("content") or "").strip()
    return ""


def _response_id(response: dict[str, Any], field: str) -> str | None:
    value = response.get(field)
    if isinstance(value, dict):
        raw = value.get("message_id") or value.get("id")
        return str(raw) if raw else None
    return None


def _normalise_for_checkpoint(value: str) -> str:
    return re.sub(r"[\s\-_/：:，,。；;、（）()【】\[\]‘’'\".!！?？]+", "", value).lower()


def _check_reply_checkpoint(scenario: ScenarioTurn, reply: str) -> dict[str, Any]:
    normalized = _normalise_for_checkpoint(reply)
    missing = [
        list(group)
        for group in scenario.required_any_groups
        if not any(_normalise_for_checkpoint(term) in normalized for term in group)
    ]
    forbidden = [
        term for term in scenario.forbidden_current_terms
        if _normalise_for_checkpoint(term) in normalized
    ]
    return {
        "passed": bool(reply) and not missing and not forbidden,
        "missing_any_of_groups": missing,
        "forbidden_current_terms": forbidden,
    }


def _project_retrieval_evidence(
    client: ApiClient,
    *,
    turn_marker: str,
    file_ids: Iterable[str],
) -> dict[str, Any]:
    """Use the owner-scoped audit API to prove that a project source was selected."""
    wanted = {str(value) for value in file_ids}
    for attempt in range(3):
        rows = _data(client.request("GET", "/context/audit/retrieval?limit=100"))
        items = rows.get("items") if isinstance(rows, dict) else []
        matching = [
            row for row in items if isinstance(row, dict)
            and turn_marker in str(row.get("query_excerpt") or "")
        ]
        if matching:
            run_id = str(matching[0].get("public_id") or "")
            detail = _data(client.request(
                "GET", f"/context/audit/retrieval/{urllib.parse.quote(run_id)}"
            ))
            candidates = detail.get("candidates") if isinstance(detail, dict) else []
            selected = [
                candidate for candidate in candidates if isinstance(candidate, dict)
                and bool(candidate.get("selected"))
                and str(candidate.get("source_public_id") or "") in wanted
            ]
            return {
                "audit_run_id": run_id or None,
                "fallback_code": matching[0].get("fallback_code"),
                "selected_project_source_ids": [
                    str(candidate.get("source_public_id")) for candidate in selected
                ],
                "selected_candidate_channels": [candidate.get("metadata") for candidate in selected],
                "passed": bool(selected),
            }
        if attempt < 2:
            time.sleep(1)
    return {
        "audit_run_id": None,
        "fallback_code": None,
        "selected_project_source_ids": [],
        "selected_candidate_channels": [],
        "passed": False,
    }


def _compaction_evidence(client: ApiClient, conversation_id: str) -> list[dict[str, Any]]:
    value = _data(client.request(
        "GET",
        f"/context/audit/compaction?conversation_id={urllib.parse.quote(conversation_id)}&limit=100",
    ))
    rows = value.get("items") if isinstance(value, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def _index_statuses(client: ApiClient, file_ids: Iterable[str]) -> list[dict[str, Any]]:
    wanted = {str(value) for value in file_ids}
    rows = _data(client.request("GET", "/context/index/documents?limit=100"))
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and str(row.get("source_public_id")) in wanted]


def _wait_for_indexing(client: ApiClient, file_ids: list[str], wait_seconds: int) -> tuple[bool, list[dict[str, Any]]]:
    deadline = time.monotonic() + wait_seconds
    latest: list[dict[str, Any]] = []
    while True:
        latest = _index_statuses(client, file_ids)
        by_source = {str(row.get("source_public_id")): row for row in latest}
        # Project-document pressure can proceed through the supported lexical
        # fallback.  Vector readiness is reported independently below; it must
        # never be silently described as healthy, but an optional dense-channel
        # outage must not erase an otherwise inspectable scenario.
        ready = len(by_source) == len(file_ids) and all(
            str(by_source[value].get("status")) == "indexed"
            and str(by_source[value].get("lexical_index_status")) == "ready"
            for value in file_ids
        )
        if ready:
            return True, latest
        if any(
            "failed" in {
                str(row.get("status")),
                str(row.get("lexical_index_status")),
            }
            for row in latest
        ) or time.monotonic() >= deadline:
            return False, latest
        time.sleep(3)


def _create_active_memories(client: ApiClient, marker: str) -> list[str]:
    memories = (
        "用户偏好：发布评审结论必须明确事实、推断和待确认项，禁止把建议写成已批准决定。",
        "用户偏好：所有时间统一同时标注 UTC 与业务地区时区，验收证据必须含 traceId。",
        "用户偏好：风险按 P0/P1/P2 分级；P0 未关闭不得进入全量发布。",
        "用户偏好：变更窗口检查必须引用 CHG-260918，回滚命令需要双人复核。",
        "用户偏好：回答先给结论，再给证据来源和责任组，避免宽泛的最佳实践。",
    )
    ids: list[str] = []
    for index, content in enumerate(memories, start=1):
        created = _data(client.request("POST", "/context/memory", {
            "scope_type": "user",
            "memory_type": "preference",
            "title": f"{SCENARIO} 发布偏好 {index}",
            "content": f"{content} 场景标识 {marker}。",
            "dedupe_key": f"ctx-project-50-{marker}-{index}",
        }))
        memory_id = _require_id(created, keys=("memory_public_id",), operation="create memory")
        _data(client.request("POST", f"/context/memory/{urllib.parse.quote(memory_id)}/activate"))
        ids.append(memory_id)
    return ids


def _delete_memories(client: ApiClient, memory_ids: Iterable[str]) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for memory_id in dict.fromkeys(str(value) for value in memory_ids if value):
        try:
            _data(client.request("DELETE", f"/context/memory/{urllib.parse.quote(memory_id)}"))
            outcomes.append({"kind": "memory", "id": memory_id, "deleted": True})
        except Exception as exc:  # cleanup evidence must not hide the primary result
            outcomes.append({"kind": "memory", "id": memory_id, "deleted": False, "error": type(exc).__name__})
    return outcomes


def _delete_state_records(client: ApiClient, state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *_delete_records(
            client,
            project_id=str(state.get("project_id") or "") or None,
            conversation_ids=[str(state.get("conversation_id") or "")],
            file_ids=[str(value) for value in state.get("file_ids") or []],
        ),
        *_delete_memories(client, state.get("memory_ids") or []),
    ]


def _owned_previous_state(client: ApiClient, state: dict[str, Any]) -> bool:
    project_id = str(state.get("project_id") or "")
    conversation_id = str(state.get("conversation_id") or "")
    if not project_id.startswith("prj_") or not conversation_id.startswith("conv_"):
        return False
    try:
        project = _data(client.request("GET", f"/projects/{urllib.parse.quote(project_id)}"))
        conversation = _data(client.request("GET", f"/conversations/{urllib.parse.quote(conversation_id)}"))
    except Exception:
        return False
    if not isinstance(project, dict) or not isinstance(conversation, dict):
        return False
    project_name = str(project.get("name") or "")
    conversation_title = str(conversation.get("title") or "")
    current_owned = (
        project_name.startswith(PROJECT_TITLE_PREFIX)
        and conversation_title.startswith(CONVERSATION_TITLE_PREFIX)
    )
    legacy_owned = (
        project_name.startswith(LEGACY_PROJECT_TITLE_PREFIX)
        and conversation_title.startswith(LEGACY_CONVERSATION_TITLE_PREFIX)
    )
    return current_owned or legacy_owned


def _send_chat(
    client: ApiClient,
    conversation_id: str,
    content: str,
    *,
    attached_file_ids: Iterable[str] = (),
) -> dict[str, Any]:
    path = f"/conversations/{urllib.parse.quote(conversation_id)}/messages"
    value = _data(client.request("POST", path, {
        "content": content,
        "attached_file_ids": list(attached_file_ids),
        "knowledge_mode_snapshot": "AUTO",
    }))
    return value if isinstance(value, dict) else {}


def _create_real_task_context(
    client: ApiClient,
    *,
    conversation_id: str,
    requirement_file_id: str,
) -> str:
    """Create the same task a user would create before continuing review chat.

    The Context Usage preview resolves the latest task in the conversation and
    can therefore include a genuine TASK_STATE section.  This deliberately
    uses the public message endpoint rather than writing an agent_tasks row.
    """
    response = _send_chat(
        client,
        conversation_id,
        "请基于已上传的《业务需求基线》创建本次跨境履约发布的测试方案任务；"
        "我没有上传测试方案模板，请使用系统默认测试方案模板。"
        "后续我会继续在当前会话评审方案，请保留项目资料、测试范围和发布门禁作为任务上下文。",
        attached_file_ids=(requirement_file_id,),
    )
    task_id = str(response.get("task_id") or "").strip()
    route = str(response.get("route") or "")
    if route != "agent_task" or not task_id:
        raise RuntimeError(
            "task-context seed did not create a task through the public message API: "
            f"route={route!r}, task_id_present={bool(task_id)}"
        )
    return task_id


_CONFIRMABLE_SECTION_ACTIONS = frozenset({
    "ai_generate",
    "keep_template",
    "manual_fill",
    "skip",
})
_TASK_FAILURE_STATUSES = frozenset({"failed", "cancelled", "canceled", "expired"})
_PREPARATION_CLARIFICATION_TYPE = "preparation_clarification"
_SCENARIO_CLARIFICATION_ANSWER = (
    "按本次跨境履约发布演练的保守测试范围执行：覆盖订单、库存、支付、"
    "履约、跨区域/时区异常和回滚；未明确能力不纳入全量发布，并以 UAT "
    "证据、traceId 与 CHG-260918 审批作为放行依据。"
)


def _recommended_confirmation_sections(pending: Any) -> list[dict[str, str]]:
    """Translate the Agent's pending recommendations into the confirm contract.

    A scenario must not guess a policy such as "all AI generate".  In
    particular, the default template intentionally retains a small number of
    standard sections.  The public confirmation API accepts ``action`` while
    the pending payload calls the Agent's choice ``suggested_action``.
    """
    if not isinstance(pending, dict):
        raise RuntimeError("pending task confirmation did not return an object")
    source_sections = pending.get("sections")
    if not isinstance(source_sections, list) or not source_sections:
        raise RuntimeError("pending task confirmation did not contain section recommendations")

    sections: list[dict[str, str]] = []
    for row in source_sections:
        if not isinstance(row, dict):
            raise RuntimeError("pending task confirmation contained a malformed section")
        section_id = str(row.get("section_id") or "").strip()
        action = str(row.get("suggested_action") or row.get("action") or "").strip().lower()
        if not section_id or action not in _CONFIRMABLE_SECTION_ACTIONS:
            raise RuntimeError(
                "pending task confirmation contained an invalid section recommendation: "
                f"section_id_present={bool(section_id)}, action={action!r}"
            )
        sections.append({"section_id": section_id, "action": action})
    return sections


def _preparation_clarification_response(pending: Any) -> dict[str, list[str] | dict[str, str]]:
    """Provide the scenario user's bounded answers to Preparation gap cards.

    A high-severity card explicitly permits a conservative scope decision, so
    the runner takes that public user option instead of inventing a product
    requirement.  Other cards need an answer; use the fixed release-readiness
    scope that is also reflected in the project instructions and fixture corpus.
    """
    if not isinstance(pending, dict):
        raise RuntimeError("pending preparation clarification did not return an object")
    cards = pending.get("cards")
    if not isinstance(cards, list) or not cards:
        raise RuntimeError("pending preparation clarification did not contain cards")

    answers: dict[str, str] = {}
    conservative_gap_ids: list[str] = []
    seen: set[str] = set()
    for card in cards:
        if not isinstance(card, dict):
            raise RuntimeError("pending preparation clarification contained a malformed card")
        gap_id = str(card.get("id") or "").strip()
        if not gap_id or gap_id in seen:
            raise RuntimeError(
                "pending preparation clarification contained an invalid card id: "
                f"gap_id={gap_id!r}"
            )
        seen.add(gap_id)
        if bool(card.get("allow_conservative_scope")):
            conservative_gap_ids.append(gap_id)
        else:
            answers[gap_id] = _SCENARIO_CLARIFICATION_ANSWER
    return {
        "answers": answers,
        "conservative_gap_ids": conservative_gap_ids,
    }


def _completed_test_plan_artifact(task_detail: Any) -> dict[str, Any]:
    """Return the durable Word deliverable required by a completed test-plan task.

    A terminal task status alone is not enough for this scenario.  The current
    test-plan graph exports a Word artifact after the section-confirmation
    decision; the task-detail contract restores that artifact after reconnects.
    Checking it here makes the scenario cover the full user-visible generation
    flow rather than just the orchestration state transition.
    """
    if not isinstance(task_detail, dict):
        raise RuntimeError("completed task detail did not return an object")
    artifact = task_detail.get("artifact")
    if not isinstance(artifact, dict):
        raise RuntimeError("completed test-plan task did not expose a Word artifact")

    artifact_id = str(artifact.get("artifact_id") or "").strip()
    artifact_type = str(artifact.get("artifact_type") or "").strip().lower()
    artifact_status = str(artifact.get("status") or "").strip().lower()
    file_ext = str(artifact.get("file_ext") or "").strip().lower().lstrip(".")
    if (
        not artifact_id
        or artifact_type != "test_plan_word"
        or artifact_status != "available"
        or file_ext != "docx"
    ):
        raise RuntimeError(
            "completed test-plan task did not expose an available DOCX artifact: "
            f"artifact_id_present={bool(artifact_id)}, artifact_type={artifact_type!r}, "
            f"status={artifact_status!r}, file_ext={file_ext!r}"
        )
    return artifact


def _wait_for_task_completion(
    client: ApiClient,
    *,
    task_id: str,
    wait_seconds: int,
) -> dict[str, Any]:
    """Wait for the real task, supply clarification cards, confirm sections, then finish.

    Test-plan generation can pause for bounded requirement clarifications before
    it reaches its mandatory section-confirmation boundary. Sending normal
    conversation messages before all task pauses reach a terminal state routes
    them to ``existing_task_action`` instead of ``chat_reply``. This helper
    exercises both public user flows and records each submitted decision.
    """
    deadline = time.monotonic() + wait_seconds
    transitions: list[str] = []
    confirmed_sections: list[dict[str, str]] | None = None
    confirmation_id: str | None = None
    confirmation_events: list[dict[str, Any]] = []
    submitted_confirmations: set[tuple[str, str]] = set()

    while True:
        detail = _data(client.request(
            "GET", f"/agent/tasks/{urllib.parse.quote(task_id)}"
        ))
        if not isinstance(detail, dict):
            raise RuntimeError("task status endpoint did not return an object")
        status = str(detail.get("status") or "").strip().lower()
        if not status:
            raise RuntimeError("task status endpoint did not return a status")
        if not transitions or transitions[-1] != status:
            transitions.append(status)

        if status == "completed":
            artifact = _completed_test_plan_artifact(detail)
            return {
                "task_id": task_id,
                "final_status": status,
                "transitions": transitions,
                "confirmation_id": confirmation_id,
                "confirmed_sections": confirmed_sections or [],
                "confirmation_events": confirmation_events,
                "artifact": artifact,
            }
        if status in _TASK_FAILURE_STATUSES:
            raise RuntimeError(
                f"test-plan task reached terminal failure status {status!r}; "
                f"transitions={transitions!r}"
            )
        if status == "waiting_user_confirm":
            pending = _data(client.request(
                "GET",
                f"/agent/tasks/{urllib.parse.quote(task_id)}/pending-confirmation",
            ))
            if not isinstance(pending, dict):
                raise RuntimeError("pending task confirmation did not return an object")
            pending_id = str(pending.get("confirmation_id") or "").strip()
            confirmation_type = str(pending.get("confirmation_type") or "").strip().lower()
            confirmation_key = (confirmation_type, pending_id)
            if not pending_id:
                raise RuntimeError("pending task confirmation did not contain a confirmation id")
            if confirmation_key in submitted_confirmations:
                raise RuntimeError(
                    "task remained waiting after the same confirmation was submitted: "
                    f"type={confirmation_type!r}, confirmation_id={pending_id!r}"
                )

            if confirmation_type == _PREPARATION_CLARIFICATION_TYPE:
                clarification = _preparation_clarification_response(pending)
                _data(client.request(
                    "POST",
                    f"/agent/tasks/{urllib.parse.quote(task_id)}/preparation-clarification",
                    clarification,
                ))
                confirmation_events.append({
                    "confirmation_id": pending_id,
                    "confirmation_type": confirmation_type,
                    "response": clarification,
                })
                transitions.append("preparation_clarification_submitted")
            elif confirmation_type in {"", "section_generation_config"}:
                if confirmed_sections is not None:
                    raise RuntimeError("task requested a second section confirmation")
                sections = _recommended_confirmation_sections(pending)
                _data(client.request(
                    "POST",
                    f"/agent/tasks/{urllib.parse.quote(task_id)}/confirm",
                    {"sections": sections},
                ))
                confirmation_id = pending_id
                confirmed_sections = sections
                confirmation_events.append({
                    "confirmation_id": pending_id,
                    "confirmation_type": "section_generation_config",
                    "response": {"sections": sections},
                })
                transitions.append("section_confirmation_submitted")
            else:
                raise RuntimeError(
                    "task requested an unsupported pending confirmation type: "
                    f"{confirmation_type!r}"
                )
            submitted_confirmations.add(confirmation_key)

        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"test-plan task did not complete within {wait_seconds}s; "
                f"last_status={status!r}, transitions={transitions!r}"
            )
        time.sleep(1)


def _upload_conversation_requirement(
    client: MultipartApiClient,
    *,
    conversation_id: str,
    filename: str,
    content: bytes,
) -> str:
    """Upload the requirement through the conversation attachment contract."""
    uploaded = _data(client.upload(
        "/files/upload",
        filename=filename,
        content=content,
        fields={"conversation_id": conversation_id},
    ))
    requirement_file_id = _require_id(
        uploaded,
        keys=("id", "file_id"),
        operation="upload task requirement into conversation",
    )
    _data(client.request(
        "POST",
        f"/files/{urllib.parse.quote(requirement_file_id)}/confirm-type",
        {"file_type": "requirement_doc"},
    ))
    return requirement_file_id


def _run(args: argparse.Namespace, run: LctRun) -> str:
    username = os.environ.get("PHASE2_E2E_USERNAME", "")
    password = os.environ.get("PHASE2_E2E_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("PHASE2_E2E_USERNAME and PHASE2_E2E_PASSWORD are required")

    marker = _stamp()
    client = MultipartApiClient(args.base_url, timeout_seconds=args.request_timeout_seconds)
    client.login(username, password)
    previous = _read_state()
    project_id: str | None = None
    conversation_id: str | None = None
    task_id: str | None = None
    file_ids: list[str] = []
    conversation_requirement_file_id: str | None = None
    memory_ids: list[str] = []
    retained = False
    measurements: list[dict[str, Any]] = []
    probe = ""
    crossed_prepare = False
    reached_transition = False
    sent_turns = 0
    final: dict[str, Any] = {}
    failure: dict[str, str] | None = None
    turn_records: list[dict[str, Any]] = []
    retrieval_records: list[dict[str, Any]] = []
    task_completion: dict[str, Any] = {}

    try:
        project = _data(client.request("POST", "/projects", {
            "name": f"{PROJECT_TITLE_PREFIX}{marker}",
            "description": "真实跨境履约项目上下文压力样本；验证五类统计、60%原文保留水位与自动摘要。",
            "memoryMode": "project_memory",
        }))
        project_id = _require_id(project, keys=("id",), operation="create project")
        conversation = _data(client.request(
            "POST",
            f"/projects/{urllib.parse.quote(project_id)}/conversations",
            {"title": f"{CONVERSATION_TITLE_PREFIX}{marker}"},
        ))
        conversation_id = _require_id(conversation, keys=("id",), operation="create conversation")
        run.observe(
            run.step("create retained project conversation", component="Project API", expected="isolated project and conversation exist"),
            project_id.startswith("prj_") and conversation_id.startswith("conv_"),
            actual=json.dumps({"project_id": project_id, "conversation_id": conversation_id}, ensure_ascii=False),
        )

        instructions = (
            "你是跨境履约平台 2.0 的发布质量助手。事实优先级为当前项目资料、已确认决定、用户记忆、一般建议；"
            "任何结论必须区分事实与推断。发布窗口固定为 2026-11-18 02:00-05:00 UTC，变更单为 CHG-260918。"
            "P0 未关闭、支付对账差异超过 0.1%、核心链路五分钟错误率超过 1% 均触发停止或回滚。"
        )
        _data(client.request("PUT", f"/projects/{urllib.parse.quote(project_id)}/instructions", {"instructions": instructions}))
        memory_ids = _create_active_memories(client, marker)

        with tempfile.TemporaryDirectory(prefix="ctx-project-50-") as fixture_root:
            specs = _make_fixture_docs(Path(fixture_root), marker)
            manifest = run.write_evidence("fixture-manifest.json", [
                {key: value for key, value in spec.items() if key != "path"} for spec in specs
            ])
            for spec in specs:
                uploaded = _data(client.upload(
                    f"/projects/{urllib.parse.quote(project_id)}/sources/upload",
                    filename=str(spec["filename"]),
                    content=Path(spec["path"]).read_bytes(),
                    fields={"source_role": str(spec["role"])},
                ))
                uploaded_file_id = _require_id(
                    uploaded,
                    keys=("fileId", "file_id"),
                    operation=f"upload {spec['label']}",
                )
                file_ids.append(uploaded_file_id)
            requirement_spec = next(
                spec for spec in specs if spec["label"] == "业务需求基线"
            )
            conversation_requirement_file_id = _upload_conversation_requirement(
                client,
                conversation_id=conversation_id,
                filename=str(requirement_spec["filename"]),
                content=Path(requirement_spec["path"]).read_bytes(),
            )
        indexed, statuses = _wait_for_indexing(client, file_ids, args.index_wait_seconds)
        run.observe(
            run.step("index ten project documents", component="Context Index API", expected="all generated DOCX sources indexed"),
            indexed and len(file_ids) == len(DOCUMENT_SPECS),
            actual=json.dumps({"count": len(file_ids), "statuses": statuses}, ensure_ascii=False),
            evidence={"fixture_manifest": manifest},
        )
        vector_degraded = [
            row
            for row in statuses
            if str(row.get("vector_index_status")) != "ready"
        ]
        if vector_degraded:
            counts: dict[str, int] = {}
            for row in vector_degraded:
                status = str(row.get("vector_index_status") or "unknown")
                counts[status] = counts.get(status, 0) + 1
            run.notes.append(
                "VECTOR_INDEX_DEGRADED_USING_LEXICAL_FALLBACK: "
                + json.dumps(counts, ensure_ascii=False, sort_keys=True)
            )
        if not indexed:
            raise RuntimeError("project document indexing did not complete")
        if not conversation_requirement_file_id:
            raise RuntimeError("conversation requirement upload did not return a file id")

        task_id = _create_real_task_context(
            client,
            conversation_id=conversation_id,
            requirement_file_id=conversation_requirement_file_id,
        )
        run.observe(
            run.step(
                "create a real task in the retained conversation",
                component="Message API + Agent Task",
                expected="a user-style test-plan request creates the latest conversation task",
            ),
            bool(task_id),
            actual=json.dumps({"task_id": task_id}, ensure_ascii=False),
        )
        completion_step = run.step(
            "complete the test-plan task before review conversation",
            component="Agent Task confirmation + outbox worker",
            expected=(
                "the task reaches completed after clarification/section confirmation "
                "and exposes its available test-plan DOCX artifact"
            ),
        )
        try:
            task_completion = _wait_for_task_completion(
                client,
                task_id=task_id,
                wait_seconds=args.task_wait_seconds,
            )
        except Exception as exc:
            run.observe(
                completion_step,
                False,
                actual=f"{type(exc).__name__}: {exc}",
            )
            raise
        run.observe(
            completion_step,
            task_completion.get("final_status") == "completed",
            actual=json.dumps(task_completion, ensure_ascii=False),
        )

        for ordinal in range(1, args.max_turns + 1):
            idle_before = _pressure_metrics(_idle_usage(client, conversation_id))
            measurements.append({"turn": ordinal, "phase": "idle_before", **idle_before})
            scenario, candidate, preview = _pre_compaction_work_packet(
                client,
                conversation_id=conversation_id,
                ordinal=ordinal,
                marker=marker,
                max_prompt_chars=args.max_prompt_chars,
                target_percent=args.target_percent,
            )
            measurements.append({
                "turn": ordinal,
                "phase": "before_send",
                "draft_chars": len(candidate),
                **preview,
            })
            raw_percent = float(preview.get("raw_pressure_percent") or 0.0)
            if raw_percent >= (
                CONVERSATION_COMPACTION_WATERLINE_PERCENT
                - PRE_COMPACTION_SAFETY_MARGIN_PERCENT
            ):
                # Never post a dossier that would leave insufficient room for
                # the persisted assistant reply.  This scenario is intended to
                # inspect the raw pre-compaction state, not exercise the 60%
                # retention transition.
                final = idle_before
                break

            started_at = time.monotonic()
            response = _send_chat(client, conversation_id, candidate)
            duration_ms = round((time.monotonic() - started_at) * 1000, 1)
            probe = candidate
            route = str(response.get("route") or "")
            reply = _assistant_reply_text(response)
            if route != "chat_reply" or not reply:
                raise RuntimeError(
                    f"turn {ordinal} did not complete as a synchronous chat reply: "
                    f"route={route!r}, assistant_reply_present={bool(reply)}"
                )
            sent_turns += 1
            final = {
                "turn": ordinal,
                "phase": "idle_after_send",
                **_pressure_metrics(_idle_usage(client, conversation_id)),
            }
            measurements.append(final)
            checkpoint = _check_reply_checkpoint(scenario, reply)
            retrieval = None
            if scenario.requires_project_retrieval:
                retrieval = _project_retrieval_evidence(
                    client,
                    turn_marker=f"{SCENARIO}:{marker}:T{ordinal:02d}",
                    file_ids=file_ids,
                )
                retrieval_records.append({"turn": ordinal, **retrieval})
            turn_records.append({
                "turn_no": ordinal,
                "phase": scenario.phase,
                "prompt": candidate,
                "route": route,
                "intent": response.get("intent"),
                "task_id": response.get("task_id"),
                "user_message_id": _response_id(response, "message"),
                "assistant_message_id": _response_id(response, "agent_reply"),
                "assistant_response": reply,
                "request_duration_ms": duration_ms,
                "context_usage": final,
                "checkpoint": checkpoint,
                "project_retrieval": retrieval,
            })
            crossed_prepare = crossed_prepare or raw_percent >= args.prepare_percent
            reached_transition = raw_percent >= args.target_percent
            if reached_transition:
                break

        checkpoint_records = [
            record for record in turn_records
            if record.get("checkpoint", {}).get("missing_any_of_groups")
            or record.get("checkpoint", {}).get("forbidden_current_terms")
        ]
        retrieval_step = run.step(
            "select uploaded project sources during source-dependent turns",
            component="Context Retrieval audit",
            expected="each source-dependent QA turn has an audit-selected uploaded project source",
        )
        run.observe(
            retrieval_step,
            bool(retrieval_records) and all(bool(row.get("passed")) for row in retrieval_records),
            actual=json.dumps(retrieval_records, ensure_ascii=False),
        )
        checkpoint_step = run.step(
            "preserve requirement evolution and delayed recall",
            component="assistant reply checkpoints",
            expected="P0 rule and CR-002 current/obsolete relationship are answered correctly",
        )
        run.observe(
            checkpoint_step,
            not checkpoint_records,
            actual=json.dumps(checkpoint_records, ensure_ascii=False),
        )
        category_step = run.step("activate all five Context Usage categories", component="Context Usage preview", expected="every UI category has non-zero selected tokens")
        run.observe(category_step, _all_categories_active(final), actual=json.dumps(final.get("breakdown") or {}, ensure_ascii=False))
        pressure_step = run.step(
            "reach the pre-compaction observation target",
            component="Context Engine + Context Usage",
            expected=(
                f"a real send reaches raw {args.target_percent}% while remaining below "
                f"the {CONVERSATION_COMPACTION_WATERLINE_PERCENT}% retention waterline"
            ),
        )
        run.observe(
            pressure_step,
            reached_transition,
            actual=json.dumps(
                {
                    "failure_code": None if reached_transition else "PRE_COMPACTION_TARGET_NOT_REACHED_WITH_REALISTIC_SCENARIO",
                    "final": final,
                    "completed_chat_turns": sent_turns,
                },
                ensure_ascii=False,
            ),
        )
        prepare_step = run.step("approach the pre-compaction target", component="Context Usage", expected=f"raw conversation reaches {args.prepare_percent}% before the {args.target_percent}% observation target")
        run.observe(prepare_step, crossed_prepare, actual=json.dumps({"measurements": len(measurements)}, ensure_ascii=False))
        compaction_rows = _compaction_evidence(client, conversation_id)
        compaction_step = run.step(
            "preserve the raw pre-compaction conversation",
            component="Context Compaction audit",
            expected="no automatic conversation compaction run is recorded",
        )
        automatic_compaction = any(
            str(row.get("status") or "") == "completed"
            and str(row.get("compaction_type") or "") == "conversation"
            for row in compaction_rows
        )
        run.observe(
            compaction_step,
            not automatic_compaction,
            actual=json.dumps(compaction_rows, ensure_ascii=False),
        )

        evidence = run.write_evidence("project-context-pressure.json", {
            "scenario": SCENARIO,
            "project_id": project_id,
            "conversation_id": conversation_id,
            "task_id": task_id,
            "task_completion": task_completion,
            "file_ids": file_ids,
            "conversation_requirement_file_id": conversation_requirement_file_id,
            "memory_ids": memory_ids,
            "last_sent_review": probe,
            "target_percent": args.target_percent,
            "prepare_percent": args.prepare_percent,
            "max_prompt_chars": args.max_prompt_chars,
            "completed_chat_turns": sent_turns,
            "measurements": measurements,
            "turn_records": turn_records,
            "retrieval_records": retrieval_records,
            "compaction_runs": compaction_rows,
            "api_calls": client.calls,
        })
        for step in run.steps:
            step.evidence.setdefault("scenario_evidence", evidence)

        # This runner is specifically meant to leave an inspectable specimen.
        # Retain even a near-target diagnostic result; its manifest records the
        # exact measured shortfall instead of silently deleting the evidence.
        state = {
            "scenario": SCENARIO,
            "project_id": project_id,
            "conversation_id": conversation_id,
            "task_id": task_id,
            "task_completion": task_completion,
            "file_ids": file_ids,
            "conversation_requirement_file_id": conversation_requirement_file_id,
            "memory_ids": memory_ids,
            "last_sent_review": probe,
            "final_metrics": final,
            "completed_chat_turns": sent_turns,
            "target_reached": reached_transition,
            "measurements": measurements,
            "turn_records": turn_records,
            "retrieval_records": retrieval_records,
            "compaction_runs": compaction_rows,
            "failure": None,
            "retained_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        _write_state(state)
        retained = True
        run.notes.append(
            "RETAINED_FOR_UI_INSPECTION: open the retained conversation; the idle card recomputes the effective working set without pasting a draft."
        )

        if previous and _owned_previous_state(client, previous):
            cleanup = _delete_state_records(client, previous)
            run.write_evidence("previous-retained-cleanup.json", cleanup)
            if not all(bool(item.get("deleted")) for item in cleanup):
                run.notes.append("PREVIOUS_SAMPLE_CLEANUP_PARTIAL: inspect cleanup evidence before rerunning.")
        return "LCT_PASS" if not any(step.status == "FAIL" for step in run.steps) else "TEST_FAIL"
    except Exception as exc:
        failure = {
            "type": type(exc).__name__,
            "message": str(exc)[:500],
        }
        raise
    finally:
        if project_id and conversation_id and not retained:
            # A created project conversation is the primary diagnostic artifact.
            # Preserve it even when setup, indexing, routing, compression or a
                # final oracle fails; otherwise the frontend cannot inspect the
            # exact state that caused the failure.
            retained = True
            partial_state = {
                "scenario": SCENARIO,
                "project_id": project_id,
                "conversation_id": conversation_id,
                "task_id": task_id,
                "task_completion": task_completion,
                "file_ids": file_ids,
                "conversation_requirement_file_id": conversation_requirement_file_id,
                "memory_ids": memory_ids,
                "last_sent_review": probe,
                "final_metrics": final,
                "completed_chat_turns": sent_turns,
                "target_reached": reached_transition,
                "measurements": measurements,
                "turn_records": turn_records,
                "retrieval_records": retrieval_records,
                "failure": failure,
                "retained_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            try:
                partial_evidence = run.write_evidence(
                    "project-context-pressure-partial.json",
                    partial_state,
                )
                _write_state(partial_state)
                for step in run.steps:
                    step.evidence.setdefault("partial_scenario_evidence", partial_evidence)
                run.notes.append(
                    "RETAINED_AFTER_FAILURE_FOR_UI_INSPECTION: the created project and conversation were not deleted."
                )
            except Exception as retain_exc:  # evidence failure must not trigger destructive cleanup
                run.notes.append(
                    "RETAINED_AFTER_FAILURE_WITHOUT_MANIFEST: "
                    f"{type(retain_exc).__name__}"
                )
        if not retained:
            cleanup = _delete_records(
                client,
                project_id=project_id,
                conversation_ids=[conversation_id] if conversation_id else [],
                file_ids=file_ids,
            )
            cleanup.extend(_delete_memories(client, memory_ids))
            if cleanup:
                run.write_evidence("cleanup.json", cleanup)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the real pre-compaction context-observation scenario")
    parser.add_argument("--base-url", default="http://localhost:8000/api")
    parser.add_argument("--request-timeout-seconds", type=int, default=240)
    parser.add_argument("--index-wait-seconds", type=int, default=300)
    parser.add_argument("--task-wait-seconds", type=int, default=900)
    parser.add_argument("--prepare-percent", type=float, default=40.0)
    parser.add_argument("--target-percent", type=float, default=45.0)
    parser.add_argument("--max-prompt-chars", type=int, default=30000)
    parser.add_argument("--max-turns", type=int, default=30)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if not 1 <= args.prepare_percent < args.target_percent < CONVERSATION_COMPACTION_WATERLINE_PERCENT:
        parser.error(
            "require 1 <= --prepare-percent < --target-percent < "
            f"{CONVERSATION_COMPACTION_WATERLINE_PERCENT:g} for the pre-compaction scenario"
        )
    if not 2000 <= args.max_prompt_chars <= 50000:
        parser.error("--max-prompt-chars must be between 2000 and 50000")
    if not 30 <= args.task_wait_seconds <= 1800:
        parser.error("--task-wait-seconds must be between 30 and 1800")
    if not 5 <= args.max_turns <= len(REALISTIC_TURNS):
        parser.error(f"--max-turns must be between 5 and {len(REALISTIC_TURNS)}")
    run = LctRun(
        lct=SCENARIO,
        repo_root=repository_root(),
        execution_path="LIVE_PROJECT_API + REAL_DOCX + REAL_MODEL + DRAFT_PREVIEW + RETAINED_SPECIMEN",
    )
    try:
        verdict = _run(args, run)
        return run.finish(lct_verdict=verdict, classification="real five-category context pressure scenario")
    except Exception as exc:
        return cli_failure(run, exc, classification="real five-category context pressure scenario")


if __name__ == "__main__":
    raise SystemExit(main())
