# Real Context Scenario Runners

`run_long_conversation_waterlines.ps1` performs a real authenticated normal-chat scenario. It creates an isolated conversation, sends 50 short real model turns, then accumulates later normal-chat history in telemetry-driven long-journal increments. It proves Soft / Hard / Absolute preflight actions, then asks for an early exact marker after it has aged out of the last-12-message window. The production conversation source retains 12 recent messages; therefore the runner permits a 50,000-character ceiling so the real retained history can actually reach the configured 126k / 153k / 171k token waterlines. Every message still uses the normal API and its actual measured snapshot tokens; no trigger is fabricated.

The default is diagnostic aggregation, not fail-fast: a failed Soft/Hard/Absolute oracle is recorded as `FAIL`, but the runner continues with independent later stages on the same conversation, including compaction audit and marker recall. Authentication, conversation creation, and a timed-out/500 normal-chat POST remain terminal because continuing could make the API state ambiguous. Adaptive retries are capped at five per waterline to avoid runaway token spend; the report records any stage that cannot be reached.

Run from the repository root:

```powershell
& .\backend\scripts\context_scenarios\run_long_conversation_waterlines.ps1
```

After a backend restart, run the inexpensive route probe first. It calls the
real login and normal-chat APIs once, with the configured model, and verifies
that a 12,000-character ordinary message actually reaches `chat.reply` and
creates a snapshot. Its `LCT_PARTIAL_PASS` result is deliberately only a
precondition check; it does **not** certify any waterline.

```powershell
& .\backend\scripts\context_scenarios\run_long_conversation_waterlines.ps1 -RouteProbeOnly
```

The runner prompts for credentials securely unless `PHASE2_E2E_USERNAME` and `PHASE2_E2E_PASSWORD` are already set for the process. It never writes those credentials, access tokens, request bodies, prompts, or raw model replies to its report. The evidence bundle is written under `test-results\phase2-resource-pack\<UTC>\CTX-LONG-01`.

By default the isolated conversation is deleted after each run. `-KeepArtifacts` preserves every run, which is useful while debugging but will leave many conversations. To retain only the most recent **fully passed** long-conversation run for front-end inspection, use:

```powershell
& .\backend\scripts\context_scenarios\run_long_conversation_waterlines.ps1 -KeepLatestPassedConversation
```

In that mode failed and probe-only conversations are deleted after their safe report evidence is written. On a later full pass, the runner deletes the previously retained conversation only after confirming through the owner-scoped API that it was created by this same scenario. Existing conversations retained before this option was introduced are never automatically removed.

When diagnosing a Hard-path defect, avoid replaying the 50 warmups for every repair. The first command stops after the real Hard attempt and retains exactly one scenario-owned checkpoint conversation:

```powershell
& .\backend\scripts\context_scenarios\run_long_conversation_waterlines.ps1 `
  -CheckpointAfterHardDiagnostic
```

After the code repair, retry only the retained real Hard boundary. This sends one calibrated normal-chat turn to the same owner-scoped conversation; it is `LCT_PARTIAL_PASS` on success because Absolute and marker recall are purposely not replayed in this low-cost diagnostic mode:

```powershell
& .\backend\scripts\context_scenarios\run_long_conversation_waterlines.ps1 `
  -ResumeHardCheckpoint
```

The checkpoint state contains only the scenario conversation ID, generated turn counts/payload sizes, and the same content-free per-turn evidence already in the report. It never contains credentials, request bodies, prompts, or raw model replies. A successful retry deletes that temporary conversation and checkpoint; a failed retry keeps the same single checkpoint for the next fix.

If a checkpoint retry itself does not enter `chat_reply`, it is consumed and must
not be retried again: that conversation now contains another large user turn
and is no longer a clean Hard boundary. The runner refuses a second retry and
never reuses an old snapshot for a non-chat response.

## CTX-HARD-01: low-turn automatic compaction probe

Before paying for the complete 50-turn CTX-LONG-01 acceptance journey, run this
fresh diagnostic probe after a Hard-path repair. It creates a new conversation,
calls the same authenticated normal-chat API and configured model, makes one
long-route precondition call and up to three calibrated real pressure turns.
It passes only when the request stays on `chat_reply` and an owner-scoped,
completed `trigger_type=preflight` compaction audit exists. It is intentionally
`LCT_PARTIAL_PASS`, not a substitute for Soft/Absolute/marker-recall acceptance.

```powershell
& .\backend\scripts\context_scenarios\run_long_conversation_waterlines.ps1 `
  -FreshHardProbe
```

The probe deletes its temporary conversation after writing evidence unless
`-KeepArtifacts` is explicitly supplied.

The backend must have loaded the current source because the scenario requires owner-scoped snapshot `preflight` telemetry. With `uvicorn --reload`, wait until the reload completes; no manual restart is needed for these Python-only changes. `OBSERVABILITY_GAP` means an old backend or an unavailable audit contract prevented strict verification; it is not a pass.

## CTX-PROJECT-01: project documents and long-conversation memory

`run_project_refund_long_memory.ps1` is a separate, real product-use scenario.
It creates a `退款功能上线测试` project, generates and uploads three actual DOCX
files (PRD, API specification, and UAT review minutes), waits until their
owner-scoped index records are searchable, then uses normal authenticated chat
with the configured model. It first checks document facts, records a final
user-owned UAT scope decision, conducts twelve distinct project-working turns,
and finally asks delayed questions without repeating the facts. A fresh second
conversation in the same project is used as a negative control: it must not
know the main conversation's final scope decision or acceptance owner.

Run it from the repository root:

```powershell
& .\backend\scripts\context_scenarios\run_project_refund_long_memory.ps1
```

This is a real HTTP/API/model journey, not pytest and not a database fixture.
It never stores credentials or access tokens. The report stores only test-owned
refund prompts and bounded reply excerpts together with exact missing/forbidden
fact atoms, so a failed run remains diagnosable after cleanup.

The runner waits up to 180 seconds for the three asynchronous document indexes;
override that bounded wait only when necessary:

```powershell
& .\backend\scripts\context_scenarios\run_project_refund_long_memory.ps1 -IndexWaitSeconds 300
```

Failed runs are cleaned up: their project, project conversations, source
bindings, and test-owned library files are removed. A complete `LCT_PASS`
retains exactly the newest sample project's three documents and main
conversation for front-end inspection. Before replacing it on a later pass,
the runner verifies the old project, conversation, and file IDs carry the
scenario-owned identifiers; manually created records are never targeted.

## CTX-PROJECT-50: realistic five-category pre-compaction context pressure

`run_ctx_project_50.ps1` creates and retains an inspectable
cross-border fulfilment project. It uploads ten real DOCX sources, installs
project instructions and five active user memories, and conducts up to thirty
different release-review turns through the normal chat API. Each turn is a
domain-specific dossier assembled from stakeholder interviews, baseline
integration, peak-load and recovery-drill notes rather than repeated rows.

Before the review turns, the runner completes the same test-plan workflow a
user would: it answers any bounded requirement-clarification cards (choosing
the conservative-scope option where the card permits it), then confirms the
Agent's own recommended section actions. Both decisions are recorded in the
retained scenario evidence.

Run it while the backend and its indexing dependencies are available:

```powershell
& .\backend\scripts\run_ctx_project_50.ps1
```

The wrapper first reads `PHASE2_E2E_USERNAME` and
`PHASE2_E2E_PASSWORD` from the current PowerShell session. Set both once in
that session to run without interactive credential prompts; the wrapper leaves
pre-existing values intact after the run:

```powershell
$env:PHASE2_E2E_USERNAME = "your-test-account"
$env:PHASE2_E2E_PASSWORD = "your-test-password"
& .\backend\scripts\run_ctx_project_50.ps1
```

Do not commit real credentials to this script or the repository.

The preparation line is 40%; the raw preflight observation target is 45%,
below the 50% automatic-conversation-compaction boundary:

```powershell
& .\backend\scripts\run_ctx_project_50.ps1 `
  -PreparePercent 40 `
  -TargetPercent 45
```

The report records two values for diagnosis:

- `raw_pressure_percent`: preflight `tokens_before / model window`;
- `visible_percent`: the post-prune read-only preview shown by the product.

The acceptance value is `raw_pressure_percent` from the preflight preview. The
scenario passes only when a real send reaches at least 45% while remaining below
the 50% compaction waterline, all five UI categories are non-zero, and no
automatic conversation compaction is recorded. `visible_percent` is retained
as a separate UI diagnostic.

The project, conversation, documents and scenario memories are retained when
the target is narrowly missed **and when setup, routing, indexing, compression,
or an assertion fails after the project conversation has been created**. A
failed run writes its failure type, completed turns, and collected measurements
to `project-context-pressure-partial.json`; it must not delete the evidence that
the frontend and report need for diagnosis. The latest retained state manifest is written to
`test-results/phase2-resource-pack/CTX-PROJECT-50-latest-retained.json`. Open
the retained conversation normally; no draft needs to be pasted because the
idle card recomputes the effective working set from persisted focus. A later
run replaces only a previous specimen whose
project and conversation names pass the runner's ownership checks.

Architecture rationale and the 50% recommendation are recorded in
`CONTEXT_COMPACTION_POLICY.md` beside this README.
