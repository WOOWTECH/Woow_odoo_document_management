# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""SUDO: privilege escalation allowlist (security_and_sweep section 6, ER-25, ER-44).

Allowlist (file, model, function -> count):
  S1  models/document_log.py   woow.document.log    _log_event              1  (# sudo-ok: S1)
  S2  models/share_service.py  woow.document.share  _resolve_request_access 2  (a: # sudo-ok: S2,
                                                                                b: # sudo-drop: S2)
  S3  models/settings.py       woow.document.settings _read_params          1  (# sudo-ok: S3)

Every other escalation in non-test code is a violation: any ``sudo`` attribute except the
literal ``sudo(False)``, ``with_user(``, ``with_env(``, ``env(su=...|user=...)`` except
``env(su=False)``, any ``su=True`` keyword, ``api.Environment(``, ``SUPERUSER_ID``,
``request.update_env(user=...)``, ``_get_param(``, ``getattr(x, 'sudo')``, and explicit
``compute_sudo=True`` / ``related_sudo=True``. ``with_user(`` is allowed only as S2-b.

The model of a class is its ``_name`` literal, else its single string ``_inherit`` (ER-44).
Counts are "<=" by default and "==" with --g1. Also enforced: the S2 shape (T-STAT-18), no
``<name>_sudo.env`` anywhere (T-STAT-19), and ``_cron_*`` / ``_maintenance_run`` start with an
``self.env.su`` check.
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
    'S2': ('woow_document_management/models/share_service.py', 'woow.document.share',
           '_resolve_request_access', {'sudo': 1, 'with_user': 1}),
    'S3': ('woow_document_management/models/settings.py', 'woow.document.settings',
           '_read_params', {'sudo': 1}),
}
SU_CHECKED_PREFIXES = ('_cron_',)
SU_CHECKED_NAMES = ('_maintenance_run',)


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


def _check_s2_shape(src, func, out):
    """T-STAT-18: sec 6.2 point 6 (with interface_contracts 21 B1)."""
    path = src.relpath
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and node.attr == 'sudo':
            call = A.parent(node)
            search = A.parent(call)
            outer = A.parent(search) if search is not None else None
            ok = (isinstance(call, ast.Call) and not call.args and not call.keywords
                  and isinstance(node.value, ast.Name) and node.value.id == 'self'
                  and isinstance(search, ast.Attribute) and search.attr == 'search'
                  and isinstance(outer, ast.Call) and outer.func is search)
            if ok:
                limit = A.kw(outer, 'limit')
                ok = (len(outer.args) == 1 and isinstance(limit, ast.Constant)
                      and limit.value == 1 and len(outer.keywords) == 1)
            if ok:
                domain = outer.args[0]
                def fixed_field(node):
                    if not isinstance(node, ast.List) or len(node.elts) != 1:
                        return None
                    cond = node.elts[0]
                    if not isinstance(cond, ast.Tuple) or len(cond.elts) != 3 \
                            or A.const_str(cond.elts[1]) != '=':
                        return None
                    field = A.const_str(cond.elts[0])
                    return field if field in ('token_hash', 'id') else None
                if isinstance(domain, ast.IfExp):
                    # Exactly two fixed equality probes may share one S2-a call.
                    ok = {fixed_field(domain.body), fixed_field(domain.orelse)} == {'token_hash', 'id'}
                else:
                    ok = fixed_field(domain) is not None
            if not ok:
                out.append(A.Violation(path, node.lineno, 'SUDO-S2-SHAPE',
                                       "S2-a must be self.sudo().search([('token_hash'|'id', '=', v)], limit=1)"))
        if isinstance(node, ast.Call) and A.call_name(node) == 'with_user':
            recv = node.func.value if isinstance(node.func, ast.Attribute) else None
            arg = node.args[0] if len(node.args) == 1 and not node.keywords else None
            ok = (isinstance(recv, ast.Name) and recv.id.endswith('_sudo')
                  and isinstance(arg, ast.Attribute) and arg.attr == 'create_uid'
                  and isinstance(arg.value, ast.Name) and arg.value.id == recv.id)
            wrapper = A.parent(node)
            ok = ok and isinstance(wrapper, ast.Attribute) and wrapper.attr == 'with_context'
            wcall = A.parent(wrapper) if ok else None
            ok = ok and isinstance(wcall, ast.Call) and A.kw(wcall, 'allowed_company_ids') is not None
            if not ok:
                out.append(A.Violation(path, node.lineno, 'SUDO-S2-SHAPE',
                                       'S2-b must be <x>_sudo.with_user(<x>_sudo.create_uid)'
                                       '.with_context(allowed_company_ids=[...])'))
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and A.enclosing_function(node) is func:
            if not isinstance(node.value, ast.Tuple):
                out.append(A.Violation(path, node.lineno, 'SUDO-S2-SHAPE',
                                       '_resolve_request_access must always return a tuple'))


def check(root, args):
    out = []
    counts = {code: {'sudo': 0, 'with_user': 0} for code in ALLOW}
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
            if matched and kind in ('sudo', 'with_user'):
                tag = ('sudo-drop: %s' if kind == 'with_user' else 'sudo-ok: %s') % matched
                if matched != 'S2' and kind == 'with_user':
                    out.append(A.Violation(relpath, node.lineno, 'SUDO-WITH-USER',
                                           'with_user( is only allowed as S2-b'))
                    continue
                if tag not in comment:
                    out.append(A.Violation(relpath, node.lineno, 'SUDO-UNTAGGED',
                                           '%s needs "# %s" on the same line' % (text, tag)))
                    continue
                counts[matched][kind] += 1
                seen_files.add(relpath)
                continue
            if kind == 'with_user':
                out.append(A.Violation(relpath, node.lineno, 'SUDO-WITH-USER',
                                       'with_user( is only allowed as S2-b'))
            else:
                where = '%s::%s' % (model or '?', fname or '<module>')
                out.append(A.Violation(relpath, node.lineno, 'SUDO-NOT-ALLOWED',
                                       '%s outside the allowlist (%s)' % (text, where)))
        for node in ast.walk(src.tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and (
                            item.name.startswith(SU_CHECKED_PREFIXES) or item.name in SU_CHECKED_NAMES):
                        if not _is_su_check(A.first_statement(item)):
                            out.append(A.Violation(relpath, item.lineno, 'SUDO-CRON-SU',
                                                   '%s must start with "if not self.env.su: raise"' % item.name))
                    if isinstance(item, ast.FunctionDef) and item.name == '_resolve_request_access' \
                            and A.class_model(node) == 'woow.document.share':
                        _check_s2_shape(src, item, out)
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
