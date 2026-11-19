import json
import os

import pytest

SAMPLE_ACCOUNT = "CN7907799469"


def test_sample_batches_replay_cleanly(tmp_path, monkeypatch):
    """The committed sample batch file must keep passing (it uses the committed synthetic data)."""
    from servicing_agent.config import REPO_ROOT
    from evals.replay_batches import run
    if not (REPO_ROOT / "data/generated/accounts.csv").read_text().count(SAMPLE_ACCOUNT):
        pytest.skip("sample account not in the current generated data")
    monkeypatch.setenv("SA_PERCEPTION", "stub")
    monkeypatch.setenv("SA_NORMALIZER", "off")          # regional text is covered by regional.json
    monkeypatch.setenv("SA_DATA_DIR", str(REPO_ROOT / "data/generated"))
    assert run(REPO_ROOT / "evals/batches/sample.json", out_dir=tmp_path) == 0

    rows = [json.loads(l) for l in next(tmp_path.glob("*.jsonl")).read_text().splitlines()]
    assert all(r["pass"] for r in rows)
    first = rows[0]
    assert first["normalized"]["message_count"] == 3
    assert first["normalized"]["tasks"][0]["slots"]["utr"].startswith("••••")   # masked in the log
    assert "371413463057" not in json.dumps(first["normalized"])
