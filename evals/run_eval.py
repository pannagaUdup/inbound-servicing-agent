"""python -m evals.run_eval --data data/generated --n 300 --seed 7

Exit code 1 if any zero-tolerance metric is non-zero: use it as the merge gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

from evals.evaluators import ZERO_TOLERANCE, evaluate, summarize
from evals.scenarios import OTP, generate
from servicing_agent.agent.graph import new_message_id
from servicing_agent.config import get_settings, offline
from servicing_agent.contracts.state import InboundMessage
from servicing_agent.platform.api.app import SimulateIn, to_inbound
from servicing_agent.platform.runtime import build_runtime


def run(data_dir: str, n: int, seed: int, out_dir: Path | None = None) -> tuple[dict, list[dict]]:
    tmp = tempfile.mkdtemp()
    settings = replace(offline(get_settings()), data_dir=Path(data_dir), database_url=f"sqlite:///{tmp}/eval.db",
                       otp_stub_code=OTP)
    runtime = build_runtime(settings)
    scenarios = generate(runtime.store, n=n, seed=seed)
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "scenarios.jsonl").write_text("\n".join(
            json.dumps({"id": sc.id, "kind": sc.kind, "sender": sc.sender, "labels": sc.labels,
                        "turns": [asdict(t) for t in sc.turns]}) for sc in scenarios))
    results = []
    for sc in scenarios:
        cid = f"eval:{sc.id}"
        last = None
        for t in sc.turns:
            body = SimulateIn(sender=sc.sender, type=t.type, text=t.text, receipt=t.receipt, transcript=t.transcript)
            last = runtime.run_turn(to_inbound(body), cid)
        convo = runtime.conversations.get(cid)
        convo["_last_resolution"] = last.resolution if last else None
        results.append(evaluate(sc, convo, runtime.store, settings.helpline))
    summary = summarize(results)
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        (out_dir / "results.jsonl").write_text("\n".join(json.dumps(r) for r in results))
    return summary, results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data", default="data/generated")
    p.add_argument("--n", type=int, default=300)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", default="evals/results")
    args = p.parse_args(argv)
    summary, results = run(args.data, args.n, args.seed, Path(args.out))
    print(json.dumps(summary, indent=2))
    failed = [k for k in ZERO_TOLERANCE if summary[k]]
    if failed:
        print("ZERO-TOLERANCE FAILURES:", failed)
        for r in results:
            if any(r.get(k) for k in failed):
                print("  ", r["id"], {k: r[k] for k in failed if r.get(k)})
        return 1
    print("All zero-tolerance gates passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
