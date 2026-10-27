"""Print the retired Stage 1 representative probe dry-run (zero network).

Kept only to reproduce the historical v1 dry-run payload; the v1 marker
``K4C-REPRESENTATIVE-2026-08-14-v1`` was rejected by independent acceptance
and is retired. New probes use ``scripts/probe_s1_07k4c_r2_dry_run.py`` and
the file-backed one-shot attempt ledger. This script performs no provider
call and no data-root access.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    from quantking.history.stage1.k4c_probe import k4c_probe_dry_run_payload

    payload = dict(k4c_probe_dry_run_payload())
    sys.stdout.write(
        json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
