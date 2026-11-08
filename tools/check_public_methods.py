# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""PUBM: public method allowlist (architecture 6.5, interface_contracts 4.9, IC-R10).

For every model class (``_name`` / ``_inherit``) under */models/ and */wizards/:
  PUBM-UNLISTED   a method without a leading '_' that is neither in [public] nor in [override]
  PUBM-FIRST      a [public] method whose first statement does not call check_access(,
                  _woow_has_rank(, _woow_require_rank(, has_group( or _require_manager(
  PUBM-OVERRIDE   an [override] method that neither calls super() nor raises
  PUBM-ABSTRACT   a public method on an AbstractModel that is not an [override]
  PUBM-API-MODEL  an @api.model public method that is not an [override]
  PUBM-MISSING    --g1: a [public] entry that no class defines
Outside --g1 a body of only ``raise NotImplementedError  # WOOW-STUB`` counts as compliant.
"""
from __future__ import annotations

import ast
import fnmatch
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astlib as A  # noqa: E402

LIST_FILE = 'tools/public_methods.txt'
CHECKS = ('check_access', '_woow_has_rank', '_woow_require_rank', 'has_group', '_require_manager')


def load_list(root):
    path = os.path.join(root, LIST_FILE)
    if not os.path.exists(path):
        raise A.ToolError('%s is missing' % LIST_FILE)
    sections = {'public': [], 'override': []}
    current = None
    for raw in A.read_text(path).split('\n'):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        if line in ('[public]', '[override]'):
            current = line[1:-1]
            continue
        if current is None:
            raise A.ToolError('%s: entry before a section header' % LIST_FILE)
        for item in line.split():
            if '::' not in item:
                raise A.ToolError('%s: bad entry %r' % (LIST_FILE, item))
            sections[current].append(item)
    return sections


def _has_check(stmt):
    if stmt is None:
        return False
    scope = stmt.test if isinstance(stmt, ast.If) else stmt
    if not isinstance(stmt, (ast.Expr, ast.Assign, ast.If, ast.AnnAssign)):
        return False
    for node in ast.walk(scope):
        if isinstance(node, ast.Call) and A.call_name(node) in CHECKS:
            return True
    return False


def _calls_super_or_raises(func):
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'super':
            return True
    return any(isinstance(stmt, ast.Raise) for stmt in A.body_without_docstring(func))


def _is_api_model(func):
    for dec in func.decorator_list:
        if A.dotted(dec) in ('api.model', 'model'):
            return True
    return False


def check(root, args):
    out = []
    lists = load_list(root)
    public = set(lists['public'])
    overrides = lists['override']
    defined = set()
    for relpath, _abspath in A.iter_files(root, exts=('.py',), include_tests=False):
        parts = relpath.split('/')
        if 'models' not in parts[:-1] and 'wizards' not in parts[:-1]:
            continue
        src = A.parse_py(root, relpath)
        for cls in ast.walk(src.tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            models = A.class_model_names(cls)
            if not models:
                continue
            abstract = A.class_kind(cls) == 'AbstractModel' and A.const_str(A.class_assign(cls, '_name'))
            for item in cls.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) or item.name.startswith('_'):
                    continue
                body = A.body_without_docstring(item)
                stub = (not args.g1 and len(body) == 1 and A.is_stub_raise(body[0], src))
                for model in models:
                    key = '%s::%s' % (model, item.name)
                    is_override = any(fnmatch.fnmatchcase(key, pat) for pat in overrides)
                    if key in public:
                        defined.add(key)
                    if is_override:
                        if not _calls_super_or_raises(item):
                            out.append(A.Violation(relpath, item.lineno, 'PUBM-OVERRIDE',
                                                   '%s must call super() or raise' % key))
                        continue
                    if abstract:
                        out.append(A.Violation(relpath, item.lineno, 'PUBM-ABSTRACT',
                                               'AbstractModel %s must not expose %s' % (model, item.name)))
                        continue
                    if _is_api_model(item):
                        out.append(A.Violation(relpath, item.lineno, 'PUBM-API-MODEL',
                                               '@api.model public method %s is not allowed' % key))
                    if key not in public:
                        out.append(A.Violation(relpath, item.lineno, 'PUBM-UNLISTED',
                                               '%s is not in %s' % (key, LIST_FILE)))
                        continue
                    if not stub and not _has_check(A.first_statement(item)):
                        out.append(A.Violation(relpath, item.lineno, 'PUBM-FIRST',
                                               '%s must start with an access check' % key))
    if args.g1:
        for key in sorted(public - defined):
            out.append(A.Violation(LIST_FILE, 1, 'PUBM-MISSING', '%s is listed but not defined' % key))
    return out


def main(argv=None):
    return A.run_tool(check, __doc__.splitlines()[0], argv)


if __name__ == '__main__':
    sys.exit(main())
