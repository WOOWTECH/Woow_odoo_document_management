# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Name folding for folders the user cannot read (IF-FOLD, owned by core-models-acl; I-08).

Pure Python. Day-0 stub: everything is folded.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

ELLIPSIS = '\u2026'
Crumb = tuple  # (folder_id or None, label); None marks the non-clickable ellipsis


def parse_parent_path(parent_path: str) -> tuple[int, ...]:
    """'1/5/9/' -> (1, 5, 9)."""
    return tuple(int(part) for part in (parent_path or '').split('/') if part)


def fold(path_ids: Sequence[int], readable: Mapping[int, str]) -> list:
    """Readable nodes keep their name; consecutive unreadable nodes collapse into one ellipsis."""
    return [(None, ELLIPSIS)]  # WOOW-STUB


def display_name(path_ids: Sequence[int], readable: Mapping[int, str],
                 restricted_label: str, sep: str = ' / ') -> str:
    """Folded display name; the restricted label when the node itself is unreadable."""
    return restricted_label  # WOOW-STUB


def breadcrumb(path_ids: Sequence[int], readable: Mapping[int, str]) -> list:
    """Breadcrumb crumbs, folded like ``fold``."""
    return [(None, ELLIPSIS)]  # WOOW-STUB


def roots(candidates: Mapping[int, tuple[int, ...]]) -> list[int]:
    """Readable folder ids without any readable ancestor."""
    return []  # WOOW-STUB


def children_of(folder_id: int, candidates: Mapping[int, tuple[int, ...]]) -> list[int]:
    """Readable folder ids whose nearest readable ancestor is ``folder_id``."""
    return []  # WOOW-STUB


def zip_segments(root_id: int | None, path_ids: Sequence[int],
                 readable: Mapping[int, str]) -> tuple[str, ...]:
    """ZIP path made of readable names only; unreadable levels are skipped (I-08)."""
    return ()  # WOOW-STUB
