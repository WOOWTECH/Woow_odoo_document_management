# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""ZIP building (IF-ZIP, owned by internal-routes-zip).

Pure Python. Day-0 stub: data classes, the exception and ``limits_from_settings`` are final;
``build_zip`` is a stub.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from typing import BinaryIO

from .constants import MIB, ZIP_DEADLINE_S


@dataclasses.dataclass(frozen=True)
class ZipEntry:
    path_segments: tuple[str, ...]          # already passed through sanitize_segment
    open_stream: Callable[[], BinaryIO]
    size: int
    compressible: bool


@dataclasses.dataclass(frozen=True)
class ZipLimits:
    max_files: int
    max_bytes: int
    max_depth: int
    deadline_s: int = ZIP_DEADLINE_S


@dataclasses.dataclass(frozen=True)
class ZipStats:
    files: int
    bytes: int
    links: int


class ZipLimitExceeded(Exception):
    """A ZIP limit was hit; ``code`` is one of files, bytes, depth, deadline."""

    CODES = ('files', 'bytes', 'depth', 'deadline')

    def __init__(self, code: str):
        if code not in self.CODES:
            raise ValueError('unknown zip limit code')
        super().__init__(code)
        self.code = code


def limits_from_settings(settings) -> ZipLimits:
    """ZipLimits from a WoowDocSettings instance."""
    return ZipLimits(
        max_files=settings.zip_max_files,
        max_bytes=settings.zip_max_mb * MIB,
        max_depth=settings.zip_max_depth,
    )


def build_zip(entries: Sequence[ZipEntry], limits: ZipLimits, fileobj: BinaryIO,
              links: Sequence[str] = ()) -> ZipStats:
    """Write the archive into ``fileobj`` after checking every limit before reading bytes."""
    raise NotImplementedError  # WOOW-STUB
