"""Replay batches of borrower messages through the agent and log what it understood and did.

    .venv/bin/python -m evals.replay_batches evals/batches/sample.json
    .venv/bin/python -m evals.replay_batches my_batches.json --use-env-db     # write to SA_DATABASE_URL
    SA_PERCEPTION=sarvam .venv/bin/python -m evals.replay_batches real.json   # real files via Sarvam

Each turn is sent to /v1/simulate/turn (one batch = one turn). For every turn it prints the
normalized input (merged text, media, tasks), the verdicts, tickets and replies, and writes the
same to a JSONL log under evals/runs/ (git-ignored: logs can contain borrower text).

Batch file format:

    {"conversations": [
      {"name": "...", "sender": "918765026957", "verify_account": "CN7907799469",
       "turns": [
         {"name": "...", "expect": ["claim_matched"],
          "messages": [
            {"text": "already paid"},
            {"type": "image", "receipt": {"utr": "...", "amount": 1.0, "txn_date": "2026-06-03"}},
            {"type": "image", "file": "../../test-inputs/receipt.png"},          # real file (sarvam mode)
            {"type": "voice", "transcript": "I paid ten thousand"}               # stub voice
          ]}]}]}

``verify_account`` runs hi -> account number -> OTP first. ``expect`` lists the reply template
ids in order; a mismatch is reported and makes the exit code 1.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import tempfile
import time
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from servicing_agent.config import REPO_ROOT, get_settings
from servicing_agent.platform.api.app import create_app


def _load_messages(turn: dict, sender: str, base: Path) -> list[dict]:
    out = []
    for m in turn["messages"]:
        m = dict(m)
        m.setdefault("sender", sender)
        if "file" in m:
            path = (base / m.pop("file")).resolve()
            m["media_base64"] = base64.b64encode(path.read_bytes()).decode()
            m.setdefault("media_filename", path.name)
        out.append(m)
    return out


def _summary(res: dict) -> dict:
    events = res.get("events", [])
    return {
        "normalized": res.get("normalized"),
        "verdicts": [e["data"]["verdict"] for e in events if e["type"] == "payment_verdict"],
        "tickets": [{"ticket_id": t["ticket_id"], "queue": t["queue"], "priority": t["priority"]}
                    for t in res.get("tickets", [])],
        "replies": [{"template_id": r["template_id"], "text": r["text"]} for r in res.get("replies", [])],
        "intents": res.get("intents", []),
        "resolution": res.get("resolution"),
        "verified": res.get("verified"),
        "duplicate": res.get("duplicate"),
    }


def _print_turn(conv: str, turn: str, s: dict, elapsed_ms: int, ok: bool | None) -> None:
    n = s["normalized"] or {}
    status = "" if ok is None else ("  PASS" if ok else "  FAIL")
    print(f"\n── {conv} › {turn}  ({elapsed_ms} ms){status}")
    print(f"   messages : {n.get('message_count')}  verified: {n.get('verified_before')} → {n.get('verified_after')}")
    print(f"   text     : {n.get('merged_text')!r}" + ("   [injection removed]" if n.get("injection_removed") else ""))
    for m in n.get("media", []):
        bits = [m["kind"], m.get("language"), m.get("utr"), m.get("amount"), m.get("txn_date")]
        extra = f"  error={m['error']}" if m.get("error") else ""
        low = f"  low={m['low_confidence_fields']}" if m.get("low_confidence_fields") else ""
        tr = f"  transcript={m['transcript']!r}" if m.get("transcript") else ""
        print(f"   media    : {' · '.join(str(b) for b in bits if b not in (None, ''))}{tr}{low}{extra}")
    for t in n.get("tasks", []):
        print(f"   task     : {t['intent']} {t['slots'] or ''}")
    if s["verdicts"]:
        print(f"   verdicts : {', '.join(s['verdicts'])}")
    for t in s["tickets"]:
        print(f"   ticket   : {t['ticket_id']} → {t['queue']} ({t['priority']})")
    for r in s["replies"]:
        print(f"   reply    : [{r['template_id']}] {r['text']}")


def run(path: Path, use_env_db: bool = False, out_dir: Path | None = None) -> int:
    spec = json.loads(path.read_text(encoding="utf-8"))
    settings = get_settings()
    if not use_env_db:
        settings = replace(settings, database_url=f"sqlite:///{tempfile.mkdtemp()}/replay.db")
    client = TestClient(create_app(settings))
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = out_dir or REPO_ROOT / "evals/runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"replay-{path.stem}-{run_id}.jsonl"
    print(f"Replaying {path} · perception={settings.perception} · db={settings.database_url.split('@')[-1]}")

    failures = 0
    with open(log_path, "w", encoding="utf-8") as log:
        for conv in spec["conversations"]:
            sender, name = conv["sender"], conv["name"]
            cid = f"replay:{run_id}:{uuid.uuid4().hex[:6]}"
            if conv.get("verify_account"):
                for text in ("hi", conv["verify_account"], conv.get("otp", "123456")):
                    client.post("/v1/simulate/messages", json={"sender": sender, "text": text, "conversation_id": cid})
            for turn in conv["turns"]:
                messages = _load_messages(turn, sender, path.parent)
                t0 = time.monotonic()
                resp = client.post("/v1/simulate/turn", json={"messages": messages, "conversation_id": cid})
                elapsed = int((time.monotonic() - t0) * 1000)
                if resp.status_code != 200:
                    failures += 1
                    print(f"\n── {name} › {turn['name']}  HTTP {resp.status_code}: {resp.text[:300]}")
                    log.write(json.dumps({"conversation": name, "turn": turn["name"], "http": resp.status_code,
                                          "error": resp.text[:1000]}) + "\n")
                    continue
                s = _summary(resp.json())
                ok = None
                if "expect" in turn:
                    ok = [r["template_id"] for r in s["replies"]] == turn["expect"]
                    failures += 0 if ok else 1
                _print_turn(name, turn["name"], s, elapsed, ok)
                if ok is False:
                    print(f"   expected : {turn['expect']}")
                request = [{k: v for k, v in m.items() if k != "media_base64"} for m in messages]
                log.write(json.dumps({"run_id": run_id, "conversation": name, "turn": turn["name"],
                                      "request": request, "elapsed_ms": elapsed, "expect": turn.get("expect"),
                                      "pass": ok, **s}, ensure_ascii=False) + "\n")
    print(f"\nLog: {log_path}")
    print("All expectations met." if not failures else f"{failures} turn(s) failed.")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("file", type=Path)
    p.add_argument("--use-env-db", action="store_true",
                   help="write to SA_DATABASE_URL (default: a throwaway SQLite file)")
    p.add_argument("--out", type=Path, help="log directory (default evals/runs/)")
    args = p.parse_args(argv)
    return run(args.file, args.use_env_db, args.out)


if __name__ == "__main__":
    sys.exit(main())
