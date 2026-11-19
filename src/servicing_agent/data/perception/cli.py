"""Try the Sarvam clients on a real file (needs SARVAM_API_KEY in .env).

  python -m servicing_agent.data.perception.cli receipt path/to/receipt.png
  python -m servicing_agent.data.perception.cli voice path/to/voice.ogg   # or .wav (16 kHz mono)
  python -m servicing_agent.data.perception.cli receipt receipt.png --raw  # also print Sarvam's raw response

Prints the MediaEvidence the agent would receive, and how long it took.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from servicing_agent.data.perception.receipt import RECEIPT_SCHEMA, ReceiptExtractor, map_docai_result
from servicing_agent.data.perception.sarvam import SarvamClient, SarvamConfig, SarvamError
from servicing_agent.data.perception.stt import VoiceTranscriber


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("kind", choices=["receipt", "voice"])
    p.add_argument("path", type=Path)
    p.add_argument("--raw", action="store_true", help="also print Sarvam's raw response (receipt only)")
    p.add_argument("--budget", type=float, default=30.0, help="seconds to wait for Document AI (default 30)")
    args = p.parse_args(argv)

    data = args.path.read_bytes()
    try:
        client = SarvamClient(SarvamConfig.from_env())
    except SarvamError as exc:
        print(exc, file=sys.stderr)
        return 2

    started = time.monotonic()
    if args.kind == "receipt":
        if args.raw:
            body = client.docai_extract(data, filename=args.path.name, schema=RECEIPT_SCHEMA, budget_s=args.budget)
            print(json.dumps(body, indent=2))
            evidence = map_docai_result(body)
        else:
            evidence = ReceiptExtractor(client, budget_s=args.budget).extract(data, filename=args.path.name)
    else:
        evidence = VoiceTranscriber(client).transcribe(data, already_wav=args.path.suffix.lower() == ".wav")
    elapsed = time.monotonic() - started

    print(json.dumps(evidence.model_dump(exclude_none=True), indent=2, ensure_ascii=False))
    print(f"\ntook {elapsed:.1f}s", file=sys.stderr)
    return 1 if evidence.error and not evidence.error.startswith("partial") else 0


if __name__ == "__main__":
    sys.exit(main())
