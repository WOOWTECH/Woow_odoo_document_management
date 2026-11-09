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
    crumbs = []
    hidden = False
    for folder_id in path_ids:
        if folder_id not in readable:
            if not hidden:
                crumbs.append((None, ELLIPSIS))
                hidden = True
        else:
            crumbs.append((folder_id, readable[folder_id]))
            hidden = False
    return crumbs


def display_name(path_ids: Sequence[int], readable: Mapping[int, str],
                 restricted_label: str, sep: str = ' / ') -> str:
    """Folded display name; the restricted label when the node itself is unreadable."""
    if not path_ids or path_ids[-1] not in readable:
        return restricted_label
    return sep.join(label for _, label in fold(path_ids, readable))


def breadcrumb(path_ids: Sequence[int], readable: Mapping[int, str]) -> list:
    """Breadcrumb crumbs, folded like ``fold``."""
    return fold(path_ids, readable)


def roots(candidates: Mapping[int, tuple[int, ...]]) -> list[int]:
    """Readable folder ids without any readable ancestor."""
    readable = set(candidates)
    return [folder_id for folder_id, ancestors in candidates.items()
            if not (readable - {folder_id}).intersection(ancestors)]


def children_of(folder_id: int, candidates: Mapping[int, tuple[int, ...]]) -> list[int]:
    """Readable folder ids whose nearest readable ancestor is ``folder_id``."""
    readable = set(candidates)
    return [candidate_id for candidate_id, ancestors in candidates.items()
            if candidate_id != folder_id and folder_id in ancestors
            and next((ancestor for ancestor in reversed(ancestors)
                      if ancestor != candidate_id and ancestor in readable), None) == folder_id]


def zip_segments(root_id: int | None, path_ids: Sequence[int],
                 readable: Mapping[int, str]) -> tuple[str, ...]:
    """ZIP path made of readable names only; unreadable levels are skipped (I-08)."""
    path = tuple(path_ids)
    if root_id is not None:
        if root_id not in path:
            return ()
        path = path[path.index(root_id):]
    return tuple(readable[folder_id] for folder_id in path if folder_id in readable)
