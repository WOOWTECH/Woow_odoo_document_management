# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Bounded, streaming ZIP construction for files the caller has authorized."""
from __future__ import annotations

import dataclasses
import os
import time
import zipfile
from collections.abc import Callable, Sequence
from contextlib import closing
from pathlib import Path
from typing import BinaryIO

from odoo.tools import config

from .constants import MIB, ZIP_DEADLINE_S
from .naming import Deduper


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


def filestore_stream(path: str) -> BinaryIO:
    """Open only an Odoo filestore file, refusing links and paths outside that tree."""
    root = (Path(config['data_dir']) / 'filestore').resolve(strict=True)
    candidate = Path(path).resolve(strict=True)
    if not candidate.is_relative_to(root):
        raise ValueError('file is outside the filestore')
    descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    return os.fdopen(descriptor, 'rb')


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
    links_payload = ('\n'.join(links) + '\n').encode('utf-8') if links else b''
    if any('\r' in link or '\n' in link for link in links):
        raise ValueError('invalid link')
    file_count = len(entries) + bool(links)
    announced_bytes = sum(entry.size for entry in entries) + len(links_payload)
    if file_count > limits.max_files:
        raise ZipLimitExceeded('files')
    if any(not entry.path_segments or len(entry.path_segments) > limits.max_depth or
           any(not part or part in ('.', '..') or '/' in part or '\\' in part
               for part in entry.path_segments) for entry in entries):
        raise ZipLimitExceeded('depth')
    if any(entry.size < 0 for entry in entries) or announced_bytes > limits.max_bytes:
        raise ZipLimitExceeded('bytes')
    if links and limits.max_depth < 1:
        raise ZipLimitExceeded('depth')

    deadline = time.monotonic() + limits.deadline_s
    seen: dict[tuple[str, ...], Deduper] = {}
    if links:
        seen[()] = Deduper(['_links.txt'])
    actual_bytes = len(links_payload)
    with zipfile.ZipFile(fileobj, mode='w', allowZip64=True,
                         compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for entry in entries:
            if time.monotonic() > deadline:
                raise ZipLimitExceeded('deadline')
            parent, leaf = entry.path_segments[:-1], entry.path_segments[-1]
            unique_leaf = seen.setdefault(parent, Deduper()).add(leaf)
            arcname = '/'.join((*parent, unique_leaf))
            info = zipfile.ZipInfo(arcname)
            info.compress_type = (zipfile.ZIP_DEFLATED if entry.compressible
                                  else zipfile.ZIP_STORED)
            with closing(entry.open_stream()) as source, archive.open(
                    info, mode='w', force_zip64=True) as target:
                read_bytes = 0
                while True:
                    if time.monotonic() > deadline:
                        raise ZipLimitExceeded('deadline')
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    read_bytes += len(chunk)
                    actual_bytes += len(chunk)
                    if read_bytes > entry.size or actual_bytes > limits.max_bytes:
                        raise ZipLimitExceeded('bytes')
                    target.write(chunk)
        if links:
            if time.monotonic() > deadline:
                raise ZipLimitExceeded('deadline')
            archive.writestr('_links.txt', links_payload)
    return ZipStats(files=file_count, bytes=actual_bytes, links=len(links))
