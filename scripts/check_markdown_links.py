"""Check local Markdown links in root documents and the docs tree."""

from __future__ import annotations

import argparse
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")
_IGNORED_SCHEMES = frozenset({"http", "https", "mailto"})


@dataclass(frozen=True, slots=True)
class LinkIssue:
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _outside(candidate: Path, root: Path) -> bool:
    normalized_candidate = os.path.normcase(str(candidate))
    normalized_root = os.path.normcase(str(root))
    try:
        return os.path.commonpath([normalized_candidate, normalized_root]) != normalized_root
    except ValueError:
        return True


def _visible_lines(path: Path) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    fence: str | None = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = _FENCE.match(line)
        if match:
            marker = match.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is None:
            lines.append((number, line))
    return lines


def _slug(text: str) -> str:
    without_closing_hashes = re.sub(r"\s+#+\s*$", "", text.strip().lower())
    without_markup = re.sub(r"<[^>]+>", "", without_closing_hashes)
    without_punctuation = re.sub(r"[^\w\-\s]", "", without_markup, flags=re.UNICODE)
    return re.sub(r"[\s]+", "-", without_punctuation).strip("-")


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: defaultdict[str, int] = defaultdict(int)
    for _, line in _visible_lines(path):
        match = _HEADING.match(line)
        if not match:
            continue
        base = _slug(match.group(1))
        count = counts[base]
        anchor = base if count == 0 else f"{base}-{count}"
        counts[base] += 1
        anchors.add(anchor)
    return anchors


def _documents(root: Path) -> list[Path]:
    documents = list(root.glob("*.md"))
    docs_root = root / "docs"
    if docs_root.is_dir():
        documents.extend(docs_root.rglob("*.md"))
    return sorted(set(documents), key=lambda path: path.as_posix().casefold())


def check_links(root: Path) -> list[LinkIssue]:
    """Return local-link failures for repository root and docs Markdown."""
    root = root.resolve()
    issues: list[LinkIssue] = []
    anchor_cache: dict[Path, set[str]] = {}
    for source in _documents(root):
        display = source.relative_to(root).as_posix()
        for line_number, line in _visible_lines(source):
            for match in _LINK.finditer(line):
                raw_target = match.group(1).strip("<>")
                parsed = urlsplit(raw_target)
                if parsed.scheme.casefold() in _IGNORED_SCHEMES:
                    continue
                decoded_path = unquote(parsed.path)
                fragment = unquote(parsed.fragment)
                if decoded_path.startswith("/"):
                    candidate = root / decoded_path.lstrip("/")
                elif decoded_path:
                    candidate = source.parent / decoded_path
                else:
                    candidate = source
                resolved = candidate.resolve(strict=False)
                if _outside(resolved, root):
                    issues.append(
                        LinkIssue(
                            display,
                            line_number,
                            f"link target escapes repository root: {raw_target}",
                        )
                    )
                    continue
                if resolved.is_dir():
                    resolved = resolved / "README.md"
                if not resolved.is_file():
                    issues.append(LinkIssue(display, line_number, f"missing target: {raw_target}"))
                    continue
                if fragment and resolved.suffix.casefold() == ".md":
                    anchors = anchor_cache.setdefault(resolved, _anchors(resolved))
                    if fragment.casefold() not in anchors:
                        issues.append(
                            LinkIssue(
                                display,
                                line_number,
                                f"missing anchor #{fragment}: {raw_target}",
                            )
                        )
    return sorted(issues, key=lambda issue: issue.render().casefold())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    issues = check_links(args.root)
    if issues:
        for issue in issues:
            print(issue.render())
        return 1
    print("markdown links: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
