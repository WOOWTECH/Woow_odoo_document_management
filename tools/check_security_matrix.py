# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""MTX: ACL, record rules and groups against tools/security_matrix.yaml (arch 4.1, 4.3, 4.4).

Every non-test XML file and every CSV of the addon is read, wherever it lives.
  MTX-LOCATION  ir.model.access / ir.rule / ir.module.category records and CSV files only under
                security/; res.groups records only in security/groups.xml, plus records in
                data/admin_assignment.xml that set nothing but users (ER-38); that file may also
                hold the PR-14 <function model="res.groups" name="write"> whose eval writes only
                users of a module group
  MTX-GROUP     groups.xml equals the matrix (category, implied groups); no users field (ER-38);
                no res.users record anywhere; no res.groups record outside the matrix writes
                implied_ids or users (base.group_user, base.group_portal, ...: DEC-10)
  MTX-FUNCTION  no <function> on res.groups / res.users / ir.rule / ir.model.access /
                ir.module.category, and no <function> whose eval mentions implied_ids or groups_id
                (base.default_user is the template of new users: DEC-10)
  MTX-DELETE    no <delete> on those models
  MTX-ACL       every ACL row (CSV and XML) matches the matrix; no base.group_public and no
                group-less rows; the log model has no W or D; --g1: every matrix row exists
  MTX-HD07      the portal folder row with W=1 carries "HD-07" in its id or name
  MTX-RULE      every ir.rule matches the matrix (model, groups, perms, domain compared as parsed
                Python expressions); no active=False and no global field; --g1: every matrix
                rule exists
  MTX-COMPANY   --g1: every model with company_id has a global company rule
Until core-models-acl lands (day 0) the CSV is header-only and rules.xml is empty: without --g1
only "actual is a subset of the matrix" is checked.
"""
from __future__ import annotations

import ast
import csv
import io
import os
import re
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astlib as A  # noqa: E402

MATRIX = 'tools/security_matrix.yaml'
SEC_DIR = A.ADDON + '/security'
CSV_FILE = SEC_DIR + '/ir.model.access.csv'
GROUPS_FILE = SEC_DIR + '/groups.xml'
ADMIN_FILE = A.ADDON + '/data/admin_assignment.xml'
PREFIX = A.ADDON + '.'
SECURITY_ONLY = ('ir.model.access', 'ir.rule', 'ir.module.category')
SENSITIVE_MODELS = SECURITY_ONLY + ('res.groups', 'res.users')
GROUP_IDS = ('group_woow_document_user', 'group_woow_document_manager',
             'group_woow_document_attachment_overview')
REF_RE = re.compile(r"ref\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _strip(ref):
    return ref[len(PREFIX):] if ref and ref.startswith(PREFIX) else ref


def _bool(value, default=True):
    if value is None:
        return default
    value = value.strip()
    if value in ('1', 'True', 'true'):
        return True
    if value in ('0', 'False', 'false', ''):
        return False
    raise ValueError('cannot read boolean %r' % value)


def _norm_domain(text):
    try:
        return ast.dump(ast.parse(' '.join(text.split()), mode='eval'))
    except SyntaxError:
        return 'unparsable:' + ' '.join(text.split())


def _field(record, name):
    for f in record.findall('field'):
        if f.get('name') == name:
            return f
    return None


def _admin_function_ok(node):
    """PR-14: <function model="res.groups" name="write" eval="[[ref('<module group>')], {'users': [...]}]"/>."""
    if node.get('model') != 'res.groups' or node.get('name') != 'write' or list(node):
        return False
    try:
        expr = ast.parse(node.get('eval') or '', mode='eval').body
    except SyntaxError:
        return False
    if not (isinstance(expr, ast.List) and len(expr.elts) == 2):
        return False
    ids, vals = expr.elts
    if not (isinstance(ids, ast.List) and ids.elts and isinstance(vals, ast.Dict)):
        return False
    for elt in ids.elts:
        if not (isinstance(elt, ast.Call) and A.dotted(elt.func) == 'ref' and len(elt.args) == 1
                and _strip(A.const_str(elt.args[0]) or '') in GROUP_IDS):
            return False
    return [A.const_str(k) for k in vals.keys] == ['users']


def _rel_addon(relpath):
    return A.ADDON + '/' + relpath


def _line_of(text, rid):
    if rid:
        for n, line in enumerate(text.split('\n'), 1):
            if 'id="%s"' % rid in line or "id='%s'" % rid in line:
                return n
    return 1


def _perms(rec, default=True):
    perms = ''
    for k in ('perm_read', 'perm_write', 'perm_create', 'perm_unlink'):
        f = _field(rec, k)
        perms += '1' if _bool(f.get('eval') if f is not None else None, default) else '0'
    return perms


def load_actual(root):
    """(acl rows, rules, groups, location problems) from every addon XML and CSV file."""
    acl, rules, groups, problems = [], {}, {}, []
    addon = os.path.join(root, A.ADDON)

    def bad(relpath, line, rule, message):
        problems.append(A.Violation(relpath, line, rule, message))

    for r, path in A.iter_files(addon, exts=('.csv',)):
        relpath = _rel_addon(r)
        if A.is_test_path(relpath):
            continue
        model = os.path.basename(relpath)[:-len('.csv')]
        if model in SENSITIVE_MODELS and relpath != CSV_FILE:
            bad(relpath, 1, 'MTX-LOCATION', '%s rows only in %s' % (model, CSV_FILE))
        if model != 'ir.model.access':
            continue
        reader = csv.DictReader(io.StringIO(A.read_text(path)))
        for n, row in enumerate(reader, 2):
            acl.append({
                'where': (relpath, n), 'id': row.get('id', ''), 'name': row.get('name', ''),
                'model': _strip(row.get('model_id:id', '')), 'group': _strip(row.get('group_id:id') or ''),
                'perms': ''.join('1' if _bool(row.get(k), False) else '0' for k in (
                    'perm_read', 'perm_write', 'perm_create', 'perm_unlink')),
            })
    for r, path in A.iter_files(addon, exts=('.xml',)):
        relpath = _rel_addon(r)
        if A.is_test_path(relpath):
            continue
        text = A.read_text(path)
        # repo files only (trusted input); stdlib expat does not resolve external entities
        tree = ET.fromstring(text.encode())
        in_security = relpath.startswith(SEC_DIR + '/')
        for node in tree.iter():
            model = node.get('model')
            rid = _strip(node.get('id', ''))
            line = _line_of(text, node.get('id', ''))
            if node.tag == 'function':
                body = ' '.join([node.get('eval') or ''] + [
                    (sub.get('eval') or '') + ' ' + (sub.text or '') for sub in node.iter()])
                if relpath == ADMIN_FILE and _admin_function_ok(node):
                    continue
                if model in SENSITIVE_MODELS:
                    bad(relpath, line, 'MTX-FUNCTION', '<function model="%s"> is not allowed' % model)
                elif 'implied_ids' in body or 'groups_id' in body:
                    bad(relpath, line, 'MTX-FUNCTION', '<function> writing implied_ids / groups_id (DEC-10)')
                continue
            if node.tag == 'delete':
                if model in SENSITIVE_MODELS:
                    bad(relpath, line, 'MTX-DELETE', '<delete model="%s"> is not allowed' % model)
                continue
            if node.tag != 'record':
                continue
            fields = {f.get('name'): f for f in node.findall('field')}
            if model in SECURITY_ONLY and not in_security:
                bad(relpath, line, 'MTX-LOCATION', '%s records only under %s/' % (model, SEC_DIR))
            if model == 'res.users':
                bad(relpath, line, 'MTX-GROUP', 'no res.users record (%s): users get document groups '
                    'only by assignment (DEC-10)' % (rid or '?'))
                continue
            if model == 'res.groups':
                if relpath == ADMIN_FILE:
                    extra = sorted(set(fields) - {'users'})
                    if extra:
                        bad(relpath, line, 'MTX-LOCATION', 'admin assignment of %s may set only users '
                            '(got %s)' % (rid, ', '.join(extra)))
                    if rid not in GROUP_IDS:
                        bad(relpath, line, 'MTX-GROUP', 'admin assignment targets %s, not a module group' % rid)
                    continue
                if relpath != GROUPS_FILE:
                    bad(relpath, line, 'MTX-LOCATION', 'res.groups records only in %s' % GROUPS_FILE)
                    if rid not in GROUP_IDS and '.' not in rid:
                        bad(relpath, line, 'MTX-GROUP', 'group %s is not in the matrix' % rid)
                if rid not in GROUP_IDS and ('implied_ids' in fields or 'users' in fields):
                    bad(relpath, line, 'MTX-GROUP', '%s must not get implied_ids / users from this module '
                        '(DEC-10)' % rid)
                if relpath != GROUPS_FILE:
                    continue
            if model == 'ir.model.access':
                mf, gf, nf = _field(node, 'model_id'), _field(node, 'group_id'), _field(node, 'name')
                acl.append({'where': (relpath, line), 'id': rid,
                            'name': (nf.text if nf is not None else '') or '',
                            'model': _strip(mf.get('ref') if mf is not None else ''),
                            'group': _strip(gf.get('ref') if gf is not None else '') or '',
                            'perms': _perms(node, default=False)})
            elif model == 'ir.rule':
                mf, gf, df = _field(node, 'model_id'), _field(node, 'groups'), _field(node, 'domain_force')
                domain = '' if df is None else (df.get('eval') or df.text or '')
                af = fields.get('active')
                if af is not None and not _bool(af.get('eval') if af.get('eval') is not None else af.text, True):
                    bad(relpath, line, 'MTX-RULE', 'rule %s is disabled (active=False)' % rid)
                if 'global' in fields:
                    bad(relpath, line, 'MTX-RULE', 'rule %s sets global (it follows from groups)' % rid)
                rules[rid] = {'where': (relpath, line), 'model': _strip(mf.get('ref') if mf is not None else ''),
                              'groups': sorted(_strip(g) for g in REF_RE.findall(gf.get('eval', '') if gf is not None else '')),
                              'perms': _perms(node), 'domain': domain}
            elif model in ('res.groups', 'ir.module.category'):
                groups[rid] = {'where': (relpath, line), 'model': model,
                               'category': _strip(fields['category_id'].get('ref')) if 'category_id' in fields else None,
                               'implied': sorted(_strip(r) for r in REF_RE.findall(
                                   fields['implied_ids'].get('eval', ''))) if 'implied_ids' in fields else [],
                               'has_users': 'users' in fields}
    return acl, rules, groups, problems


def model_ref(model):
    return 'model_' + model.replace('.', '_')


def check(root, args):
    out = []
    path = os.path.join(root, MATRIX)
    if not os.path.exists(path):
        raise A.ToolError('%s is missing' % MATRIX)
    mx = A.load_yaml_file(path)
    alias = mx['aliases']
    acl, rules, groups, problems = load_actual(root)
    out.extend(problems)

    # groups (final on day 0)
    for gid, spec in mx['groups'].items():
        got = groups.get(gid)
        where = (SEC_DIR + '/groups.xml', 1)
        if got is None:
            out.append(A.Violation(where[0], where[1], 'MTX-GROUP', 'group %s is missing' % gid))
            continue
        want_implied = sorted(spec.get('implied') or [])
        if got['implied'] != want_implied or got['category'] != spec.get('category'):
            out.append(A.Violation(got['where'][0], 1, 'MTX-GROUP',
                                   'group %s differs from the matrix (implied %s, category %s)' % (
                                       gid, got['implied'], got['category'])))
        if got['has_users']:
            out.append(A.Violation(got['where'][0], 1, 'MTX-GROUP',
                                   'group %s must not set users (admin assignment is noupdate, ER-38)' % gid))
    for cat in mx.get('categories') or []:
        if cat not in groups:
            out.append(A.Violation(SEC_DIR + '/groups.xml', 1, 'MTX-GROUP', 'category %s is missing' % cat))
    for gid, got in groups.items():
        if got['model'] == 'res.groups' and gid not in mx['groups']:
            out.append(A.Violation(got['where'][0], 1, 'MTX-GROUP', 'group %s is not in the matrix' % gid))
    user_group = alias['user']
    if user_group in (groups.get('base.group_user') or {}).get('implied', []):
        out.append(A.Violation(GROUPS_FILE, 1, 'MTX-GROUP', 'base.group_user must not imply %s (DEC-10)' % user_group))

    # ACL
    by_ref = {model_ref(m): m for m in mx['acl']}
    want_acl = {}
    for model, per_group in mx['acl'].items():
        for g, perms in per_group.items():
            want_acl[(model, alias.get(g, g))] = perms
    hd07_model, hd07_alias = mx['hd07']
    seen = set()
    for row in acl:
        rp, line = row['where']
        model = by_ref.get(row['model'], row['model'])
        group = row['group']
        if not group:
            out.append(A.Violation(rp, line, 'MTX-ACL', 'ACL row %s has no group (would include public)' % row['id']))
            continue
        if group == 'base.group_public':
            out.append(A.Violation(rp, line, 'MTX-ACL', 'ACL row %s grants base.group_public' % row['id']))
            continue
        if model == 'woow.document.log' and (row['perms'][1] == '1' or row['perms'][3] == '1'):
            out.append(A.Violation(rp, line, 'MTX-ACL', 'the audit log has no write or unlink ACL'))
        want = want_acl.get((model, group))
        if want is None:
            out.append(A.Violation(rp, line, 'MTX-ACL', 'ACL row %s (%s, %s) is not in the matrix' % (row['id'], model, group)))
        elif want != row['perms']:
            out.append(A.Violation(rp, line, 'MTX-ACL', 'ACL row %s perms %s, matrix %s' % (row['id'], row['perms'], want)))
        if (model, group) in seen:
            out.append(A.Violation(rp, line, 'MTX-ACL', 'duplicate ACL row for (%s, %s)' % (model, group)))
        seen.add((model, group))
        if model == hd07_model and group == alias[hd07_alias] and row['perms'][1] == '1' \
                and 'HD-07' not in (row['id'] + ' ' + row['name']):
            out.append(A.Violation(rp, line, 'MTX-HD07', 'portal folder W=1 row must say HD-07 (probe only)'))
    if args.g1:
        for key in sorted(set(want_acl) - seen):
            out.append(A.Violation(SEC_DIR + '/ir.model.access.csv', 1, 'MTX-ACL', 'missing ACL row %s / %s' % key))

    # rules
    want_rules = {r['id']: r for r in mx['rules']}
    for rid, got in rules.items():
        rp, line = got['where']
        want = want_rules.get(rid)
        if want is None:
            out.append(A.Violation(rp, line, 'MTX-RULE', 'rule %s is not in the matrix' % rid))
            continue
        model = by_ref.get(got['model'], got['model'])
        groups_want = sorted(alias.get(g, g) for g in (want.get('groups') or []))
        if model != want['model']:
            out.append(A.Violation(rp, line, 'MTX-RULE', 'rule %s model %s, matrix %s' % (rid, model, want['model'])))
        if got['groups'] != groups_want:
            out.append(A.Violation(rp, line, 'MTX-RULE', 'rule %s groups %s, matrix %s' % (rid, got['groups'], groups_want)))
        if got['perms'] != want['perms']:
            out.append(A.Violation(rp, line, 'MTX-RULE', 'rule %s perms %s, matrix %s' % (rid, got['perms'], want['perms'])))
        if _norm_domain(got['domain']) != _norm_domain(want['domain']):
            out.append(A.Violation(rp, line, 'MTX-RULE', 'rule %s domain differs from the matrix' % rid))
        if 'commercial_partner_id' in got['domain']:
            out.append(A.Violation(rp, line, 'MTX-RULE', 'rule %s uses commercial_partner_id (ER-04)' % rid))
    if args.g1:
        for rid in sorted(set(want_rules) - set(rules)):
            out.append(A.Violation(SEC_DIR + '/rules.xml', 1, 'MTX-RULE', 'missing rule %s (%s)' % (rid, want_rules[rid]['ref'])))
        for model in mx.get('company_models') or []:
            if not any(r['model'] == model and not r.get('groups') and 'company_id' in r['domain']
                       and r['id'] in rules for r in mx['rules']):
                out.append(A.Violation(SEC_DIR + '/rules.xml', 1, 'MTX-COMPANY', '%s has no global company rule' % model))
    return out


def main(argv=None):
    return A.run_tool(check, __doc__.splitlines()[0], argv)


if __name__ == '__main__':
    sys.exit(main())
