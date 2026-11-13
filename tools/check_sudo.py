# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""SUDO: privilege escalation allowlist (security_and_sweep section 6, ER-25, ER-44).

Allowlist (file, model, function -> count):
  S1  models/document_log.py   woow.document.log    _log_event              1  (# sudo-ok: S1)
  S3  models/settings.py       woow.document.settings _read_params          1  (# sudo-ok: S3)

Every other escalation in non-test code is a violation: any ``sudo`` attribute except the
literal ``sudo(False)``, ``with_user(``, ``with_env(``, ``env(su=...|user=...)`` except
``env(su=False)``, any ``su=True`` keyword, ``api.Environment(``, ``SUPERUSER_ID``,
``request.update_env(user=...)``, ``_get_param(``, ``getattr(x, 'sudo')``, and explicit
``compute_sudo=True`` / ``related_sudo=True``. ``with_user(`` is never allowed.

The model of a class is its ``_name`` literal, else its single string ``_inherit`` (ER-44).
Counts are "<=" by default and "==" with --g1. Also enforced: no ``<name>_sudo.env`` anywhere
(T-STAT-19), and ``_cron_*`` start with an ``self.env.su`` check.
"""
from __future__ import annotations

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astlib as A  # noqa: E402

ALLOW = {
    'S1': ('woow_document_management/models/document_log.py', 'woow.document.log', '_log_event',
           {'sudo': 1}),
    'S3': ('woow_document_management/models/settings.py', 'woow.document.settings',
           '_read_params', {'sudo': 1}),
}
SU_CHECKED_PREFIXES = ('_cron_',)


def _escalations(src):
    """Yield (node, kind, text) for every escalation node of a parsed file."""
    for node in ast.walk(src.tree):
        if isinstance(node, ast.Attribute) and node.attr == 'sudo':
            par = A.parent(node)
            if isinstance(par, ast.Call) and par.func is node and len(par.args) == 1 \
                    and A.is_const(par.args[0], False) and not par.keywords:
                continue
            yield node, 'sudo', '.sudo'
        elif isinstance(node, ast.Call):
            name = A.call_name(node)
            if name == 'with_user':
                yield node, 'with_user', 'with_user('
            elif name == 'with_env':
                yield node, 'other', 'with_env('
            elif name == 'env' and (A.kw(node, 'user') is not None or (
                    A.kw(node, 'su') is not None and not A.is_const(A.kw(node, 'su'), False))):
                yield node, 'other', 'env(su=/user=)'
            elif name == 'Environment':
                yield node, 'other', 'api.Environment('
            elif name == 'update_env' and A.kw(node, 'user') is not None:
                yield node, 'other', 'update_env(user=)'
            elif name == '_get_param':
                yield node, 'other', '_get_param('
            elif name == 'getattr' and len(node.args) >= 2 and A.const_str(node.args[1]) == 'sudo':
                yield node, 'other', "getattr(..., 'sudo')"
            for keyword in node.keywords:
                if keyword.arg in ('compute_sudo', 'related_sudo') and A.is_const(keyword.value, True):
                    yield node, 'other', '%s=True' % keyword.arg
                elif keyword.arg == 'su' and A.is_const(keyword.value, True) and name != 'env':
                    yield node, 'other', 'su=True'
        elif isinstance(node, ast.Name) and node.id == 'SUPERUSER_ID':
            yield node, 'other', 'SUPERUSER_ID'
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == 'SUPERUSER_ID' for alias in node.names):
                yield node, 'other', 'import SUPERUSER_ID'
        elif isinstance(node, ast.Attribute) and node.attr == 'SUPERUSER_ID':
            yield node, 'other', 'SUPERUSER_ID'


def _context(node):
    func = A.enclosing_function(node)
    cls = A.enclosing_class(func) if func is not None else A.enclosing_class(node)
    model = A.class_model(cls) if cls is not None else None
    return func, cls, model


def _is_su_check(stmt):
    """``if not self.env.su: raise ...``."""
    if not isinstance(stmt, ast.If):
        return False
    test = stmt.test
    if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
        return False
    operand = test.operand
    if not (isinstance(operand, ast.Attribute) and operand.attr == 'su'
            and isinstance(operand.value, ast.Attribute) and operand.value.attr == 'env'
            and isinstance(operand.value.value, ast.Name) and operand.value.value.id == 'self'):
        return False
    return any(isinstance(s, ast.Raise) for s in stmt.body)


def check(root, args):
    out = []
    counts = {code: {'sudo': 0} for code in ALLOW}
    seen_files = set()
    for relpath, _abspath in A.iter_files(root, exts=('.py',)):
        test_file = A.is_test_path(relpath)
        src = A.parse_py(root, relpath)
        # T-STAT-19: <name>_sudo.env anywhere (tests included)
        for node in ast.walk(src.tree):
            if isinstance(node, ast.Attribute) and node.attr == 'env' \
                    and isinstance(node.value, ast.Name) and node.value.id.endswith('_sudo'):
                out.append(A.Violation(relpath, node.lineno, 'SUDO-ENV-REUSE',
                                       '%s.env must not be used; use the scope env' % node.value.id))
        if test_file:
            continue
        for node, kind, text in _escalations(src):
            func, cls, model = _context(node)
            fname = func.name if func is not None else None
            comment = src.comment_for(node) if not isinstance(node, ast.Attribute) else \
                src.comment(node.lineno)
            matched = None
            for code, (afile, amodel, afunc, _kinds) in ALLOW.items():
                if relpath == afile and model == amodel and fname == afunc:
                    matched = code
            if matched and kind == 'sudo':
                tag = 'sudo-ok: %s' % matched
                if tag not in comment:
                    out.append(A.Violation(relpath, node.lineno, 'SUDO-UNTAGGED',
                                           '%s needs "# %s" on the same line' % (text, tag)))
                    continue
                counts[matched]['sudo'] += 1
                seen_files.add(relpath)
                continue
            if kind == 'with_user':
                out.append(A.Violation(relpath, node.lineno, 'SUDO-WITH-USER',
                                       'with_user( is not allowed'))
            else:
                where = '%s::%s' % (model or '?', fname or '<module>')
                out.append(A.Violation(relpath, node.lineno, 'SUDO-NOT-ALLOWED',
                                       '%s outside the allowlist (%s)' % (text, where)))
        for node in ast.walk(src.tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name.startswith(SU_CHECKED_PREFIXES):
                        if not _is_su_check(A.first_statement(item)):
                            out.append(A.Violation(relpath, item.lineno, 'SUDO-CRON-SU',
                                                   '%s must start with "if not self.env.su: raise"' % item.name))
    for code, (afile, _model, afunc, expected) in ALLOW.items():
        for kind, n in expected.items():
            got = counts[code][kind]
            bad = got != n if args.g1 else got > n
            if bad:
                out.append(A.Violation(afile, 1, 'SUDO-COUNT',
                                       '%s %s count is %d, expected %s%d' % (
                                           code, kind, got, '' if args.g1 else '<=', n)))
    return out


def main(argv=None):
    return A.run_tool(check, __doc__.splitlines()[0], argv)


if __name__ == '__main__':
    sys.exit(main())
