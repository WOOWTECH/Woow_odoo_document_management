# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""MTX: ACL, record rules and groups against tools/security_matrix.yaml (arch 4.1, 4.3, 4.4).

  MTX-GROUP     groups.xml equals the matrix (category, implied groups); no users field (ER-38);
                nothing makes base.group_user imply the document user group (DEC-10)
  MTX-ACL       every ACL row (CSV and XML) matches the matrix; no base.group_public and no
                group-less rows; the log model has no W or D; --g1: every matrix row exists
  MTX-HD07      the portal folder row with W=1 carries "HD-07" in its id or name
  MTX-RULE      every ir.rule matches the matrix (model, groups, perms, domain compared as parsed
                Python expressions); --g1: every matrix rule exists
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
PREFIX = A.ADDON + '.'
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


def _xml_files(root):
    sec = os.path.join(root, SEC_DIR)
    if not os.path.isdir(sec):
        return []
    return [(A.rel(root, os.path.join(sec, n)), os.path.join(sec, n))
            for n in sorted(os.listdir(sec)) if n.endswith('.xml')]


def _all_xml_files(root):
    return [(r, p) for r, p in A.iter_files(os.path.join(root, A.ADDON), exts=('.xml',))
            if not A.is_test_path(r)]


def load_actual(root):
    acl, rules, groups, extra = [], {}, {}, []
    csv_path = os.path.join(root, SEC_DIR, 'ir.model.access.csv')
    csv_rel = SEC_DIR + '/ir.model.access.csv'
    if os.path.exists(csv_path):
        reader = csv.DictReader(io.StringIO(A.read_text(csv_path)))
        for n, row in enumerate(reader, 2):
            acl.append({
                'where': (csv_rel, n), 'id': row.get('id', ''), 'name': row.get('name', ''),
                'model': _strip(row.get('model_id:id', '')), 'group': _strip(row.get('group_id:id') or ''),
                'perms': ''.join('1' if _bool(row.get(k), False) else '0' for k in (
                    'perm_read', 'perm_write', 'perm_create', 'perm_unlink')),
            })
    for relpath, path in _xml_files(root):
        # repo files only (trusted input); stdlib expat does not resolve external entities
        tree = ET.fromstring(A.read_text(path).encode())
        for rec in tree.iter('record'):
            model = rec.get('model')
            rid = _strip(rec.get('id', ''))
            if model == 'ir.model.access':
                mf, gf = _field(rec, 'model_id'), _field(rec, 'group_id')
                perms = ''
                for k in ('perm_read', 'perm_write', 'perm_create', 'perm_unlink'):
                    f = _field(rec, k)
                    perms += '1' if _bool(f.get('eval') if f is not None else None, True) else '0'
                acl.append({'where': (relpath, 1), 'id': rid,
                            'name': (_field(rec, 'name').text if _field(rec, 'name') is not None else '') or '',
                            'model': _strip(mf.get('ref') if mf is not None else ''),
                            'group': _strip(gf.get('ref') if gf is not None else '') or '',
                            'perms': perms})
            elif model == 'ir.rule':
                mf, gf, df = _field(rec, 'model_id'), _field(rec, 'groups'), _field(rec, 'domain_force')
                perms = ''
                for k in ('perm_read', 'perm_write', 'perm_create', 'perm_unlink'):
                    f = _field(rec, k)
                    perms += '1' if _bool(f.get('eval') if f is not None else None, True) else '0'
                domain = '' if df is None else (df.get('eval') or df.text or '')
                rules[rid] = {'where': (relpath, 1), 'model': _strip(mf.get('ref') if mf is not None else ''),
                              'groups': sorted(_strip(g) for g in REF_RE.findall(gf.get('eval', '') if gf is not None else '')),
                              'perms': perms, 'domain': domain}
            elif model in ('res.groups', 'ir.module.category'):
                fields = {f.get('name'): f for f in rec.findall('field')}
                groups[rid] = {'where': (relpath, 1), 'model': model,
                               'category': _strip(fields['category_id'].get('ref')) if 'category_id' in fields else None,
                               'implied': sorted(_strip(r) for r in REF_RE.findall(
                                   fields['implied_ids'].get('eval', ''))) if 'implied_ids' in fields else [],
                               'has_users': 'users' in fields}
    for relpath, path in _all_xml_files(root):
        text = A.read_text(path)
        if '<record' not in text:
            continue
        for rec in ET.fromstring(text.encode()).iter('record'):
            if rec.get('id') == 'base.group_user':
                extra.append((relpath, rec))
    return acl, rules, groups, extra


def model_ref(model):
    return 'model_' + model.replace('.', '_')


def check(root, args):
    out = []
    path = os.path.join(root, MATRIX)
    if not os.path.exists(path):
        raise A.ToolError('%s is missing' % MATRIX)
    mx = A.load_yaml_file(path)
    alias = mx['aliases']
    acl, rules, groups, extra = load_actual(root)

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
    for relpath, rec in extra:
        if user_group in rec.get('id', '') or any(user_group in (f.get('eval') or '') for f in rec.findall('field')):
            out.append(A.Violation(relpath, 1, 'MTX-GROUP', 'base.group_user must not imply %s (DEC-10)' % user_group))
    if user_group in groups and 'base.group_user' != user_group and \
            user_group in (groups.get('base.group_user') or {}).get('implied', []):
        out.append(A.Violation(SEC_DIR + '/groups.xml', 1, 'MTX-GROUP', 'DEC-10 violated'))

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
