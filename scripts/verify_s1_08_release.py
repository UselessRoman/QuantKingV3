"""Read-only S1-08 fixed release verifier entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from quantking.common.config import load_settings
from quantking.common.errors import ConfigurationError
from quantking.data.adapters.release_verifier import verify_s108_release

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "default.toml"
_FAILURE = {
    "error_code": "S1_08_RELEASE_VERIFICATION_FAILED",
    "message": "S1-08 release verification failed.",
    "status": "FAILED",
}


def _canonical(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify one explicit S1-08 release read-only")
    parser.add_argument("--version-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(_DEFAULT_CONFIG_PATH)
        result = verify_s108_release(
            data_root=settings.paths.data_root,
            version_id=args.version_id,
        )
    except (ConfigurationError, OSError, ValueError):
        print(_canonical(_FAILURE))
        return 2
    print(_canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
