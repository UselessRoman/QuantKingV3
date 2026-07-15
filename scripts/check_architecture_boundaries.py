"""Enforce third-party imports at QuantKing adapter and entry boundaries."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path

_VENDOR_DATA = frozenset({"akshare", "baostock", "tushare", "xtquant"})


@dataclass(frozen=True, slots=True)
class Violation:
    path: str
    line: int
    dependency: str
    allowed: str

    def render(self) -> str:
        return (
            f"{self.path}:{self.line}: forbidden import {self.dependency} (allowed: {self.allowed})"
        )


def _starts_with(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return parts[: len(prefix)] == prefix


def _has_persistence_adapter(parts: tuple[str, ...]) -> bool:
    return any(
        parts[index : index + 2] == ("adapters", "persistence") for index in range(len(parts) - 1)
    )


def _fastapi_allowed(parts: tuple[str, ...]) -> bool:
    if not parts or parts[0] != "web":
        return False
    relative = "/".join(parts)
    return (
        relative
        in {
            "web/adapters/compatibility.py",
            "web/api.py",
            "web/app.py",
            "web/main.py",
            "web/routes.py",
            "web/schemas.py",
        }
        or _starts_with(parts, ("web", "api"))
        or _starts_with(parts, ("web", "schemas"))
    )


def _request_allowed(parts: tuple[str, ...]) -> bool:
    if not parts or parts[0] != "web":
        return False
    relative = "/".join(parts)
    return relative in {
        "web/api.py",
        "web/app.py",
        "web/main.py",
        "web/routes.py",
    } or _starts_with(parts, ("web", "api"))


def _allowed(top_level: str, parts: tuple[str, ...]) -> tuple[bool, str] | None:
    if top_level in _VENDOR_DATA:
        allowed = _starts_with(parts, ("data", "adapters")) or _starts_with(
            parts, ("trading", "adapters")
        )
        return allowed, "data/adapters or trading/adapters"
    if top_level == "qlib":
        allowed = _starts_with(parts, ("research", "adapters")) or _starts_with(
            parts, ("backtest", "adapters")
        )
        return allowed, "research/adapters or backtest/adapters"
    if top_level == "lightgbm":
        return _starts_with(parts, ("research", "adapters")), "research/adapters"
    if top_level == "backtrader":
        return _starts_with(parts, ("backtest", "adapters")), "backtest/adapters"
    if top_level in {"alembic", "sqlalchemy"}:
        return _has_persistence_adapter(parts), "*/adapters/persistence"
    if top_level in {"duckdb", "pyarrow"}:
        return _starts_with(parts, ("data", "adapters")), "data/adapters"
    if top_level == "fastapi":
        return _fastapi_allowed(parts), "web entry or schema modules"
    return None


def _imports(tree: ast.AST) -> list[tuple[int, str, bool]]:
    imports: list[tuple[int, str, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(
                (node.lineno, alias.name, alias.name == "fastapi.requests") for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            request = node.module == "fastapi.requests" or (
                node.module == "fastapi" and any(alias.name == "Request" for alias in node.names)
            )
            imports.append((node.lineno, node.module, request))
    return imports


def check_boundaries(root: Path) -> list[Violation]:
    """Return all architecture violations below ``root/src/quantking``."""
    source_root = root / "src" / "quantking"
    violations: list[Violation] = []
    for path in sorted(source_root.rglob("*.py"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(source_root)
        parts = relative.parts
        display = (Path("src") / "quantking" / relative).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=display)
        except (OSError, SyntaxError) as error:
            line = error.lineno if isinstance(error, SyntaxError) and error.lineno else 1
            violations.append(Violation(display, line, "<unparseable>", "valid Python syntax"))
            continue

        for line, module, imports_request in _imports(tree):
            if (
                parts
                and parts[0] == "web"
                and module.startswith("quantking.jobs.adapters.persistence")
            ):
                violations.append(
                    Violation(
                        display,
                        line,
                        "jobs.persistence",
                        "injected Web application port",
                    )
                )
                continue
            top_level = module.split(".", maxsplit=1)[0]
            if top_level == "fastapi" and imports_request:
                if not _request_allowed(parts):
                    violations.append(
                        Violation(display, line, "fastapi.Request", "explicit web entry modules")
                    )
                continue
            decision = _allowed(top_level, parts)
            if decision is not None and not decision[0]:
                violations.append(Violation(display, line, top_level, decision[1]))

    return sorted(violations, key=lambda issue: issue.render().casefold())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    violations = check_boundaries(args.root.resolve())
    if violations:
        for violation in violations:
            print(violation.render())
        return 1
    print("architecture boundaries: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
