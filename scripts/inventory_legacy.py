"""Generate the Stage 0 S0-06 read-only legacy candidate inventory."""

from __future__ import annotations

import argparse
from pathlib import Path

from quantking.common.legacy_inventory import audit_tree, write_reports


def _parser() -> argparse.ArgumentParser:
    repository_root = Path(__file__).resolve().parents[1]
    evidence_root = repository_root / "docs" / "stage0" / "evidence"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(r"E:\QuantKing"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--hash-small-code",
        action="store_true",
        help="opt in to hashing small code; leave disabled for metadata-only audits",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=evidence_root / "legacy_inventory.json",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=evidence_root / "legacy_inventory.md",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Audit a source tree; only a non-dry run writes reports inside this repository."""

    args = _parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    try:
        report = audit_tree(
            args.root,
            source_label="E:/QuantKing (legacy project)",
            hash_small_code=args.hash_small_code,
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(f"legacy inventory failed: {error}")
        return 2

    print(
        "legacy inventory: "
        f"files={report.summary.file_count} "
        f"bytes={report.summary.total_bytes} "
        f"items={report.summary.item_count} "
        f"skipped={report.summary.skipped_count} "
        f"hashed={report.hashed_file_count} "
        f"source_unchanged={str(report.source_unchanged).lower()}"
    )
    print(f"legacy fingerprint: {report.before_fingerprint.digest_sha256}")
    if not report.source_unchanged:
        print("legacy inventory failed: source fingerprint changed during the read-only audit")
        return 3
    if args.dry_run:
        print("dry-run: no report files written")
        return 0

    try:
        write_reports(
            report,
            json_path=args.json,
            markdown_path=args.markdown,
            allowed_output_root=repository_root,
            source_root=args.root,
        )
    except (OSError, ValueError) as error:
        print(f"legacy inventory report write failed: {error}")
        return 4
    print(f"json report: {args.json.resolve(strict=False)}")
    print(f"markdown report: {args.markdown.resolve(strict=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
