"""Replaceable filesystem boundaries for S0-05 services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StableCopyResult:
    size: int
    sha256: str


class DiskSpacePort(Protocol):
    def free_bytes(self, path: Path) -> int: ...


class StableFileCopierPort(Protocol):
    def copy_stable(self, source: Path, target: Path) -> StableCopyResult: ...


class SQLiteBackupPort(Protocol):
    def backup(self, source: Path, target: Path) -> None: ...
