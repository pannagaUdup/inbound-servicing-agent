"""python -m servicing_agent.data.generators.cli --out data/generated --accounts 10000 --seed 42"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from servicing_agent.data.generators.core import generate, validate, write_csvs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate synthetic D1/D2/D7 data")
    p.add_argument("--out", default="data/generated")
    p.add_argument("--accounts", type=int, default=10_000)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args(argv)

    ds = generate(args.accounts, args.seed)
    problems = validate(ds)
    if problems:
        print("validation failed:", *problems[:20], sep="\n  ")
        return 1
    counts = write_csvs(ds, Path(args.out))
    for name, n in counts.items():
        print(f"{name:18s} {n:>8,d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
