# Phase 2 live API evidence runner

Run against the locally started backend:

```powershell
& .\backend\scripts\run_phase2_live_e2e.ps1
```

The script securely prompts for the account and password if the process does
not already have `PHASE2_E2E_USERNAME` / `PHASE2_E2E_PASSWORD`. It never writes
credentials, JWTs, or cookies to source code or result files.

Default coverage is real HTTP API evidence for LCT-03 lifecycle, LCT-04
workspace records, and the owner-scoped snapshot/retrieval/compaction audit
endpoints. It creates unique records; workspace test records and their test
conversations are deleted during cleanup. The LCT-03 `forget` tombstone remains
because that is the product's intended lifecycle state.

For manual compaction evidence, use a dedicated pre-populated test conversation
and explicitly opt in (this can invoke the configured model):

```powershell
& .\backend\scripts\run_phase2_live_e2e.ps1 `
  -EnableLiveCompaction -ConversationId '<dedicated-test-conversation-id>'
```

Or let the runner create, seed, and clean up a dedicated conversation. This
also makes provider/model calls, so it remains an explicit opt-in:

```powershell
& .\backend\scripts\run_phase2_live_e2e.ps1 `
  -EnableLiveCompaction -SeedLiveConversation
```

Outputs are written under `test-results/phase2-resource-pack/<UTC>/LIVE-E2E/`.
The default expected verdict is `LCT_PARTIAL_PASS`: public APIs cannot expose
all required prompt/evidence details, and LCT-01/02 live compaction is opt-in.
