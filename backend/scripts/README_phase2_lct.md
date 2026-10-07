# Phase 2 resource-pack LCT runners

Run one LCT from the repository root:

```powershell
& .\backend\scripts\run_lct01.ps1
& .\backend\scripts\run_lct02.ps1
& .\backend\scripts\run_lct03.ps1
& .\backend\scripts\run_lct04.ps1
& .\backend\scripts\run_lct05.ps1
& .\backend\scripts\run_lct06.ps1
```

These commands invoke newly authored standalone resource-pack scenarios. They
do **not** start `pytest` or select files from `backend/tests`.

Each run writes a self-contained bundle under:

`test-results/phase2-resource-pack/<UTC timestamp>/<LCT>/`

- `report.json`: verdict, test status, execution classification, branch/head,
  failed step, inputs, expected/actual values and evidence paths;
- `steps.json`: every test step and its status;
- `execution.log`: concise scenario event log;
- `evidence/*.json`: state snapshots and fault matrices;
- `evidence/*-failure.txt`: assertion or exception trace when a step fails.

The final one-line JSON printed by the command contains both the LCT verdict
and the exact `report.json` path. Send that entire LCT result directory back
for diagnosis.

Verdicts distinguish executable test success from product acceptance:

- `TEST_PASS` / `TEST_FAIL`: this standalone script itself passed or failed;
- `AUTOMATED_ONLY`: safe local scenario evidence only, not a live product E2E;
- `NOT_IMPLEMENTED`: the resource pack identifies a missing product wiring;
- `LCT_PASS`, `LCT_PARTIAL_PASS`, `OBSERVABILITY_GAP`, and
  `PHASE2_PARTIAL_ACCEPTED` / `PHASE2_ACCEPTED` are acceptance-level labels,
  never silently substituted by a generic `PASS`.

LCT-06 intentionally returns `TEST_PASS` together with `NOT_IMPLEMENTED` when
its internal components succeed: production Agent graph automatic loop
compaction is not currently wired, so it cannot certify that product path.
