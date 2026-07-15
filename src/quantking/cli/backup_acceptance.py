"""Explicit CLI for the authorized S0-05 synthetic E/F acceptance run."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from quantking.jobs.backup.acceptance import run_synthetic_acceptance


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--offsite-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    print(f"authorized data root: {args.data_root}", file=sys.stderr)
    print(f"authorized offsite root: {args.offsite_root}", file=sys.stderr)
    print(f"unique acceptance run id: {args.run_id}", file=sys.stderr)
    print(
        "scope: new synthetic directories only; zero deletes, zero user-data reads, "
        "zero network/QMT",
        file=sys.stderr,
    )
    evidence = run_synthetic_acceptance(args.data_root, args.offsite_root, args.run_id)
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0
