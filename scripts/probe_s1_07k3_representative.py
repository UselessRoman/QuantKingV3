"""Print the bounded S1-07K3B representative probe plan without network access."""

from __future__ import annotations

import argparse
import json

from quantking.data.comparison.k3_probe import k3_representative_dry_run_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S1-07K3B representative four-source probe")
    parser.add_argument("--dry-run", action="store_true", required=True)
    parser.parse_args(argv)
    print(
        json.dumps(
            dict(k3_representative_dry_run_payload()),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
