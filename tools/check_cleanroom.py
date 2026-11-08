# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""CLEAN: clean-room checks, exactly the four items of ER-33.

  CLEAN-HEADER    every .py, .xml, .js and .scss starts with "Copyright 2026 WoowTech" and an
                  LGPL-3 licence line (first 12 lines)
  CLEAN-LEGACY    no technical name of the legacy module (regex below) in any text file
  CLEAN-PHRASE    no phrase of tools/cleanroom_forbidden.txt in .po, .xml or .js files
  CLEAN-TREE      no static/lib directory, no symlink, no export-ignore / export-subst
CLEANROOM.md and tools/tests/fixtures/** are excluded (ER-33, ER-47.12). "文件管理" is allowed.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astlib as A  # noqa: E402

PHRASES_FILE = 'tools/cleanroom_forbidden.txt'
LEGACY_RE = re.compile(r'\bsh' + r'_[a-z0-9_]+')
HEADER_EXTS = ('.py', '.xml', '.js', '.scss')
PHRASE_EXTS = ('.po', '.pot', '.xml', '.js')
EXCLUDED = {'CLEANROOM.md', PHRASES_FILE}


def load_phrases(root):
    path = os.path.join(root, PHRASES_FILE)
    if not os.path.exists(path):
        raise A.ToolError('%s is missing' % PHRASES_FILE)
    return [line.strip() for line in A.read_text(path).split('\n')
            if line.strip() and not line.lstrip().startswith('#')]


def check(root, args):
    out = []
    phrases = load_phrases(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in A.SKIP_DIRS]
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            r = A.rel(root, path)
            if A.is_fixture(r):
                continue
            if os.path.islink(path):
                out.append(A.Violation(r, 1, 'CLEAN-TREE', 'symlinks are not allowed'))
            if name == 'lib' and os.path.isdir(path) and r.endswith('static/lib'):
                out.append(A.Violation(r, 1, 'CLEAN-TREE', 'static/lib (vendored third-party code) is not allowed'))
    for relpath, abspath in A.iter_files(root):
        if relpath in EXCLUDED or os.path.islink(abspath):
            continue
        text = A.try_read_text(abspath)
        if text is None:
            continue
        lines = text.split('\n')
        if relpath.endswith(HEADER_EXTS):
            head = '\n'.join(lines[:12])
            if 'Copyright 2026 WoowTech' not in head or 'LGPL-3' not in head:
                out.append(A.Violation(relpath, 1, 'CLEAN-HEADER',
                                       'missing "Copyright 2026 WoowTech" + LGPL-3 header'))
        for n, line in enumerate(lines, 1):
            m = LEGACY_RE.search(line)
            if m:
                out.append(A.Violation(relpath, n, 'CLEAN-LEGACY', 'legacy technical name %s' % m.group(0)))
        if relpath.endswith(PHRASE_EXTS):
            for n, line in enumerate(lines, 1):
                for phrase in phrases:
                    if phrase in line:
                        out.append(A.Violation(relpath, n, 'CLEAN-PHRASE', 'phrase listed in %s' % PHRASES_FILE))
        if os.path.basename(relpath) == '.gitattributes':
            for n, line in enumerate(lines, 1):
                if re.search(r'export-(ignore|subst)', line.split('#', 1)[0]):
                    out.append(A.Violation(relpath, n, 'CLEAN-TREE', 'export-ignore / export-subst are not allowed'))
    return out


def main(argv=None):
    return A.run_tool(check, __doc__.splitlines()[0], argv)


if __name__ == '__main__':
    sys.exit(main())
