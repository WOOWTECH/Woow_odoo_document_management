# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""ROUTE: HTTP route rules R1-R13 (security_and_sweep section 7 with ER-22, ER-27, ER-47.2).

R1  routes only in */controllers/*.py; decorator arguments are literals (max_content_length may
    be the integer expression 95 * 1024 * 1024 or the name max_upload_body)
R2  methods=[...] written out, GET/POST only; upload/unlock/open/close routes are POST
R3  auth written out, 'user' or 'public'; 'public' only in woow_document_management/controllers/share.py
R4  type absent or 'http'            R5  no csrf= argument at all
R6  path parameters only <int:name>  R7  no cors=, readonly=True, save_session=False
R8  upload routes set max_content_length (= 99,614,720, or max_upload_body)
R9  no path-less @route() overrides; no inherited core routes: controller classes extend only
    http.Controller, CustomerPortal or this module's own controller classes; in a class that
    extends CustomerPortal or one of this module's controllers every public method carries its
    own @route (an undecorated override inherits the parent's routing, http.py
    _generate_routing_rules); core CustomerPortal route methods are never overridden
R10 no /web/, /mail/, /portal/, /website/, /json/ prefix
R11 auth='public' functions start with self._share_entry(...); the GET /woow-docs/s landing
    instead must not use .env[...] or bind request.env / request.session to a name, call
    .touch( / _log_event( / create( / write( / unlink(, or change request.session (item or
    attribute assignment, del, update/pop/setdefault/clear/popitem)
R1 also covers the decorator form: odoo.http.route under any import alias counts, a route
    object may only be used directly as @<route>(...), and controller functions carry no other
    decorator than staticmethod / classmethod.
R12 every route equals a row of tools/routes.lock.json (--g1: the two sets are equal)
R13 website=True only in controllers/portal.py

--emit-entry-points prints the entry point list (one per decorated route + the fixed
CustomerPortal._prepare_home_portal_values entry; 17 for the final code). --from-lock builds it
from tools/routes.lock.json instead of the code. --target compares the list with a private
target YAML file (context.entry_points).
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astlib as A  # noqa: E402

PUBLIC_FILE = 'woow_document_management/controllers/share.py'
PORTAL_FILE = 'woow_document_management/controllers/portal.py'
LOCK_FILE = 'tools/routes.lock.json'
LANDING = '/woow-docs/s'
MAX_BODY = 95 * 1024 * 1024
FORBIDDEN_PREFIXES = ('/web/', '/mail/', '/portal/', '/website/', '/json/')
POST_WORDS = ('upload', 'unlock', 'open', 'close')
PARAM_RE = re.compile(r'<([^>]*)>')
HOME_ENTRY = {
    'route': 'CustomerPortal._prepare_home_portal_values (core /my/counters is NOT overridden)',
    'auth': 'user',
    'file': PORTAL_FILE,
}


CORE_PORTAL_ROUTE_METHODS = frozenset({
    # route methods of odoo.addons.portal.controllers.portal.CustomerPortal (ref odoo-18)
    'home', 'account', 'security', 'counters', 'deactivate_account', 'attachment_add',
    'attachment_remove',
})
ALLOWED_PLAIN_DECORATORS = ('staticmethod', 'classmethod')
SESSION_MUTATORS = ('update', 'pop', 'setdefault', 'clear', 'popitem')
SESSION_SLOTS = ('can_save', 'is_dirty', 'is_new', 'should_rotate', 'sid')
LANDING_WRITE_CALLS = ('touch', '_log_event', 'create', 'write', 'unlink')


def _is_route_decorator(dec, aliases=None):
    if not isinstance(dec, ast.Call):
        return False
    mods, routes = (aliases or ({'http', 'odoo.http'}, {'route'}))[:2]
    return A.is_route_func(dec.func, mods, routes)


def _in_controllers(relpath):
    parts = relpath.split('/')
    return len(parts) >= 2 and parts[-2] == 'controllers'


def _controller_class_names(root):
    names = set()
    for relpath, _abspath in A.iter_files(root, exts=('.py',), include_tests=False):
        if _in_controllers(relpath):
            for node in ast.walk(A.parse_py(root, relpath).tree):
                if isinstance(node, ast.ClassDef):
                    names.add(node.name)
    return names


def structural(root):
    """R1 / R9 checks that do not depend on a single route decorator."""
    out = []
    local_classes = _controller_class_names(root)
    for relpath, _abspath in A.iter_files(root, exts=('.py',), include_tests=False):
        if relpath.startswith('tools/'):
            continue
        src = A.parse_py(root, relpath)
        mods, routes, _requests, ctrls = A.http_aliases(src.tree)
        decorator_funcs = set()
        for node in ast.walk(src.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and A.is_route_func(dec.func, mods, routes):
                        decorator_funcs.add(id(dec.func))
        for node in ast.walk(src.tree):
            # a route object used anywhere but directly as @<route>(...)
            if A.is_route_reference(node, mods, routes) and id(node) not in decorator_funcs:
                out.append(A.Violation(relpath, node.lineno, 'R1',
                                       'odoo.http.route may only be used directly as a decorator'))
            elif isinstance(node, ast.Call) and A.call_name(node) == 'getattr' and len(node.args) > 1 \
                    and A.const_str(node.args[1]) == 'route':
                out.append(A.Violation(relpath, node.lineno, 'R1', "getattr(..., 'route') is not allowed"))
        if not _in_controllers(relpath):
            continue
        for node in ast.walk(src.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and A.is_route_func(dec.func, mods, routes):
                        continue
                    if isinstance(dec, ast.Name) and dec.id in ALLOWED_PLAIN_DECORATORS:
                        continue
                    out.append(A.Violation(relpath, dec.lineno, 'R1',
                                           'controller functions take no decorator but @<route>(...) '
                                           '(got @%s)' % A.dotted(dec)))
            if not isinstance(node, ast.ClassDef):
                continue
            kinds = []
            for base in node.bases:
                text = A.dotted(base)
                if text in ('NamedTuple', 'typing.NamedTuple'):
                    kinds.append('plain')
                elif (isinstance(base, ast.Name) and base.id in ctrls) or \
                        (isinstance(base, ast.Attribute) and base.attr == 'Controller'
                         and A.dotted(base.value) in mods):
                    kinds.append('controller')
                elif text == 'CustomerPortal' or text.endswith('.portal.CustomerPortal'):
                    kinds.append('portal')
                elif isinstance(base, ast.Name) and base.id in local_classes:
                    kinds.append('local')
                else:
                    out.append(A.Violation(relpath, node.lineno, 'R9',
                                           'class %s extends %s: controllers extend only http.Controller, '
                                           'CustomerPortal or this module\'s own controllers' % (node.name, text)))
                    kinds.append('core')
            if not set(kinds) - {'plain', 'controller'}:
                continue    # nothing to inherit routes from
            for stmt in node.body:
                if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                decorated = any(isinstance(d, ast.Call) and A.is_route_func(d.func, mods, routes)
                                for d in stmt.decorator_list)
                if stmt.name in CORE_PORTAL_ROUTE_METHODS:
                    out.append(A.Violation(relpath, stmt.lineno, 'R9',
                                           '%s.%s overrides a core portal route' % (node.name, stmt.name)))
                elif not stmt.name.startswith('_') and not decorated:
                    out.append(A.Violation(relpath, stmt.lineno, 'R9',
                                           'public controller method %s.%s has no @route of its own '
                                           '(an undecorated override inherits the parent route)'
                                           % (node.name, stmt.name)))
    return out


def _literal(node):
    try:
        return True, ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return False, None


def _int_expr(node):
    """Value of an integer constant expression like 95 * 1024 * 1024, else None."""
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left, right = _int_expr(node.left), _int_expr(node.right)
        if left is not None and right is not None:
            return left * right
    return None


def extract(root):
    """[(relpath, func_node, decorator, info, problems)] for every decorated route function."""
    found = []
    for relpath, _abspath in A.iter_files(root, exts=('.py',), include_tests=False):
        if relpath.startswith('tools/'):
            continue
        src = A.parse_py(root, relpath)
        aliases = A.http_aliases(src.tree)
        for node in ast.walk(src.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if _is_route_decorator(dec, aliases):
                    found.append((relpath, src, node, dec))
    return found


def _info(relpath, func, dec):
    problems = []
    routes = []
    if dec.args:
        ok, value = _literal(dec.args[0])
        if not ok:
            problems.append(('R1', 'route path must be a literal'))
        elif isinstance(value, str):
            routes = [value]
        elif isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
            routes = list(value)
        else:
            problems.append(('R1', 'route path must be a string or a list of strings'))
        if len(dec.args) > 1:
            problems.append(('R1', 'only one positional argument is allowed'))
    kws = {}
    for keyword in dec.keywords:
        if keyword.arg is None:
            problems.append(('R1', '**kwargs in a route decorator'))
            continue
        if keyword.arg == 'max_content_length':
            value = _int_expr(keyword.value)
            if value is not None:
                kws[keyword.arg] = value
            elif (isinstance(keyword.value, ast.Name) and keyword.value.id == 'max_upload_body') or (
                    isinstance(keyword.value, ast.Attribute) and keyword.value.attr == 'max_upload_body'):
                kws[keyword.arg] = MAX_BODY
            else:
                problems.append(('R1', 'max_content_length must be 95 * 1024 * 1024 or max_upload_body'))
            continue
        ok, value = _literal(keyword.value)
        if not ok:
            problems.append(('R1', '%s= must be a literal' % keyword.arg))
            continue
        kws[keyword.arg] = value
    return {
        'file': relpath, 'func': func.name, 'routes': routes,
        'methods': kws.get('methods'), 'auth': kws.get('auth'), 'type': kws.get('type', 'http'),
        'website': bool(kws.get('website', False)), 'kws': kws,
    }, problems


def _is_request_attr(node, attr):
    return isinstance(node, ast.Attribute) and node.attr == attr and A.req_dotted(node) == 'request.' + attr


def _is_session_value(node):
    """request.session[...] or request.session.get(...) / .setdefault(...)."""
    if isinstance(node, ast.Subscript):
        return _is_request_attr(node.value, 'session')
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr in ('get', 'setdefault'):
        return _is_request_attr(node.func.value, 'session')
    return False


def _r11_landing(relpath, func, out):
    def bad(node, message):
        out.append(A.Violation(relpath, node.lineno, 'R11', 'landing page must not ' + message))

    for node in ast.walk(func):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) \
                and node.value.attr == 'env':
            bad(node, 'use .env[...] (no woow.* model, no data access)')
        if isinstance(node, ast.Call):
            name = A.call_name(node)
            if name in LANDING_WRITE_CALLS:
                bad(node, 'call %s(' % name)
            if name in SESSION_MUTATORS and isinstance(node.func, ast.Attribute) and (
                    _is_request_attr(node.func.value, 'session') or _is_session_value(node.func.value)):
                bad(node, 'change request.session (%s()' % name)
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
            value = node.value
            if value is not None and (_is_request_attr(value, 'env') or _is_request_attr(value, 'session')):
                bad(node, 'bind request.env / request.session to a name')
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript) and A.req_dotted(target).startswith('request.session'):
                    bad(node, 'assign request.session[...]')
                elif isinstance(target, ast.Attribute) and _is_request_attr(target.value, 'session'):
                    bad(node, 'assign request.session.%s' % target.attr)


def _r11_entry(stmt):
    if isinstance(stmt, ast.Assign):
        value = stmt.value
    elif isinstance(stmt, ast.Expr):
        value = stmt.value
    else:
        return False
    return (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
            and value.func.attr == '_share_entry' and isinstance(value.func.value, ast.Name)
            and value.func.value.id == 'self')


def check_route(relpath, func, dec, info, problems, out):
    for rule, message in problems:
        out.append(A.Violation(relpath, dec.lineno, rule, message))
    kws = info['kws']
    if not _in_controllers(relpath):
        out.append(A.Violation(relpath, dec.lineno, 'R1', 'routes are only allowed in */controllers/*.py'))
    if not info['routes']:
        out.append(A.Violation(relpath, dec.lineno, 'R9', 'path-less @route() override is not allowed'))
    methods = info['methods']
    if not isinstance(methods, (list, tuple)) or not methods:
        out.append(A.Violation(relpath, dec.lineno, 'R2', 'methods=[...] must be written out'))
    else:
        if any(m not in ('GET', 'POST') for m in methods):
            out.append(A.Violation(relpath, dec.lineno, 'R2', 'only GET and POST are allowed'))
        text = ' '.join([func.name.lower()] + [r.lower() for r in info['routes']])
        if any(w in text for w in POST_WORDS) and list(methods) != ['POST']:
            out.append(A.Violation(relpath, dec.lineno, 'R2',
                                   'upload/unlock/open/close routes must be POST only'))
    auth = info['auth']
    if auth not in ('user', 'public'):
        out.append(A.Violation(relpath, dec.lineno, 'R3',
                               "auth must be written out as 'user' or 'public' (got %r)" % (auth,)))
    if auth == 'public' and relpath != PUBLIC_FILE:
        out.append(A.Violation(relpath, dec.lineno, 'R3', "auth='public' only in %s" % PUBLIC_FILE))
    if info['type'] != 'http':
        out.append(A.Violation(relpath, dec.lineno, 'R4', "type must be absent or 'http'"))
    if 'csrf' in kws:
        out.append(A.Violation(relpath, dec.lineno, 'R5', 'csrf= is not allowed'))
    for route in info['routes']:
        for param in PARAM_RE.findall(route):
            if not re.fullmatch(r'int:[a-z_][a-z0-9_]*', param):
                out.append(A.Violation(relpath, dec.lineno, 'R6',
                                       'path parameter <%s> must be <int:name>' % param))
        if route.startswith(FORBIDDEN_PREFIXES):
            out.append(A.Violation(relpath, dec.lineno, 'R10', 'route %s shadows a core prefix' % route))
    if 'cors' in kws:
        out.append(A.Violation(relpath, dec.lineno, 'R7', 'cors= is not allowed'))
    if 'readonly' in kws and kws['readonly'] is not False:
        out.append(A.Violation(relpath, dec.lineno, 'R7', 'readonly=True is not allowed'))
    if 'save_session' in kws and kws['save_session'] is not True:
        out.append(A.Violation(relpath, dec.lineno, 'R7', 'save_session=False is not allowed'))
    is_upload = 'upload' in func.name.lower() or any('upload' in r for r in info['routes'])
    if is_upload and kws.get('max_content_length') != MAX_BODY:
        out.append(A.Violation(relpath, dec.lineno, 'R8',
                               'upload routes need max_content_length=95 * 1024 * 1024'))
    if info['website'] and relpath != PORTAL_FILE:
        out.append(A.Violation(relpath, dec.lineno, 'R13', 'website=True only in %s' % PORTAL_FILE))
    if auth == 'public':
        if info['routes'] == [LANDING]:
            _r11_landing(relpath, func, out)
        elif not _r11_entry(A.first_statement(func)):
            out.append(A.Violation(relpath, func.lineno, 'R11',
                                   'public route must start with self._share_entry(...)'))


def lock_key(row):
    return json.dumps({k: row.get(k) for k in ('file', 'func', 'routes', 'methods', 'auth', 'type',
                                                'website')}, sort_keys=True)


def load_lock(root):
    path = os.path.join(root, LOCK_FILE)
    if not os.path.exists(path):
        raise A.ToolError('%s is missing' % LOCK_FILE)
    rows = json.loads(A.read_text(path))
    if not isinstance(rows, list):
        raise A.ToolError('%s must be a JSON list' % LOCK_FILE)
    return rows


def entry_points(rows):
    eps = []
    for row in rows:
        eps.append({'route': ', '.join(row['routes']), 'methods': list(row['methods'] or []),
                    'auth': row['auth'], 'type': row.get('type', 'http'), 'file': row['file'],
                    'func': row['func']})
    eps.append(dict(HOME_ENTRY))
    return eps


def _ep_key(ep):
    return (ep.get('route'), tuple(ep.get('methods') or ()), ep.get('auth'), ep.get('file'))


def compare_target(eps, target_path):
    data = A.load_yaml_file(target_path)
    target = (data or {}).get('context', {}).get('entry_points') or []
    want = sorted(_ep_key(ep) for ep in target)
    got = sorted(_ep_key(ep) for ep in eps)
    return want == got, want, got


def check(root, args):
    out = structural(root)
    lock = load_lock(root)
    lock_keys = {lock_key(row) for row in lock}
    infos = []
    for relpath, src, func, dec in extract(root):
        info, problems = _info(relpath, func, dec)
        check_route(relpath, func, dec, info, problems, out)
        infos.append(info)
        if lock_key(info) not in lock_keys:
            out.append(A.Violation(relpath, dec.lineno, 'R12',
                                   'route %s is not in %s' % (info['func'], LOCK_FILE)))
    if args.g1:
        code_keys = {lock_key(info) for info in infos}
        for row in lock:
            if lock_key(row) not in code_keys:
                out.append(A.Violation(LOCK_FILE, 1, 'R12',
                                       'locked route %s is not implemented' % row.get('func')))
        if len(infos) + 1 != 17:
            out.append(A.Violation(LOCK_FILE, 1, 'R12',
                                   'expected 17 entry points, found %d' % (len(infos) + 1)))
    if getattr(args, 'target', None):
        same, want, got = compare_target(entry_points(lock if args.from_lock else infos), args.target)
        if not same:
            for key in sorted(set(want) ^ set(got)):
                side = 'only in target' if key in want else 'only in %s' % ('lock' if args.from_lock else 'code')
                out.append(A.Violation(LOCK_FILE, 1, 'R12', 'entry point %s (%s)' % (key[0], side)))
    if getattr(args, 'emit_entry_points', False):
        rows = lock if args.from_lock else infos
        print(json.dumps(entry_points(rows), indent=2))
        return None if not out else out
    return out


def _extra(parser):
    parser.add_argument('--emit-entry-points', action='store_true',
                        help='print the entry point list as JSON')
    parser.add_argument('--from-lock', action='store_true',
                        help='with --emit-entry-points: build the list from routes.lock.json')
    parser.add_argument('--target', default=None,
                        help='private target YAML whose context.entry_points must match')


def main(argv=None):
    return A.run_tool(check, __doc__.splitlines()[0], argv, extra=_extra)


if __name__ == '__main__':
    sys.exit(main())
