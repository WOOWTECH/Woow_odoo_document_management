# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""SQL: raw SQL allowlist (codes of tools/sql_allowlist.yaml; architecture 6.4, security_and_sweep 8, ER-24, ER-47.4).

- ``<x>.execute(`` and ``SQL(`` in non-test code only inside a tools/sql_allowlist.yaml entry
  (file + function) with ``# sql-ok: <id>`` on the line of the call.
- The query argument is a string literal, a name bound to one in the same function / module,
  or ``SQL(<literal>, ...)``; f-strings, ``%``, ``.format`` and ``+`` are rejected.
- ``pg_advisory`` / ``pg_try_advisory`` in any code file outside services/locks.py fails (T-STAT-22),
  and services/locks.py must not call the builtin ``hash(`` (keys come from sha256).
- ``CREATE EXTENSION`` is rejected everywhere.
- --g1: every allowlist entry is used at least once.
tools/ and tools/tests/fixtures/ are not scanned.
"""
from __future__ import annotations

import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astlib as A  # noqa: E402

ALLOWLIST = 'tools/sql_allowlist.yaml'
LOCKS_FILE = 'woow_document_management/services/locks.py'
ADVISORY_RE = re.compile(r'pg_(try_)?advisory', re.I)
EXTENSION_RE = re.compile(r'create\s+extension', re.I)
CODE_EXTS = ('.py', '.xml', '.js', '.sh', '.csv', '.sql', '.scss')
TAG_RE = re.compile(r'sql-ok:\s*(Q\d+)')


def load_allowlist(root):
    path = os.path.join(root, ALLOWLIST)
    if not os.path.exists(path):
        raise A.ToolError('%s is missing' % ALLOWLIST)
    data = A.load_yaml_file(path) or {}
    entries = data.get('entries') or []
    for entry in entries:
        if not {'id', 'file', 'functions'} <= set(entry):
            raise A.ToolError('bad entry in %s: %r' % (ALLOWLIST, entry))
    return entries


def _bound_literal(name, func, module):
    """True when ``name`` is assigned only literals / SQL(<literal>) in func or at module level."""
    for scope in (func, module):
        if scope is None:
            continue
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and name in {
                arg.arg for arg in (scope.args.posonlyargs + scope.args.args +
                                    scope.args.kwonlyargs +
                                    ([scope.args.vararg] if scope.args.vararg else []) +
                                    ([scope.args.kwarg] if scope.args.kwarg else []))}:
            return False
        safe = set()
        bindings = set()
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name \
                    and _query_ok(node.value, None, None, depth=1):
                safe.add(id(node.targets[0]))
            for child in ast.walk(node) if isinstance(node, (ast.Assign, ast.AnnAssign,
                    ast.AugAssign, ast.NamedExpr, ast.For, ast.AsyncFor, ast.With,
                    ast.AsyncWith, ast.ExceptHandler)) else ():
                if isinstance(child, ast.Name) and child.id == name and isinstance(child.ctx, ast.Store):
                    bindings.add(id(child))
        if bindings:
            return bindings == safe
    return False


def _query_ok(node, func, module, depth=0):
    if A.const_str(node) is not None:
        return True
    if isinstance(node, ast.Call) and A.call_name(node) == 'SQL' and node.args:
        return A.const_str(node.args[0]) is not None or (
            isinstance(node.args[0], ast.Name) and depth == 0
            and _bound_literal(node.args[0].id, func, module))
    if isinstance(node, ast.Name) and depth == 0:
        return _bound_literal(node.id, func, module)
    return False


def check(root, args):
    out = []
    entries = load_allowlist(root)
    used = set()
    for relpath, abspath in A.iter_files(root):
        if relpath.startswith('tools/'):
            continue
        text = A.try_read_text(abspath)
        if text is None:
            continue
        is_test = A.is_test_path(relpath)
        if not is_test and relpath.endswith(CODE_EXTS):
            for n, line in enumerate(text.split('\n'), 1):
                if ADVISORY_RE.search(line) and relpath != LOCKS_FILE:
                    out.append(A.Violation(relpath, n, 'SQL-ADVISORY',
                                           'advisory locks only through services/locks.py (ER-24)'))
                if EXTENSION_RE.search(line):
                    out.append(A.Violation(relpath, n, 'SQL-EXTENSION', 'CREATE EXTENSION is not allowed'))
        if not relpath.endswith('.py') or is_test:
            continue
        src = A.parse_py(root, relpath)
        for node in ast.walk(src.tree):
            if not isinstance(node, ast.Call):
                continue
            name = A.call_name(node)
            if relpath == LOCKS_FILE and name == 'hash' and isinstance(node.func, ast.Name):
                out.append(A.Violation(relpath, node.lineno, 'SQL-LOCK-HASH',
                                       'builtin hash() is salted per process; use advisory_key()'))
            is_execute = name in ('execute', 'executemany') and isinstance(node.func, ast.Attribute)
            is_sql = name == 'SQL' and isinstance(A.parent(node), ast.AST) and not (
                isinstance(A.parent(node), ast.Call) and A.call_name(A.parent(node)) in ('execute', 'executemany'))
            if not (is_execute or is_sql):
                continue
            func = A.enclosing_function(node)
            fname = func.name if func is not None else None
            tags = TAG_RE.findall(src.comment_for(node))
            entry = None
            for e in entries:
                if e['file'] == relpath and (fname in e['functions'] or '*' in e['functions']):
                    if not tags or e['id'] in tags:
                        entry = e
                        break
            if entry is None:
                out.append(A.Violation(relpath, node.lineno, 'SQL-NOT-ALLOWED',
                                       'raw SQL outside tools/sql_allowlist.yaml (%s)' % (fname or '<module>')))
                continue
            if entry['id'] not in tags:
                out.append(A.Violation(relpath, node.lineno, 'SQL-UNTAGGED',
                                       'needs "# sql-ok: %s" on the same line' % entry['id']))
                continue
            used.add((entry['id'], entry['file']))
            query = node.args[0] if node.args else None
            if is_execute and (query is None or not _query_ok(query, func, src.tree)):
                out.append(A.Violation(relpath, node.lineno, 'SQL-FORMAT',
                                       'query must be a literal or SQL(<literal>); no f-string, %, format or +'))
            if is_sql and (not node.args or not _query_ok(node, func, src.tree)):
                out.append(A.Violation(relpath, node.lineno, 'SQL-FORMAT',
                                       'SQL( needs a literal template; values through %s parameters'))
    if args.g1:
        for e in entries:
            if (e['id'], e['file']) not in used:
                out.append(A.Violation(e['file'], 1, 'SQL-UNUSED',
                                       'allowlist entry %s is not used' % e['id']))
    return out


def main(argv=None):
    return A.run_tool(check, __doc__.splitlines()[0], argv)


if __name__ == '__main__':
    sys.exit(main())
