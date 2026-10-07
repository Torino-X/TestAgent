"""Phase 2.9B.4 — Tagged Narrative Stream V1 增量解码器。

协议(见 docs/93 技术方案 §9.2):

    <HEADLINE>
    需求文档解析已完成
    </HEADLINE>
    <SUMMARY>
    已识别需求文档中的主要业务结构。
    </SUMMARY>
    <IMPACT>
    这些结果将用于确定测试范围。
    </IMPACT>
    <NEXT_ACTION>
    接下来将解析测试方案模板。
    </NEXT_ACTION>
    <DETAIL>
    识别章节 43 个
    </DETAIL>
    <DETAIL>
    识别关键表格 7 个
    </DETAIL>

要求:
* 支持标签跨 token、文本跨 chunk;
* 支持重复 DETAIL;
* 拒绝标签外正文;
* 检测未闭合标签;
* 检测缺少必填字段;
* 不把半截标签展示给用户(仅输出已闭合字段的 delta);
* 输出 field 级 delta;
* 最终输出完整 PublicExecutionUpdate(headline/summary/impact/next_action/details)。

实现: 增量解析器维护一个当前标签栈。文本累积到当前打开标签;标签闭合时
产生一个已完成字段值,并可作为 delta flush。标签外正文被忽略(并标记
``outside_text_seen`` 以便调用方决定是否容忍/拒绝)。
"""

from __future__ import annotations

import re
from typing import List, Optional

from .schemas import NarrativeStreamChunk

_TAG_RE = re.compile(r"</?(HEADLINE|SUMMARY|IMPACT|NEXT_ACTION|DETAIL|NARRATIVE)\s*>")
_TAGS = {"HEADLINE", "SUMMARY", "IMPACT", "NEXT_ACTION", "DETAIL", "NARRATIVE"}


class StreamDecodeError(ValueError):
    """流解码错误(未闭合标签 / 非法结构)。"""


class NarrativeStreamDecoder:
    """增量 Tagged Narrative Stream V1 解码器。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        # 增量解码器的内部状态:
        #   _buffer            累计未被消费的 token 文本(可能含半截标签)
        #   _open_tag          当前打开的标签(None 表示当前没在标签内)
        #   _fields            单值字段的累积内容(HEADLINE/SUMMARY/IMPACT/NEXT_ACTION/NARRATIVE)
        #   _detail_items      DETAIL 字段的多条累积(列表,每条对应一个 <DETAIL>...</DETAIL>)
        #   _chunk_index       自增 chunk 索引,前端 reducer 用来去重 SSE chunk
        #   _outside_text_seen 记录模型在标签外写了正文(协议要求拒绝,本类仅记录标记)
        #   _closed_tags       已闭合的标签集合(目前未使用,预留审计)
        self._buffer = ""
        self._open_tag: Optional[str] = None
        self._fields: dict[str, str] = {}
        self._detail_items: List[str] = []
        self._chunk_index = 0
        self._outside_text_seen = False
        self._closed_tags: set[str] = set()

    # ── 主入口 ──────────────────────────────────────────────────────────

    def feed(self, text: str) -> List[NarrativeStreamChunk]:
        """喂入一段 token 文本,返回已完成的 field 级 delta。

        协议:流式 LLM 输出的 token 块依次喂入,本方法:
          1) 把 token 拼接到 buffer;
          2) 在 buffer 内搜索最早的标签;
          3) 处理"标签前的内容"(属于当前打开标签的部分);
          4) 处理标签本身(开/关);
          5) 标签闭合时产出 field 完成的 delta。
        """
        self._buffer += text
        deltas: List[NarrativeStreamChunk] = []
        # 循环:一次 feed 可能包含多个标签闭合,要全部处理。
        while True:
            match = _TAG_RE.search(self._buffer)
            if match is None:
                break
            # 1) 标签前文本:属于当前打开标签。
            before = self._buffer[: match.start()]
            tag = match.group(1)
            is_closing = self._buffer[match.start() : match.start() + 2] == "</"
            # 2) 标签间文本:若没开标签,属于"标签外正文"。
            if before:
                if self._open_tag is None:
                    # 模型在标签外写了正文 → 仅标记不产出 delta。
                    # 由上层 composer 决定是否容忍(目前严格模式下 goto fallback)。
                    self._outside_text_seen = True
                else:
                    # 正常:累计到当前字段值。
                    self._accumulate(self._open_tag, before)
            # 3) 处理标签本身:闭合 / 开头。
            if is_closing:
                # 闭合必须匹配当前打开的标签;否则视为协议错误 → raise。
                if self._open_tag != tag:
                    raise StreamDecodeError(
                        f"unexpected closing tag </{tag}> (open={self._open_tag})"
                    )
                value = self._finish_field(tag)
                if value is not None:
                    deltas.append(value)
                self._open_tag = None
            else:
                # 开头:本协议不允许嵌套,遇到嵌套 raise(防 prompt injection)。
                if self._open_tag is not None:
                    raise StreamDecodeError(
                        f"nested opening tag <{tag}> inside <{self._open_tag}>"
                    )
                self._open_tag = tag
                if tag == "DETAIL":
                    # DETAIL 支持多条并列,遇到新的 <DETAIL> 就压一个空槽位。
                    self._detail_items.append("")
            self._buffer = self._buffer[match.end():]
        return deltas

    def finish(self) -> List[NarrativeStreamChunk]:
        """流结束后调用: 冲刷剩余缓冲并校验完整性。

        返回剩余 deltas;若存在未闭合标签或缺少必填字段则抛 StreamDecodeError。
        """
        if self._open_tag is not None:
            # 模型流中断,留了未闭合的标签(如 <HEADLINE> 没闭合)。
            raise StreamDecodeError(f"unclosed tag <{self._open_tag}>")
        if self._buffer.strip():
            # 收尾的字符串不再是标签(应是空白),视为"标签外正文"。
            self._outside_text_seen = True
            self._buffer = ""
        # 必填字段校验:如果模型走 NARRATIVE 协议,不需要 4 个 HEADLINE/SUMMARY 等;
        # 走 HEADLINE/SUMMARY 协议时,必填 4 个字段不能缺。
        if not self._field_text("NARRATIVE"):
            missing = []
            for field in ("HEADLINE", "SUMMARY", "IMPACT", "NEXT_ACTION"):
                if not self._field_text(field):
                    missing.append(field)
            if missing:
                raise StreamDecodeError(f"missing required field(s): {missing}")
        return []

    # ── helpers ─────────────────────────────────────────────────────────

    def _accumulate(self, tag: str, text: str) -> None:
        if tag == "DETAIL":
            if self._detail_items:
                self._detail_items[-1] += text
            else:
                self._detail_items.append(text)
        else:
            self._fields[tag] = self._fields.get(tag, "") + text

    def _finish_field(self, tag: str) -> Optional[NarrativeStreamChunk]:
        if tag == "DETAIL":
            value = self._detail_items[-1].strip() if self._detail_items else ""
            if not value:
                # 空 DETAIL 项忽略。
                if self._detail_items:
                    self._detail_items.pop()
                return None
            self._chunk_index += 1
            return NarrativeStreamChunk(
                field="details", delta=value, chunk_index=self._chunk_index
            )
        value = self._fields.get(tag, "").strip()
        field = tag.lower()
        if field == "next_action":
            field = "next_action"
        if field == "narrative":
            field = "narrative_text"
        if not value:
            return None
        self._chunk_index += 1
        return NarrativeStreamChunk(
            field=field,  # type: ignore[arg-type]
            delta=value,
            chunk_index=self._chunk_index,
        )

    def _field_text(self, tag: str) -> str:
        if tag == "DETAIL":
            return "".join(self._detail_items)
        return self._fields.get(tag, "").strip()

    # ── 输出 ────────────────────────────────────────────────────────────

    def public_update_dict(self) -> dict:
        """组装完整 PublicExecutionUpdate(合同五字段)。"""
        narrative_text = self._field_text("NARRATIVE")
        headline = self._field_text("HEADLINE") or narrative_text[:80]
        summary = self._field_text("SUMMARY") or narrative_text[:200]
        return {
            "headline": headline,
            "summary": summary,
            "impact": self._field_text("IMPACT"),
            "next_action": self._field_text("NEXT_ACTION"),
            "details": [d.strip() for d in self._detail_items if d.strip()],
            "narrative_text": narrative_text,
        }

    @property
    def outside_text_seen(self) -> bool:
        return self._outside_text_seen

    @property
    def detail_count(self) -> int:
        return len(self._detail_items)


__all__ = ["NarrativeStreamDecoder", "StreamDecodeError"]
