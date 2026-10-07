from __future__ import annotations

import json
import zipfile
from pathlib import Path

from scripts.export_incident_bundle import export_bundle


def test_incident_bundle_is_sanitized_and_contains_required_files(tmp_path: Path):
    log_dir = tmp_path / "logs" / "app"
    log_dir.mkdir(parents=True)
    (log_dir / "events.jsonl").write_text(
        json.dumps({
            "event": "llm.request.failed", "level": "ERROR", "task_id": "task_1",
            "trace_id": "a" * 32, "error_fingerprint": "fingerprint", "prompt": "PROMPT_SECRET_SENTINEL",
            "authorization": "Bearer JWT_SECRET_SENTINEL", "snapshot_id": "snapshot_1",
        }) + "\n",
        encoding="utf-8",
    )
    output = export_bundle(log_dir=tmp_path / "logs", output=tmp_path / "incident.zip", filters={"task_id": "task_1"})
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"manifest.json", "timeline.jsonl", "errors.json", "references.json"}
        blob = "".join(archive.read(name).decode("utf-8") for name in archive.namelist())
    assert "PROMPT_SECRET_SENTINEL" not in blob
    assert "JWT_SECRET_SENTINEL" not in blob
    assert "fingerprint" in blob
