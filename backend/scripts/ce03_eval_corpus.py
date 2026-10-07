"""CE-03 评测语料：为 dataset.json 的 expected 文档生成脱敏测试文档内容。

生成的文档用于索引到 ES(lexical) 与 Qdrant(dense)，
使评测能对 expected_doc_ids / expected_chunk_ids 命中。

用法：
  python scripts/ce03_eval_corpus.py   # 打印语料 JSON（供索引脚本/runner 使用）

文档内容为脱敏测试文档（不包含真实业务数据/机密）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

DATASET_PATH = Path(__file__).resolve().parent.parent / "tests" / "ce03_eval" / "dataset.json"
OUT_PATH = Path(__file__).resolve().parent.parent / "tests" / "ce03_eval" / "corpus.json"


# 每个 doc_xxx 的脱敏内容模板（与查询语义匹配，可被 lexical 命中）
_DOC_CONTENT = {
    "doc_k01": "测试计划的风险分析章节应识别项目风险、测试风险与发布风险，并给出缓解策略。",
    "doc_k02": "接口自动化测试的环境准备步骤包括配置依赖服务、准备测试数据与初始化凭据。",
    "doc_k03": "性能测试的并发模型选择取决于被测系统的线程模型、连接池与压测目标。",
    "doc_k04": "安全测试的威胁建模方法包括 STRIDE、攻击树与数据流图分析。",
    "doc_k05": "兼容性测试矩阵应覆盖操作系统、浏览器、分辨率与网络环境的组合。",
    "doc_k06": "测试数据准备的最佳实践包括数据脱敏、复用池与按用例维度隔离。",
    "doc_k07": "缺陷报告的复现步骤应描述前置条件、操作序列、预期结果与实际结果。",
    "doc_k08": "测试环境与生产环境的差异包括配置、数据规模与网络拓扑，需评估影响。",
    "doc_k09": "自动化测试的稳定性策略包括重试机制、等待策略与幂等设计。",
    "doc_k10": "单元测试的覆盖率目标应根据代码复杂度设定，并关注分支与边界。",
    "doc_k11": "集成测试的接口依赖桩处理应隔离外部系统并模拟边界响应。",
    "doc_k12": "回归测试的选择策略应基于变更影响分析、风险等级与执行成本。",
    "doc_k13": "冒烟测试的范围应覆盖核心链路与关键入口，快速反馈构建健康度。",
    "doc_k14": "测试用例评审的检查清单包括需求覆盖、前置条件、步骤可执行性与断言完整性。",
    "doc_k15": "测试进度跟踪的度量指标包括用例通过率、缺陷密度与阻塞项数量。",
    "doc_ws01": "登录模块的测试要点包括正常登录、错误密码、锁定策略与验证码。",
    "doc_ws02": "支付流程的异常场景测试包括余额不足、超时、重复提交与回调失败。",
    "doc_ws03": "导出功能的并发测试重点包括并发导出不冲突、文件完整性与大文件处理。",
    "doc_ws04": "上传模块的边界测试包括空文件、超大文件、非法类型与并发上传。",
    "doc_ws05": "权限控制的测试设计包括角色隔离、越权访问与最小权限验证。",
    "doc_ws06": "消息推送的可靠性测试包括断网恢复、重复推送与送达确认。",
    "doc_ws07": "数据导入的校验规则测试包括必填、格式、长度与唯一性冲突。",
    "doc_ws08": "报表生成的性能测试包括大数据集渲染、缓存命中与并发导出。",
    "doc_ws09": "搜索功能的排序验证包括相关性、时间与字段权重组合。",
    "doc_ws10": "通知中心的幂等性测试包括重复触发不重复发送与状态幂等。",
    "doc_ws11": "数据看板的刷新频率验证包括手动刷新、定时刷新与并发读取。",
    "doc_ws12": "移动端适配的测试矩阵包括分辨率、字体缩放与横竖屏切换。",
    "doc_ws13": "日志系统的存储与清理测试包括日志轮转、容量上限与检索性能。",
    "doc_ws14": "外部接口的容错测试包括超时、重试、降级与熔断。",
    "doc_ws15": "配置中心的变更生效验证包括热更新、回滚与多环境隔离。",
}

_MEMORY_CONTENT = {
    "mem_u01": "用户偏好使用本地测试环境。",
    "mem_u02": "用户常用的测试工具是自研回归平台。",
    "mem_u03": "用户重点关注功能与性能测试。",
    "mem_u04": "用户质量目标是零线上缺陷。",
    "mem_u05": "用户常用技术栈为 Python 与 Vue。",
    "mem_u06": "用户偏好异步沟通方式。",
    "mem_u07": "用户项目背景是大型企业应用。",
    "mem_u08": "用户偏好基于角色的权限认证。",
    "mem_u09": "用户部署环境为云上 Kubernetes。",
    "mem_u10": "用户权限需求是分级管理。",
    "mem_ws01": "本工作区常用 pytest 与 Playwright。",
    "mem_ws02": "本工作区部署环境约定为双环境。",
    "mem_ws03": "本工作区安全规范要求机密分级。",
    "mem_ws04": "本工作区质量标准为用例全通过。",
    "mem_ws05": "本工作区工具链为 GitLab 与 Jenkins。",
    "mem_ws06": "本工作区测试数据规范为脱敏复用。",
    "mem_ws07": "本工作区发布流程为灰度发布。",
    "mem_ws08": "本工作区沟通渠道为飞书群。",
    "mem_ws09": "本工作区监控告警规范为阈值分级。",
    "mem_ws10": "本工作区数据备份策略为每日全量。",
    "mem_pb01": "准备阶段应生成覆盖需求与边界的用例。",
    "mem_pb02": "评审阶段应核对需求追溯与断言完整。",
    "mem_pb03": "修复阶段应回归受影响链路。",
    "mem_pb04": "增量更新应评估变更影响范围。",
    "mem_pb05": "生成阶段应输出结构化测试方案。",
}


def build_corpus() -> dict:
    data = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    corpus: dict[str, dict] = {}
    for q in data["queries"]:
        for did in q.get("expected_doc_ids") or []:
            content = _DOC_CONTENT.get(did)
            if content:
                corpus[did] = {
                    "document_id": did,
                    "chunk_id": q.get("expected_chunk_ids", [None])[0] if q.get("expected_chunk_ids") else f"chunk_{did}",
                    "content": content,
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                }
        for mid in q.get("expected_memory_ids") or []:
            content = _MEMORY_CONTENT.get(mid)
            if content:
                corpus[mid] = {
                    "memory_id": mid,
                    "content": content,
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                }
    return {"dataset_id": data["dataset_id"], "version": data["dataset_version"], "docs": corpus}


def main() -> None:
    corpus = build_corpus()
    OUT_PATH.write_text(json.dumps(corpus, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"corpus written: {OUT_PATH} docs={len(corpus['docs'])}")


if __name__ == "__main__":
    main()
