# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""FORB: forbidden constructs (architecture 6.2, security_and_sweep 8, ER-04/22/29/35).

Python (AST), non-test code unless noted:
  F-SUDO-BROWSE   .sudo().browse( / sudo() search or read with request values
  F-ROUTE         route auth='none', csrf=False, type='json' (odoo.http.route under any alias)
  F-MAIL-TEMPLATE create/write/unlink on mail.template
  F-USERS         creating res.users; writing groups_id
  F-MARKUP        Markup( on non-literals; sanitize=False / sanitize_attributes=False
  F-PATH          open( / os.path.join( / tempfile with non-constant paths
  F-REDIRECT      redirect( / redirect_query( (request, werkzeug, odoo.http, any alias) and a
                  'Location' header (dict key, tuple, headers[...] =) with anything but a literal
                  or a %d-only local path
  F-HOST          request.httprequest.host / host_url, HTTP_HOST, X-Forwarded-Host
  F-SESSION       session uid / pre_uid / pre_login / login / session_token as attribute,
                  item (session['uid']) or .get/.pop/.setdefault key; authenticate(, finalize(
  F-HASH          hashlib.sha1 / md5
  F-NUMCAST       float( / bool( / complex( of request values
  F-TOKEN-API     limited_field_access_token, _get_limited_field_access_token, generate_access_token
  F-LOG           _logger calls carrying token/password/email/name-like values; print(
  F-HTTPEXC       controllers: raise request.not_found / NotFound / werkzeug.exceptions.*,
                  abort(, return request.not_found(  (T-STAT-23, ER-29)
  F-FORCE-SEND    force_send=True (tests included; T-STAT-25, ER-35)
  F-TOUCH         .touch( calls (T-STAT-21, R2b-02)
  F-SESSION-MUT   in-place mutation of request.session values: request.session[k][..] = ,
                  request.session.get(k)[..] = , .pop/.update/... on such a value, attribute
                  assignment request.session.x = , request.session.update/pop/setdefault/clear(
                  (http.request / odoo.http.request count as request) (T-STAT-21, R2b-03)
  F-TEST-PASSWORD password literals in tests
Text:
  F-QWEB-RAW      t-raw in XML                      F-COMMERCIAL commercial_partner_id in
  F-JS-SINK       innerHTML, outerHTML, insertAdjacentHTML, markup(, document.write, eval(,
                  new Function(, window.open( without noopener         security/*.xml, controllers,
  F-BLANK         target="_blank" without rel noopener (XML, JS)        models (T-STAT-24, ER-04)
  F-EICAR         the EICAR test string
  F-STUB          WOOW-STUB markers in non-test files (--g1 only; T-STAT-16)
tools/ and tools/tests/fixtures/ are not scanned (they spell the patterns out).
"""
from __future__ import annotations

import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astlib as A  # noqa: E402

REQUEST_NAMES = {'kw', 'kwargs', 'post', 'params', 'args'}
LOG_LEVELS = {'debug', 'info', 'warning', 'warn', 'error', 'exception', 'critical'}
LOG_SENSITIVE = re.compile(r'(token|passw|secret|email|login|(^|_)name$)', re.I)
HTTP_EXC = {'NotFound', 'Forbidden', 'BadRequest', 'Unauthorized', 'MethodNotAllowed',
            'InternalServerError', 'TooManyRequests', 'ServiceUnavailable', 'Gone', 'HTTPException'}
EICAR = 'EICAR-STANDARD-ANTIVIRUS-' + 'TEST-FILE'
JS_SINKS = [
    (re.compile(r'\.innerHTML\b'), 'innerHTML'),
    (re.compile(r'\.outerHTML\b'), 'outerHTML'),
    (re.compile(r'insertAdjacentHTML\s*\('), 'insertAdjacentHTML('),
    (re.compile(r'(?<![\w.])markup\s*\('), 'markup('),
    (re.compile(r'document\.write\s*\('), 'document.write('),
    (re.compile(r'(?<![\w.])eval\s*\('), 'eval('),
    (re.compile(r'new\s+Function\s*\('), 'new Function('),
]
BLANK_RE = re.compile(r'target\s*=\s*["\']_blank["\']')
STUB_MARK = 'WOOW-' + 'STUB'
CODE_EXTS = ('.py', '.xml', '.js', '.scss', '.csv', '.sh')


def _refs_request(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and (sub.id in REQUEST_NAMES or sub.id == 'request'):
            return True
    return False


def _chain_has_sudo(node):
    cur = node
    while isinstance(cur, (ast.Attribute, ast.Call, ast.Subscript)):
        if isinstance(cur, ast.Call):
            if A.call_name(cur) == 'sudo' and not (cur.args and A.is_const(cur.args[0], False)):
                return True
            cur = cur.func
        else:
            cur = cur.value
    return False


SESSION_KEYS = ('uid', 'pre_uid', 'pre_login', 'login', 'session_token')
SESSION_SLOTS = ('can_save', 'is_dirty', 'is_new', 'should_rotate', 'sid')
SESSION_MUTATORS = ('pop', 'update', 'setdefault', 'clear', 'append', 'popitem', 'extend', 'remove',
                    'add', 'discard', 'insert')


def _is_session(node):
    """request.session (also http.request.session, odoo.http.request.session)."""
    return isinstance(node, ast.Attribute) and node.attr == 'session' and A.req_dotted(node) == 'request.session'


def _is_any_session(node):
    """<anything>.session, for the uid / pre_uid key rules."""
    return isinstance(node, ast.Attribute) and node.attr == 'session'


def _is_session_value(node):
    """A value stored in the session: request.session[...] or request.session.get/setdefault(...)."""
    if isinstance(node, ast.Subscript):
        return _is_session(node.value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr in ('get', 'setdefault'):
        return _is_session(node.func.value)
    return False


def _redirect_ok(arg):
    if A.const_str(arg) is not None:
        return True
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mod):
        fmt = A.const_str(arg.left)
        if fmt is not None:
            specs = re.findall(r'%.', fmt.replace('%%', ''))
            return bool(specs) and all(s == '%d' for s in specs)
    return False


def check_python(src, out):
    path = src.relpath
    is_test = A.is_test_path(path)
    in_controllers = '/controllers/' in '/' + path
    mods, routes, _requests, _ctrls = A.http_aliases(src.tree)
    redirect_names = {'redirect', 'redirect_query'}
    for node in ast.walk(src.tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in ('redirect', 'redirect_query') and alias.asname:
                    redirect_names.add(alias.asname)
    for node in ast.walk(src.tree):
        line = getattr(node, 'lineno', 1)
        if isinstance(node, ast.Call):
            name = A.call_name(node)
            func = node.func
            for keyword in node.keywords:
                if keyword.arg == 'force_send' and A.is_const(keyword.value, True):
                    out.append(A.Violation(path, line, 'F-FORCE-SEND', 'force_send=True is not allowed'))
            if is_test:
                for keyword in node.keywords:
                    if keyword.arg and 'password' in keyword.arg and A.const_str(keyword.value):
                        out.append(A.Violation(path, line, 'F-TEST-PASSWORD',
                                               'use a generated password (self.pw), not a literal'))
                continue
            if name == 'browse' and isinstance(func, ast.Attribute) and _chain_has_sudo(func.value):
                out.append(A.Violation(path, line, 'F-SUDO-BROWSE', 'sudo().browse( is not allowed'))
            if name in ('search', 'search_count', 'search_read', 'read', 'read_group') \
                    and isinstance(func, ast.Attribute) and _chain_has_sudo(func.value) \
                    and any(_refs_request(a) for a in list(node.args) + [k.value for k in node.keywords]):
                out.append(A.Violation(path, line, 'F-SUDO-BROWSE',
                                       'sudo() search/read with request values'))
            if A.is_route_func(func, mods, routes):
                for keyword in node.keywords:
                    value = keyword.value
                    if (keyword.arg == 'auth' and A.const_str(value) == 'none') or \
                            (keyword.arg == 'csrf' and A.is_const(value, False)) or \
                            (keyword.arg == 'type' and A.const_str(value) == 'json'):
                        out.append(A.Violation(path, line, 'F-ROUTE',
                                               '%s=%s is not allowed' % (keyword.arg, A.dotted(value))))
            if name in ('create', 'write', 'unlink') and isinstance(func, ast.Attribute):
                recv = A.dotted(func.value)
                if 'mail.template' in recv or re.search(r'(^|[._])templates?$', recv):
                    out.append(A.Violation(path, line, 'F-MAIL-TEMPLATE',
                                           'mail.template must never be written'))
                if name == 'create' and "'res.users'" in recv.replace('"', "'"):
                    out.append(A.Violation(path, line, 'F-USERS', 'res.users must not be created'))
            if name in ('_create_user', '_signup_create_user'):
                out.append(A.Violation(path, line, 'F-USERS', 'res.users must not be created'))
            if name == 'Markup' and node.args and A.const_str(node.args[0]) is None:
                out.append(A.Violation(path, line, 'F-MARKUP', 'Markup( on a non-literal'))
            for keyword in node.keywords:
                if keyword.arg in ('sanitize', 'sanitize_attributes', 'sanitize_tags') \
                        and A.is_const(keyword.value, False):
                    out.append(A.Violation(path, line, 'F-MARKUP', '%s=False is not allowed' % keyword.arg))
            if name == 'open' and isinstance(func, ast.Name) and node.args \
                    and A.const_str(node.args[0]) is None:
                out.append(A.Violation(path, line, 'F-PATH', 'open( with a non-constant path'))
            if name == 'join' and A.dotted(func) == 'os.path.join' \
                    and any(A.const_str(a) is None for a in node.args):
                out.append(A.Violation(path, line, 'F-PATH', 'os.path.join( with non-constant parts'))
            if isinstance(func, ast.Attribute) and A.dotted(func.value) == 'tempfile':
                for keyword in node.keywords:
                    if keyword.arg in ('dir', 'prefix', 'suffix') and A.const_str(keyword.value) is None:
                        out.append(A.Violation(path, line, 'F-PATH', 'tempfile with a non-constant %s' % keyword.arg))
            if name in redirect_names:
                target = node.args[0] if node.args else (A.kw(node, 'location') or A.kw(node, 'url'))
                if target is None or not _redirect_ok(target):
                    out.append(A.Violation(path, line, 'F-REDIRECT',
                                           '%s( needs a literal or a %%d-only local path' % name))
            if name in ('authenticate', 'finalize'):
                out.append(A.Violation(path, line, 'F-SESSION', '%s( is not allowed' % name))
            if name in ('limited_field_access_token', '_get_limited_field_access_token',
                        'generate_access_token'):
                out.append(A.Violation(path, line, 'F-TOKEN-API', '%s( is not allowed' % name))
            if name in ('sha1', 'md5') or (name == 'new' and A.dotted(func) == 'hashlib.new'
                                            and node.args and A.const_str(node.args[0]) in ('sha1', 'md5')):
                out.append(A.Violation(path, line, 'F-HASH', 'sha1/md5 are not allowed'))
            if name in ('float', 'bool', 'complex') and isinstance(func, ast.Name) \
                    and any(_refs_request(a) for a in node.args):
                out.append(A.Violation(path, line, 'F-NUMCAST',
                                       '%s( on a request value; use int() with a range check' % name))
            if name == 'print' and isinstance(func, ast.Name):
                out.append(A.Violation(path, line, 'F-LOG', 'print( is not allowed'))
            if name in LOG_LEVELS and isinstance(func, ast.Attribute) \
                    and A.dotted(func.value).endswith('_logger'):
                for arg in node.args:
                    text = A.const_str(arg)
                    if text is not None and re.search(r'token|passw|secret|e-?mail', text, re.I):
                        out.append(A.Violation(path, line, 'F-LOG', 'log text mentions a secret or e-mail'))
                    for sub in ast.walk(arg):
                        ident = sub.id if isinstance(sub, ast.Name) else (
                            sub.attr if isinstance(sub, ast.Attribute) else None)
                        if ident and LOG_SENSITIVE.search(ident):
                            out.append(A.Violation(path, line, 'F-LOG',
                                                   'log argument %s may carry personal data or a secret' % ident))
                            break
            if name == 'touch':
                out.append(A.Violation(path, line, 'F-TOUCH', '.touch( is not allowed (R2b-02)'))
            if name in SESSION_MUTATORS and isinstance(func, ast.Attribute) and (
                    _is_session_value(func.value) or
                    (_is_session(func.value) and name in ('pop', 'update', 'setdefault', 'clear', 'popitem'))):
                out.append(A.Violation(path, line, 'F-SESSION-MUT',
                                       'copy the session value, change it, then reassign it (R2b-03)'))
            if name in ('get', 'pop', 'setdefault') and isinstance(func, ast.Attribute) \
                    and _is_any_session(func.value) and node.args \
                    and A.const_str(node.args[0]) in SESSION_KEYS:
                out.append(A.Violation(path, line, 'F-SESSION',
                                       'session %r must not be touched' % A.const_str(node.args[0])))
            if in_controllers and name == 'abort':
                out.append(A.Violation(path, line, 'F-HTTPEXC', 'abort( in a controller; return a Response'))
        elif is_test:
            if isinstance(node, ast.Assign) and A.const_str(node.value) is not None:
                for target in node.targets:
                    tname = target.id if isinstance(target, ast.Name) else (
                        target.attr if isinstance(target, ast.Attribute) else '')
                    if re.search(r'(password|passwd|^pw$)', tname or '', re.I):
                        out.append(A.Violation(path, line, 'F-TEST-PASSWORD',
                                               'use a generated password, not a literal'))
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if key is not None and 'password' in (A.const_str(key) or '') and A.const_str(value):
                        out.append(A.Violation(path, line, 'F-TEST-PASSWORD',
                                               'use a generated password, not a literal'))
        elif isinstance(node, ast.Attribute):
            if node.attr in ('host', 'host_url') and A.dotted(node.value).endswith('httprequest'):
                out.append(A.Violation(path, line, 'F-HOST', 'request host is untrusted'))
            elif node.attr == 'host_url':
                out.append(A.Violation(path, line, 'F-HOST', 'host_url is untrusted'))
            elif node.attr in ('uid', 'login', 'session_token') and A.dotted(node.value).endswith('session'):
                out.append(A.Violation(path, line, 'F-SESSION', 'session.%s must not be touched' % node.attr))
            elif node.attr in ('pre_uid', 'pre_login'):
                out.append(A.Violation(path, line, 'F-SESSION', '%s must not be touched' % node.attr))
            if node.attr in ('sha1', 'md5') and A.dotted(node.value) == 'hashlib' \
                    and not isinstance(A.parent(node), ast.Call):
                out.append(A.Violation(path, line, 'F-HASH', 'sha1/md5 are not allowed'))
        elif isinstance(node, ast.Subscript) and _is_any_session(node.value) \
                and A.const_str(node.slice) in SESSION_KEYS:
            out.append(A.Violation(path, line, 'F-SESSION',
                                   'session[%r] must not be touched' % A.const_str(node.slice)))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            low = node.value.lower()
            if 'http_host' in low or 'x-forwarded-host' in low:
                out.append(A.Violation(path, line, 'F-HOST', 'Host headers are untrusted'))
        elif isinstance(node, ast.ImportFrom) and node.module == 'hashlib':
            if any(alias.name in ('sha1', 'md5') for alias in node.names):
                out.append(A.Violation(path, line, 'F-HASH', 'sha1/md5 are not allowed'))
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript) and (
                        _is_session_value(target.value) or
                        (isinstance(target.value, ast.Subscript) and
                         A.req_dotted(target.value).startswith('request.session'))):
                    out.append(A.Violation(path, line, 'F-SESSION-MUT',
                                           'copy the session value, change it, then reassign it (R2b-03)'))
                if isinstance(target, ast.Attribute) and _is_session(target.value) \
                        and target.attr not in SESSION_SLOTS:
                    out.append(A.Violation(path, line, 'F-SESSION-MUT',
                                           'request.session.%s = writes a session key; assign '
                                           'request.session[KEY] = <copied dict> (R2b-03)' % target.attr))
                if isinstance(target, ast.Subscript) and _is_any_session(target.value) \
                        and A.const_str(target.slice) in SESSION_KEYS:
                    out.append(A.Violation(path, line, 'F-SESSION',
                                           'session[%r] must not be set' % A.const_str(target.slice)))
                if isinstance(target, ast.Subscript) and (A.const_str(target.slice) or '').lower() == 'location' \
                        and A.dotted(target.value).endswith('headers') and isinstance(node, ast.Assign) \
                        and not _redirect_ok(node.value):
                    out.append(A.Violation(path, line, 'F-REDIRECT',
                                           'Location header needs a literal or a %d-only local path'))
                if isinstance(target, ast.Attribute) and target.attr == 'groups_id':
                    out.append(A.Violation(path, line, 'F-USERS', 'groups_id must not be written'))
                if isinstance(target, ast.Attribute) and target.attr in ('uid', 'pre_uid') \
                        and A.dotted(target.value).endswith('session'):
                    out.append(A.Violation(path, line, 'F-SESSION', 'session uid must not be set'))
        elif isinstance(node, ast.Tuple):
            if len(node.elts) == 2 and (A.const_str(node.elts[0]) or '').lower() == 'location' \
                    and not _redirect_ok(node.elts[1]):
                out.append(A.Violation(path, line, 'F-REDIRECT',
                                       'Location header needs a literal or a %d-only local path'))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if key is not None and A.const_str(key) == 'groups_id':
                    out.append(A.Violation(path, line, 'F-USERS', 'groups_id must not be written'))
                if key is not None and (A.const_str(key) or '').lower() == 'location' \
                        and not _redirect_ok(value):
                    out.append(A.Violation(path, line, 'F-REDIRECT',
                                           'Location header needs a literal or a %d-only local path'))
        if in_controllers and not is_test and isinstance(node, (ast.Raise, ast.Return)):
            value = node.exc if isinstance(node, ast.Raise) else node.value
            if value is None:
                continue
            target = value.func if isinstance(value, ast.Call) else value
            text = A.req_dotted(target)
            bad = text == 'request.not_found' or text.startswith('werkzeug.exceptions.')
            if isinstance(node, ast.Raise):
                bad = bad or (isinstance(target, ast.Name) and target.id in HTTP_EXC) or \
                    (isinstance(target, ast.Attribute) and target.attr in HTTP_EXC)
            if bad:
                out.append(A.Violation(path, line, 'F-HTTPEXC',
                                       '%s %s in a controller; return a streaming Response (ER-29)'
                                       % ('raise' if isinstance(node, ast.Raise) else 'return', text)))


def check_text(relpath, text, args, out):
    lines = text.split('\n')
    is_test = A.is_test_path(relpath)
    ext = os.path.splitext(relpath)[1]
    if EICAR in text:
        out.append(A.Violation(relpath, 1, 'F-EICAR', 'EICAR test string'))
    if args.g1 and not is_test and ext in CODE_EXTS and STUB_MARK in text:
        for n, line in enumerate(lines, 1):
            if STUB_MARK in line:
                out.append(A.Violation(relpath, n, 'F-STUB', 'day-0 stub marker left (T-STAT-16)'))
    parts = relpath.split('/')
    if 'commercial_partner_id' in text and not is_test and (
            (ext == '.xml' and 'security' in parts) or
            (ext == '.py' and ('controllers' in parts or 'models' in parts))):
        for n, line in enumerate(lines, 1):
            if 'commercial_partner_id' in line:
                out.append(A.Violation(relpath, n, 'F-COMMERCIAL',
                                       'portal access matches user.partner_id only (ER-04)'))
    if ext == '.xml':
        for n, line in enumerate(lines, 1):
            if re.search(r'\bt-raw\s*=', line):
                out.append(A.Violation(relpath, n, 'F-QWEB-RAW', 't-raw is not allowed; use t-out'))
    if ext in ('.xml', '.js'):
        for n, line in enumerate(lines, 1):
            if BLANK_RE.search(line) and 'noopener' not in line:
                out.append(A.Violation(relpath, n, 'F-BLANK', 'target="_blank" needs rel="noopener ..."'))
    if ext == '.js':
        for n, line in enumerate(lines, 1):
            code = line.split('//', 1)[0] if not line.strip().startswith('*') else ''
            for rx, label in JS_SINKS:
                if rx.search(code):
                    out.append(A.Violation(relpath, n, 'F-JS-SINK', '%s is not allowed' % label))
            if re.search(r'window\.open\s*\(', code) and 'noopener' not in code:
                out.append(A.Violation(relpath, n, 'F-JS-SINK', 'window.open( without noopener'))


def check(root, args):
    out = []
    for relpath, abspath in A.iter_files(root):
        if relpath.startswith('tools/') or relpath.startswith('.git/'):
            continue
        text = A.try_read_text(abspath)
        if text is None:
            continue
        check_text(relpath, text, args, out)
        if relpath.endswith('.py'):
            check_python(A.parse_py(root, relpath), out)
    return out


def main(argv=None):
    return A.run_tool(check, __doc__.splitlines()[0], argv)


if __name__ == '__main__':
    sys.exit(main())
