"""Print the frozen S1-07K4C-R3 supplemental dry-run (zero network).

The R3 dry-run freezes exactly three user-accepted supplemental samples
(suspension, low-liquidity, legal pre-listing empty upstream), each bound to
an exact symbol, trade date, minimal window and an independent reference
artifact SHA. It references the immutable R2 component by SHA only and never
repeats R2's 36 invocations.

``--frozen`` prints the dry-run for the user-accepted frozen samples without
any transcription step. A ``--samples`` JSON file is accepted for contract
inspection only and must reproduce the same three samples exactly. Without
either, this script prints the stable blocker payload and exits non-zero
instead of fabricating samples. Real execution requires a separate, fresh
authorization (Phase B) and is never performed here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BLOCKER_SCHEMA = "s1-07k4c-r3-sample-selection-blocker.v1"


def _load_samples(path: str) -> list[dict[str, object]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("samples"), list):
        raise ValueError("samples file must carry a 'samples' list")
    return list(payload["samples"])


def _blocker(status: str, message: str, extra: dict[str, object] | None = None) -> str:
    payload: dict[str, object] = {
        "schema_version": BLOCKER_SCHEMA,
        "status": status,
        "message": message,
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"


def _sample_from_dict(item: dict[str, object]) -> dict[str, object]:
    from quantking.data.comparison.k4c_evidence import K4CR3SampleReference

    def reference(value: object) -> K4CR3SampleReference | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("listing/calendar reference must be an object")
        return K4CR3SampleReference(
            source=str(value["source"]),
            version_or_file=str(value["version_or_file"]),
            sha256=str(value["sha256"]),
        )

    result = {
        "objective": str(item["objective"]),
        "symbol": str(item["symbol"]),
        "trade_date": str(item["trade_date"]),
        "window_days": int(item["window_days"]),
        "exchange": str(item["exchange"]),
        "expected_roles": {str(key): str(value) for key, value in item["expected_roles"].items()},
        "reference": reference(item["reference"]),
        "success_criterion": str(item["success_criterion"]),
        "failure_reason_code": str(item["failure_reason_code"]),
        "missing_explanation": (
            None if item.get("missing_explanation") is None else str(item["missing_explanation"])
        ),
        "suspension_evidence_kind": (
            None
            if item.get("suspension_evidence_kind") is None
            else str(item["suspension_evidence_kind"])
        ),
        "listing_date": (None if item.get("listing_date") is None else str(item["listing_date"])),
        "listing_reference": reference(item.get("listing_reference")),
        "calendar_reference": reference(item.get("calendar_reference")),
    }
    if result["reference"] is None:
        raise ValueError("sample reference is required")
    return result


def main(argv: list[str] | None = None) -> int:
    from quantking.data.comparison.k4c_evidence import (
        BLOCKED_SAMPLE_SELECTION,
        K4CR3ObjectiveSample,
        k4c_r3_dry_run_payload,
        k4c_r3_frozen_samples,
        validate_k4c_r3_sample,
    )

    parser = argparse.ArgumentParser(description="S1-07K4C-R3 zero-network dry-run")
    parser.add_argument("--frozen", action="store_true", help="use the frozen accepted samples")
    parser.add_argument("--samples", help="JSON file carrying the three frozen samples")
    arguments = parser.parse_args(argv)
    if not arguments.frozen and not arguments.samples:
        sys.stdout.write(
            _blocker(
                BLOCKED_SAMPLE_SELECTION,
                "R3 sample selection is blocked: no local material can fix exact "
                "symbols/dates for suspension, low-liquidity and legal pre-listing "
                "empty upstream. Use --frozen for the user-accepted samples or "
                "request a bounded read-only sample-selection authorization.",
            )
        )
        return 1
    try:
        if arguments.frozen:
            samples = tuple(k4c_r3_frozen_samples())
        else:
            raw_samples = _load_samples(arguments.samples)
            samples = tuple(K4CR3ObjectiveSample(**_sample_from_dict(item)) for item in raw_samples)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        sys.stdout.write(_blocker("R3_SAMPLE_FILE_INVALID", type(error).__name__))
        return 2
    reasons: list[str] = []
    for sample in samples:
        reasons.extend(validate_k4c_r3_sample(sample))
    if reasons:
        sys.stdout.write(
            _blocker(
                "R3_SAMPLE_INVALID",
                "sample contract validation failed",
                {
                    "reasons": sorted(set(reasons)),
                },
            )
        )
        return 2
    payload = dict(k4c_r3_dry_run_payload(samples))
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
