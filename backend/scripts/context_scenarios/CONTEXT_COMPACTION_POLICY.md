# TestAgent context retention and compaction policy

## Decision

The corrected 50% figure is a preparation waterline for the **next assembled
model request**, not a percentage of all messages ever stored. Reaching 50%
must not immediately summarize or delete the raw conversation.

TestAgent should keep three separate concepts:

1. Durable history: all original messages remain in the database and are never
   deleted by prompt compaction.
2. Active prompt: only context selected for the next model request contributes
   to the usage percentage.
3. Compaction artifacts: versioned summaries/checkpoints cover older messages,
   while a verbatim tail remains available to the model.

Recommended waterlines for normal chat:

| Active request pressure | Action |
| --- | --- |
| below 50% | Keep the normal verbatim window and retrieve relevant project data. |
| 50-65% | Prepare asynchronously: refresh summaries, estimate growth, cap bulky tool results, but do not replace recent raw turns. |
| 65-80% | Show an orange warning and recommend compression; keep the selected working set intact so recent decisions do not disappear merely because the warning line was crossed. |
| about 80% | Remove stale/duplicate/recoverable optional material, then compact the oldest eligible conversation region if still needed. Keep at least the latest 10 complete turns verbatim. |
| 90-95% | Force compaction or block the request if output/headroom cannot be guaranteed. |

The exact percentages must be derived from each model's context window and
reserved output/tool headroom. They are policy defaults, not global constants.

## Why a fixed 20-turn-only prompt is insufficient

A 20-turn cap is a useful safety ceiling, but it should not be the only trigger.
Twenty one-line turns and twenty large technical reviews have radically
different token costs. Conversely, automatically summarizing turn 21 when the
active request is only 8% full loses detail for no capacity benefit.

The target design is token-aware and hybrid:

- retain up to 20 recent complete turns during normal operation;
- before 50%, allow more raw history only if the total prompt budget permits;
- after compaction, preserve at least the latest 10 complete turns verbatim;
- summarize only messages older than that protected tail;
- use relevance retrieval for project documents and long-term memory;
- replace large tool outputs with references/previews before summarizing human
  decisions;
- persist a compaction manifest with covered message IDs and verify protected
  facts before activating the summary.

## Test-plan generation: evidence before conversation

Test-plan generation uses a stricter ordering because uploaded requirements
are primary evidence:

1. Keep roughly the latest six complete user/assistant turns verbatim so user
   instructions about scope, format and exclusions are not lost.
2. If preflight pressure is high, compact Conversation first. The
   `preserve_evidence*` policies must never feed Evidence, Knowledge, Memory or
   Task State into the conversation compactor.
3. Keep a requirement verbatim when it fits the model-aware direct Evidence
   threshold. The threshold is derived from the active model window and the
   `test_plan.generate.outline` budget after reserving all non-Evidence layers
   (about 75K estimated tokens for the standard 200K generation window).
   For a larger source, split the complete parsed text into contiguous chunks,
   extract every chunk, and attach source offsets and SHA-256 references.
4. A missing extraction or incomplete coverage manifest blocks final
   generation. It must never degrade into prefix/tail truncation.
5. If the complete derived evidence plus protected layers still exceeds the
   model's Absolute guard, block the request and report the safe error. Do not
   silently discard requirement chunks.

The original uploaded file and complete parsed text remain the source of
truth. Chunk extraction is a traceable generation view, not destructive
replacement storage. Model extraction remains probabilistic, so exact source
references and a later coverage review are still required for high-stakes
acceptance.

## Industry evidence

- OpenAI's Responses API compaction is triggered when the rendered input token
  count crosses `compact_threshold`; a compaction item carries forward needed
  state in fewer tokens. This is an active-request threshold, not lifetime
  storage: <https://developers.openai.com/api/docs/guides/compaction>
- OpenAI's managed Agents/Codex architecture separates durable sessions from
  context compaction; the harness manages both instead of treating the current
  prompt as the only source of truth:
  <https://developers.openai.com/api/docs/guides/agents>
- ChatGPT documents saved memory, relevant chat-history recall, instructions
  and files as distinct context sources, and explicitly says memory does not
  retain every detail from every conversation:
  <https://help.openai.com/en/articles/8590148-memory-in-chatgpt>
- GitHub Copilot CLI documents background compaction at roughly 80%, keeps
  about 20% buffer, creates checkpoints, and explicitly notes that summaries
  cannot preserve every fine detail: <https://docs.github.com/en/copilot/concepts/agents/copilot-cli/context-management>
- LangChain documents token-count-based trimming near the model limit and
  summarizing earlier messages while retaining current thread state:
  <https://docs.langchain.com/oss/python/langchain/short-term-memory>
- Anthropic recommends explicit state/memory handoff across context windows and
  notes that starting fresh from durable project state can sometimes be better
  than repeatedly compacting summaries:
  <https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/prompt-templates-and-variables>

## Scenario acceptance semantics

`CTX-PROJECT-50` records both `tokens_before` and the selected prompt after
preflight. It accepts only a real send whose raw preflight pressure reaches at
least 45% while staying below the 50% automatic-conversation-compaction
boundary. All five categories must be non-zero, and the compaction audit must
contain no completed conversation run. `visible_percent` remains a separate UI
diagnostic; the latest persisted user goal is represented exactly once as
Current Goal and excluded from Conversation accounting by message id.
