"""Print the frozen Stage 1 representative probe dry-run (zero network).

The R2 dry-run freezes the new authorization marker, the plan hash, the
one-shot attempt rules, the executable objective criteria and the network
budget. This script performs no provider call and no data-root access; real
execution requires a separate, fresh authorization (a new authorization_id
under the frozen R2 marker, accepted by the file-backed attempt ledger).
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    from quantking.history.stage1.k4c_evidence import k4c_r2_dry_run_payload

    payload = dict(k4c_r2_dry_run_payload())
    sys.stdout.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
