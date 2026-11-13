# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Self-tests of the 8 static tools (T-STAT-01..13, 16..25; IF-STATIC).

Run with ``python3 -I -m unittest discover tools/tests`` from the repository root. Standard
library only; fixtures are synthetic and stored as ``*.fixture`` files (see fixtures/README.md).
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(TOOLS)
FIXTURES = os.path.join(TOOLS, 'tests', 'fixtures')
W = 'woow_document_management/'
CONFIG_FILES = ['tools/routes.lock.json', 'tools/sql_allowlist.yaml', 'tools/public_methods.txt',
                'tools/security_matrix.yaml', 'tools/cleanroom_forbidden.txt']
TOOL_NAMES = ['sudo', 'routes', 'forbidden', 'sql', 'sweep_config', 'security_matrix',
              'public_methods', 'cleanroom']


def _load(name):
    spec = importlib.util.spec_from_file_location('woow_check_%s' % name,
                                                  os.path.join(TOOLS, 'check_%s.py' % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOLS_MOD = {name: _load(name) for name in TOOL_NAMES}
sys.path.insert(0, TOOLS)
import _astlib  # noqa: E402


def _meta(text):
    """Split a fixture into (meta dict, body)."""
    if text.startswith('#! '):
        first, _, body = text.partition('\n')
        meta = dict(part.split('=', 1) for part in first[3:].split())
        return meta, body
    return {}, text


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _write(root, relpath, text):
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)


def snippets(case):
    """[(name, meta, body)] of a snippet fixture directory."""
    out = []
    base = os.path.join(FIXTURES, case)
    for name in sorted(os.listdir(base)):
        with open(os.path.join(base, name), encoding='utf-8') as fh:
            meta, body = _meta(fh.read())
        out.append((name[:-len('.fixture')], meta, body))
    return out


class ToolCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='woow-static-')
        self.root = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        for relpath in CONFIG_FILES:
            _write(self.root, relpath, _read(os.path.join(REPO, relpath)))

    def tree(self, case):
        base = os.path.join(FIXTURES, case)
        for dirpath, _dirs, names in os.walk(base):
            for name in names:
                if not name.endswith('.fixture'):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), base)[:-len('.fixture')]
                with open(os.path.join(dirpath, name), encoding='utf-8') as fh:
                    _write(self.root, rel, fh.read())

    def put(self, relpath, text):
        _write(self.root, relpath, text)

    def run_tool(self, name, g1=False, **extra):
        args = argparse.Namespace(g1=g1, root=self.root, emit_entry_points=False, from_lock=False,
                                  target=None)
        for key, value in extra.items():
            setattr(args, key, value)
        with contextlib.redirect_stdout(io.StringIO()):
            return TOOLS_MOD[name].check(self.root, args)

    def rules(self, name, **kw):
        return sorted({v.rule for v in self.run_tool(name, **kw)})

    def assertClean(self, name, **kw):
        violations = self.run_tool(name, **kw)
        self.assertEqual([str(v) for v in violations], [])

    def assertRule(self, name, rule, **kw):
        violations = self.run_tool(name, **kw)
        self.assertIn(rule, {v.rule for v in violations}, [str(v) for v in violations])
        return violations

    def snippet_cases(self, base_case, snippet_case, tool, **kw):
        for name, meta, body in snippets(snippet_case):
            with self.subTest(snippet=name):
                self.tearDown_tree()
                if base_case:
                    self.tree(base_case)
                self.put(meta['target'], body)
                for rule in meta['expect'].split(','):
                    self.assertRule(tool, rule, **kw)

    def tearDown_tree(self):
        for entry in os.listdir(self.root):
            if entry != 'tools':
                path = os.path.join(self.root, entry)
                shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)


# --------------------------------------------------------------------------- SUDO

class TestCheckSudo(ToolCase):

    def test_check_sudo_rejects_untagged(self):
        """T-STAT-01: an untagged .sudo( is rejected."""
        self.tree('sudo_good')
        self.assertClean('sudo', g1=True)
        name, meta, body = [s for s in snippets('sudo_snippets') if s[0] == 'untagged.py'][0]
        self.put(meta['target'], body)
        self.assertRule('sudo', 'SUDO-UNTAGGED')

    def test_check_sudo_counts_exact(self):
        """T-STAT-02: S1=1, S3=1 exactly with --g1; <= without; _inherit model (ER-44)."""
        self.tree('sudo_good')
        self.assertClean('sudo', g1=True)
        self.assertClean('sudo')
        # one missing: fine before G1, rejected with --g1
        os.remove(os.path.join(self.root, W + 'models/settings.py'))
        self.assertClean('sudo')
        self.assertRule('sudo', 'SUDO-COUNT', g1=True)
        # one too many: rejected in both modes
        self.tree('sudo_good')
        for name, meta, body in snippets('sudo_snippets'):
            if name == 'extra_s1.py':
                self.put(meta['target'], body)
        self.assertRule('sudo', 'SUDO-COUNT')
        self.assertRule('sudo', 'SUDO-COUNT', g1=True)

    def test_check_sudo_inherit_model_resolution(self):
        """T-STAT-02 (ER-44): the single string _inherit is the model; a list of two is not."""
        self.tree('sudo_good')
        self.assertClean('sudo', g1=True)   # the S1 fixture names its model by _inherit only
        for name, meta, body in snippets('sudo_snippets'):
            if name == 'inherit_not_single.py':
                self.put(meta['target'], body)
        self.assertRule('sudo', 'SUDO-NOT-ALLOWED')

    def test_with_user_is_never_allowed(self):
        """T-STAT-02: with_user( is rejected everywhere in non-test code."""
        self.snippet_cases('sudo_good', 'sudo_snippets', 'sudo')

    def test_aliases_detected(self):
        """T-STAT-03: sudo aliases, env(su=True), SUPERUSER_ID, api.Environment, getattr, ..."""
        self.tree('sudo_good')
        name, meta, body = [s for s in snippets('sudo_snippets') if s[0] == 'aliases.py'][0]
        self.put(meta['target'], body)
        texts = ' '.join(v.message for v in self.run_tool('sudo'))
        for alias in ('.sudo', 'env(su=/user=)', 'api.Environment(', 'SUPERUSER_ID',
                      "getattr(..., 'sudo')", 'with_env(', '_get_param(', 'update_env(user=)',
                      'compute_sudo=True', 'related_sudo=True', 'import SUPERUSER_ID'):
            self.assertIn(alias, texts)
        # downgrades are allowed anywhere
        self.assertNotIn('settings.py', ' '.join(str(v) for v in self.run_tool('sudo')
                                                 if 'sudo(False)' in v.message))

    def test_sudo_env_reuse_rejected(self):
        """T-STAT-19: <name>_sudo.env is rejected anywhere."""
        self.tree('sudo_good')
        name, meta, body = [s for s in snippets('sudo_snippets') if s[0] == 'env_reuse.py'][0]
        self.put(meta['target'], body)
        self.assertRule('sudo', 'SUDO-ENV-REUSE')

    def test_cron_must_check_su_first(self):
        """sec 6.2 point 8: _cron_* start with an env.su check."""
        self.tree('sudo_good')
        name, meta, body = [s for s in snippets('sudo_snippets') if s[0] == 'cron_su.py'][0]
        self.put(meta['target'], body)
        self.assertRule('sudo', 'SUDO-CRON-SU')


# --------------------------------------------------------------------------- ROUTE

class TestCheckRoutes(ToolCase):

    def mutate(self, relpath, old, new):
        path = os.path.join(self.root, relpath)
        text = _read(path)
        self.assertIn(old, text)
        _write(self.root, relpath, text.replace(old, new, 1))

    def test_check_routes_good_tree_and_g1(self):
        """T-STAT-04: the 9 locked routes pass, also with --g1."""
        self.tree('routes_good')
        self.assertClean('routes')
        self.assertClean('routes', g1=True)

    def test_check_routes_lock_subset_and_g1_equality(self):
        """T-STAT-04 R12: a missing route is fine before G1, not with --g1; extra routes fail."""
        self.tree('routes_good')
        self.mutate(W + 'controllers/main.py', "@http.route('/woow_document/zip'", "# @http.route('/woow_document/zip'")
        self.assertClean('routes')
        self.assertRule('routes', 'R12', g1=True)
        self.tree('routes_good')
        self.mutate(W + 'controllers/main.py', "'/woow_document/zip'", "'/woow_document/zip2'")
        self.assertRule('routes', 'R12')

    def test_check_routes_public_is_rejected(self):
        """T-STAT-04 R3: auth='public' is not allowed in any controller."""
        self.tree('routes_good')
        self.mutate(W + 'controllers/main.py', "'/woow_document/zip', type='http', auth='user'",
                    "'/woow_document/zip', type='http', auth='public'")
        self.assertRule('routes', 'R3')

    def test_check_routes_auth_none_csrf_json(self):
        """T-STAT-04 R3/R4/R5: auth='none', type='json', csrf=False are rejected."""
        for old, new, rule in (
                ("auth='user', methods=['GET'])\n    def woow_zip", "auth='none', methods=['GET'])\n    def woow_zip", 'R3'),
                ("'/woow_document/zip', type='http'", "'/woow_document/zip', type='json'", 'R4'),
                ("'/woow_document/zip', type='http'", "'/woow_document/zip', csrf=False, type='http'", 'R5')):
            with self.subTest(rule=rule):
                self.tree('routes_good')
                self.mutate(W + 'controllers/main.py', old, new)
                self.assertRule('routes', rule)

    def test_check_routes_methods_required_and_post(self):
        """T-STAT-04 R2: methods written out; upload routes are POST."""
        self.tree('routes_good')
        self.mutate(W + 'controllers/main.py', "auth='user', methods=['GET'])\n    def woow_zip",
                    "auth='user')\n    def woow_zip")
        self.assertRule('routes', 'R2')
        self.tree('routes_good')
        self.mutate(W + 'controllers/main.py', "'/woow_document/upload', type='http', auth='user', methods=['POST']",
                    "'/woow_document/upload', type='http', auth='user', methods=['GET', 'POST']")
        self.assertRule('routes', 'R2')

    def test_check_routes_path_converter(self):
        """T-STAT-04 R6: no <path:> or other converters."""
        self.tree('routes_good')
        self.mutate(W + 'controllers/main.py', "'/woow_document/doc/<int:document_id>/preview'",
                    "'/woow_document/doc/<path:rest>/preview'")
        self.assertRule('routes', 'R6')

    def test_check_routes_cors_and_prefix_and_pathless(self):
        """T-STAT-04 R7/R9/R10."""
        cases = (
            ("'/woow_document/zip', type='http'", "'/woow_document/zip', cors='*', type='http'", 'R7'),
            ("'/woow_document/zip', type='http'", "'/web/woow_zip', type='http'", 'R10'),
            ("@http.route('/woow_document/zip', type='http', auth='user', methods=['GET'])", '@http.route()', 'R9'),
        )
        for old, new, rule in cases:
            with self.subTest(rule=rule):
                self.tree('routes_good')
                self.mutate(W + 'controllers/main.py', old, new)
                self.assertRule('routes', rule)

    def test_check_routes_upload_body_limit(self):
        """T-STAT-04 R8: upload routes need max_content_length = 95 MiB."""
        self.tree('routes_good')
        self.mutate(W + 'controllers/main.py', 'max_content_length=95 * 1024 * 1024',
                    'max_content_length=100 * 1024 * 1024')
        self.assertRule('routes', 'R8')
        self.tree('routes_good')
        self.mutate(W + 'controllers/main.py', 'max_content_length=95 * 1024 * 1024',
                    'max_content_length=max_upload_body')
        self.assertClean('routes')

    def test_check_routes_literals_and_location(self):
        """T-STAT-04 R1/R13: literal arguments, routes only in controllers, website only in portal."""
        self.tree('routes_good')
        self.mutate(W + 'controllers/main.py', "@http.route('/woow_document/zip'", "@http.route(ZIP_ROUTE")
        self.assertRule('routes', 'R1')
        self.tree('routes_good')
        self.mutate(W + 'controllers/main.py', "'/woow_document/zip', type='http', auth='user'",
                    "'/woow_document/zip', type='http', auth='user', website=True")
        self.assertRule('routes', 'R13')
        self.tree('routes_good')
        os.makedirs(os.path.join(self.root, W + 'models'), exist_ok=True)
        shutil.copy(os.path.join(self.root, W + 'controllers/main.py'), os.path.join(self.root, W + 'models/main.py'))
        self.assertRule('routes', 'R1')

    def test_check_routes_alias_and_indirect_decorators(self):
        """T-STAT-04 R1/R3/R5: route under an import alias, a stored route object, other decorators."""
        cases = (
            ("from odoo.http import route as r\n\n\nclass Evil(http.Controller):\n"
             "    @r('/woow_document/evil2', auth='public', csrf=False, methods=['GET'])\n"
             "    def evil2(self, **kw):\n        return None\n", {'R3', 'R5', 'R12'}),
            ("import odoo.http as oh\n\n\nclass Evil(oh.Controller):\n"
             "    @oh.route('/woow_document/evil3', auth='public', methods=['GET'])\n"
             "    def evil3(self, **kw):\n        return None\n", {'R3', 'R12'}),
            ("dec = http.route('/woow_document/evil', auth='none', csrf=False, type='json')\n\n\n"
             "class Evil(http.Controller):\n    @dec\n    def evil(self, **kw):\n        return None\n",
             {'R1'}),
            ("import functools\nwrap = functools.partial(http.route, auth='none')\n", {'R1'}),
            ("class Evil(http.Controller):\n    @getattr(http, 'route')('/woow_document/e', auth='none')\n"
             "    def evil(self, **kw):\n        return None\n", {'R1'}),
        )
        for code, rules in cases:
            with self.subTest(code=code[:40]):
                self.tree('routes_good')
                path = os.path.join(self.root, W + 'controllers/main.py')
                _write(self.root, W + 'controllers/main.py', _read(path) + '\n\n' + code)
                found = set(self.rules('routes'))
                self.assertTrue(rules <= found, (rules, found))

    def test_check_routes_r9_core_controller_inheritance(self):
        """T-STAT-04 R9: no core controller subclass, no undecorated or core-route override."""
        cases = (
            (W + 'controllers/main.py',
             "from odoo.addons.web.controllers.binary import Binary\n\n\nclass EvilBin(Binary):\n"
             "    def content_common(self, **kw):\n        return super().content_common(**kw)\n"),
            (W + 'controllers/portal.py',
             "class Evil2(WoowDocumentPortal):\n    def home(self, **kw):\n        return super().home(**kw)\n"),
            (W + 'controllers/main.py',
             "class Evil3(WoowDocumentController):\n    def woow_zip(self, **kw):\n"
             "        return super().woow_zip(**kw)\n"),
        )
        for relpath, code in cases:
            with self.subTest(code=code[:40]):
                self.tree('routes_good')
                _write(self.root, relpath, _read(os.path.join(self.root, relpath)) + '\n\n' + code)
                self.assertRule('routes', 'R9')

    def test_check_routes_r9_undecorated_override(self):
        """T-STAT-04 R9: CustomerPortal.account / security overridden without a decorator."""
        for name in ('account', 'security', 'some_public_helper'):
            with self.subTest(name=name):
                self.tree('routes_good')
                self.mutate(W + 'controllers/portal.py', '    def _prepare_home_portal_values(self, counters):',
                            '    def %s(self, **post):\n        return super().%s(**post)\n\n'
                            '    def _prepare_home_portal_values(self, counters):' % (name, name))
                self.assertRule('routes', 'R9')
        self.tree('routes_good')
        self.mutate(W + 'controllers/portal.py', "    def portal_my_docs(self",
                    "    def account(self")
        self.assertRule('routes', 'R9')     # decorated, but replaces a core portal route

    def test_emit_entry_points_matches_routes(self):
        """T-STAT-17: 10 entry points from the code, equal to the ones built from the lock file."""
        self.tree('routes_good')
        mod = TOOLS_MOD['routes']
        from_code = mod.entry_points([info for info in (
            mod._info(r, f, d)[0] for r, _s, f, d in mod.extract(self.root))])
        from_lock = mod.entry_points(mod.load_lock(REPO))
        self.assertEqual(len(from_code), 10)
        self.assertEqual(len(from_lock), 10)
        key = lambda ep: (ep['route'], tuple(ep.get('methods') or ()), ep['auth'], ep['file'])  # noqa: E731
        self.assertEqual(sorted(map(key, from_code)), sorted(map(key, from_lock)))
        out = subprocess.run([sys.executable, '-I', os.path.join(TOOLS, 'check_routes.py'),
                              '--root', self.root, '--emit-entry-points'],
                             capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(json.loads(out.stdout)), 10)

    def test_routes_lock_has_final_9_rows(self):
        """interface_contracts 21 B5: the lock file holds the final 9 rows."""
        rows = TOOLS_MOD['routes'].load_lock(REPO)
        self.assertEqual(len(rows), 9)
        by_file = {}
        for row in rows:
            by_file.setdefault(row['file'].rsplit('/', 1)[1], []).append(row)
            self.assertEqual(set(row), {'file', 'func', 'routes', 'methods', 'auth', 'type', 'website'})
        self.assertEqual({k: len(v) for k, v in by_file.items()}, {'main.py': 4, 'portal.py': 5})
        self.assertTrue(all(r['auth'] == 'user' for r in rows))


# --------------------------------------------------------------------------- FORB

class TestCheckForbidden(ToolCase):

    def test_check_forbidden_rules(self):
        """T-STAT-05: every forbidden construct of arch 6.2 / sec 8 is rejected (parametrised)."""
        self.tree('forbidden_good')
        self.assertClean('forbidden', g1=True)
        for name, meta, body in snippets('forbidden_snippets'):
            with self.subTest(snippet=name):
                self.tearDown_tree()
                self.tree('forbidden_good')
                self.put(meta['target'], body)
                g1 = meta['expect'] == 'F-STUB'
                self.assertRule('forbidden', meta['expect'], g1=g1)

    def test_js_forbidden_sinks(self):
        """T-STAT-06: innerHTML, insertAdjacentHTML, markup(, window.open without noopener."""
        for name, meta, body in snippets('forbidden_snippets'):
            if name == 'sinks.js':
                self.put(meta['target'], body)
        messages = ' '.join(v.message for v in self.run_tool('forbidden'))
        for sink in ('innerHTML', 'insertAdjacentHTML(', 'markup(', 'window.open( without noopener'):
            self.assertIn(sink, messages)

    def test_eicar_literal_rejected(self):
        """T-STAT-05: the EICAR test string (built at run time, never stored in the repo)."""
        marker = 'EICAR-STANDARD-ANTIVIRUS-' + 'TEST-FILE'
        self.put(W + 'tests/test_x.py', 'DATA = "%s"\n' % marker)
        self.assertRule('forbidden', 'F-EICAR')

    def test_no_stub_markers_left(self):
        """T-STAT-16: WOOW-STUB markers are fine before G1 and rejected with --g1."""
        stub = 'WOOW-' + 'STUB'
        self.put(W + 'models/x.py', 'def f():\n    raise NotImplementedError  # %s\n' % stub)
        self.assertClean('forbidden')
        self.assertRule('forbidden', 'F-STUB', g1=True)
        self.put(W + 'tests/test_x.py', '# %s in a test is ignored\n' % stub)
        os.remove(os.path.join(self.root, W + 'models/x.py'))
        self.assertClean('forbidden', g1=True)

    def test_no_session_touch_or_inplace_mutation(self):
        """T-STAT-21: .touch( and in-place session mutation; copy + reassign is fine."""
        for name, meta, body in snippets('forbidden_snippets'):
            if name in ('touch.py', 'session_mut.py'):
                self.put(meta['target'].replace('x.py', name), body)
        rules = self.rules('forbidden')
        self.assertIn('F-TOUCH', rules)
        self.assertIn('F-SESSION-MUT', rules)
        self.tearDown_tree()
        self.tree('forbidden_good')   # contains request.session[KEY] = dict(...)
        self.assertClean('forbidden')
        # every in-place form on its own (each line must be caught separately)
        for line in ("request.session.get('woow_doc_state', {})[str(k)] = {}",
                     "request.session.get('woow_doc_state').pop(k)",
                     "request.session.setdefault('woow_doc_state', {})[k] = {}",
                     "http.request.session['woow_doc_state'][k] = {}",
                     "odoo.http.request.session['woow_doc_state'].update({})",
                     "request.session['woow_doc_state'][k] = {}",
                     "request.session.woow_seen = 1",
                     "request.session.update({'k': 1})",
                     "request.session.setdefault('k', 1)",
                     "request.session.clear()"):
            with self.subTest(line=line):
                self.put(W + 'controllers/y.py', 'import odoo\nfrom odoo import http\nfrom odoo.http import request\n\n\n'
                         'def f(k):\n    %s\n' % line)
                self.assertRule('forbidden', 'F-SESSION-MUT')
        self.put(W + 'controllers/y.py', 'from odoo.http import request\n\n\ndef f(k):\n'
                 '    g = dict(request.session.get(\'woow_doc_state\') or {})\n    g.pop(k, None)\n'
                 '    request.session[\'woow_doc_state\'] = g\n    request.session.should_rotate = True\n')
        self.assertClean('forbidden')

    def test_session_uid_keys_rejected(self):
        """T-STAT-21 / F-SESSION: uid / pre_uid / login keys as item, attribute or .get key."""
        for line in ("request.session['uid'] = 2", "request.session['pre_uid'] = 2",
                     "x = request.session['uid']", "x = request.session.get('uid')",
                     "request.session.pop('pre_login', None)", "request.session['session_token'] = 'x'",
                     "x = request.session.login", "request.session.uid = 2"):
            with self.subTest(line=line):
                self.put(W + 'controllers/y.py', 'from odoo.http import request\n\n\ndef f():\n    %s\n' % line)
                self.assertRule('forbidden', 'F-SESSION')

    def test_redirect_variants_rejected(self):
        """T-STAT-05 / F-REDIRECT: redirect_query, werkzeug redirect, aliases, Location headers."""
        for line in ("return request.redirect_query(post.get('next'))",
                     "return request.redirect(post.get('next'))",
                     "return werkzeug.utils.redirect(post.get('next'))",
                     "return go(post.get('next'))",
                     "return Response('', status=302, headers={'Location': post.get('next')})",
                     "return Response('', status=302, headers=[('Location', post.get('next'))])",
                     "response.headers['Location'] = post.get('next')"):
            with self.subTest(line=line):
                self.put(W + 'controllers/y.py', 'import werkzeug\nfrom werkzeug.utils import redirect as go\n'
                         'from odoo.http import Response, request\n\n\ndef f(post, response):\n    %s\n' % line)
                self.assertRule('forbidden', 'F-REDIRECT')
        self.put(W + 'controllers/y.py', 'from odoo.http import request\n\n\ndef f(folder_id):\n'
                 "    request.redirect('/my/docs')\n    return request.redirect('/my/docs/folder/%d' % folder_id)\n")
        self.assertClean('forbidden')

    def test_route_alias_flags_rejected(self):
        """T-STAT-05 / F-ROUTE: auth='none', csrf=False, type='json' under an alias of route."""
        for deco in ("@r('/x', csrf=False)", "@oh.route('/x', auth='none')", "@r('/x', type='json')"):
            with self.subTest(deco=deco):
                self.put(W + 'controllers/y.py', 'import odoo.http as oh\nfrom odoo.http import route as r\n\n\n'
                         'class C(oh.Controller):\n    %s\n    def x(self):\n        return 1\n' % deco)
                self.assertRule('forbidden', 'F-ROUTE')

    def test_no_raised_http_exceptions_in_controllers(self):
        """T-STAT-23 / T-SEC-81: raise/abort/return not_found in controllers (ER-29)."""
        for name, meta, body in snippets('forbidden_snippets'):
            if name == 'httpexc.py':
                self.put(meta['target'], body)
                self.put(W + 'models/y.py', body)   # models may raise
        violations = [v for v in self.run_tool('forbidden') if v.rule == 'F-HTTPEXC']
        self.assertEqual(len(violations), 5, [str(v) for v in violations])
        self.assertTrue(all('/controllers/' in v.path for v in violations))

    def test_no_commercial_partner_in_rules_or_controllers(self):
        """T-STAT-24: commercial_partner_id in security XML, controllers and models (ER-04)."""
        for name, meta, body in snippets('forbidden_snippets'):
            if name.startswith('commercial'):
                self.put(meta['target'], body)
        paths = {v.path for v in self.run_tool('forbidden') if v.rule == 'F-COMMERCIAL'}
        self.assertEqual(paths, {W + 'security/rules.xml', W + 'controllers/x.py', W + 'models/x.py'})

    def test_no_force_send_true(self):
        """T-STAT-25: force_send=True (ER-35)."""
        for name, meta, body in snippets('forbidden_snippets'):
            if name == 'force_send.py':
                self.put(meta['target'], body)
        self.assertRule('forbidden', 'F-FORCE-SEND')


# --------------------------------------------------------------------------- SQL

class TestCheckSql(ToolCase):

    def test_check_sql_allowlisted_q4_passes(self):
        """T-STAT-07: Q4 in services/locks.py with tags and literal queries passes."""
        self.tree('sql_good')
        self.assertClean('sql')
        self.assertRule('sql', 'SQL-UNUSED', g1=True)

    def test_check_sql_rejections(self):
        """T-STAT-07: outside the allowlist, untagged, f-string / % / format / + are rejected."""
        self.snippet_cases('sql_good', 'sql_snippets', 'sql')

    def test_check_sql_format_variants(self):
        """T-STAT-07: each of f-string, %, .format and + is reported."""
        self.tree('sql_good')
        for name, meta, body in snippets('sql_snippets'):
            if name == 'fstring.py':
                self.put(meta['target'], body)
        found = [v for v in self.run_tool('sql') if v.rule == 'SQL-FORMAT']
        self.assertEqual(len(found), 4, [str(v) for v in found])

    def test_check_sql_rebound_query_is_rejected(self):
        """A literal query stops being safe after any later rebinding or augmentation."""
        self.tree('sql_good')
        path = W + 'services/locks.py'
        original = _read(os.path.join(self.root, path))
        for assignment in ('q += suffix', 'q := suffix', 'for q in values: pass'):
            with self.subTest(assignment=assignment):
                if assignment.startswith('q :='):
                    mutation = 'if (q := suffix): pass'
                else:
                    mutation = assignment
                body = ('def try_lock(self, suffix, values):\n'
                        '    q = "SELECT 1"\n'
                        '    %s\n' % mutation +
                        '    self.env.cr.execute(q)  # sql-ok: Q4\n')
                self.put(path, original + '\n' + body)
                self.assertRule('sql', 'SQL-FORMAT')

    def test_advisory_locks_only_in_locks_py(self):
        """T-STAT-22 / T-SEC-86: pg_*advisory* outside locks.py; builtin hash( in locks.py."""
        self.tree('sql_good')
        self.assertClean('sql')
        for name, meta, body in snippets('sql_snippets'):
            if name in ('advisory.py', 'builtin_hash.py'):
                self.put(meta['target'], body)
        rules = self.rules('sql')
        self.assertIn('SQL-ADVISORY', rules)
        self.assertIn('SQL-LOCK-HASH', rules)


# --------------------------------------------------------------------------- PUBM

class TestCheckPublicMethods(ToolCase):

    def test_check_public_methods_good(self):
        """T-STAT-08: listed methods with a first-statement check, overrides, a stub."""
        self.tree('pubm_good')
        self.assertClean('public_methods')
        violations = self.run_tool('public_methods', g1=True)
        self.assertIn('PUBM-FIRST', {v.rule for v in violations})      # the stub is not OK in G1
        self.assertIn('PUBM-MISSING', {v.rule for v in violations})

    def test_check_public_methods_rejections(self):
        """T-STAT-08: unlisted, wrong first statement, AbstractModel, @api.model, override."""
        self.snippet_cases('pubm_good', 'pubm_snippets', 'public_methods')


# --------------------------------------------------------------------------- MTX

def _matrix_files():
    """CSV + rules.xml generated from the matrix (what core-models-acl must ship)."""
    mx = _astlib.load_yaml_file(os.path.join(REPO, 'tools/security_matrix.yaml'))
    alias = mx['aliases']
    lines = ['id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink']
    for model, per_group in mx['acl'].items():
        for g, perms in per_group.items():
            ident = 'access_%s_%s' % (model.replace('.', '_'), g)
            name = ident + (' HD-07 probe only' if (model, g) == tuple(mx['hd07']) else '')
            lines.append('%s,%s,model_%s,%s,%s' % (ident, name, model.replace('.', '_'), alias[g],
                                                  ','.join(perms)))
    recs = []
    for r in mx['rules']:
        groups = ', '.join("(4, ref('%s'))" % alias[g] for g in r['groups'])
        fields = ''.join('<field name="%s" eval="%s"/>' % (k, 'True' if p == '1' else 'False')
                         for k, p in zip(('perm_read', 'perm_write', 'perm_create', 'perm_unlink'), r['perms']))
        domain = r['domain'].replace('&', '&amp;').replace('<', '&lt;')
        recs.append('<record id="%s" model="ir.rule"><field name="name">%s</field>'
                    '<field name="model_id" ref="model_%s"/>%s<field name="domain_force">%s</field>%s</record>' % (
                        r['id'], r['ref'], r['model'].replace('.', '_'),
                        ('<field name="groups" eval="[%s]"/>' % groups) if groups else '', domain, fields))
    return '\n'.join(lines) + '\n', '<odoo>\n%s\n</odoo>\n' % '\n'.join(recs)


class TestCheckSecurityMatrix(ToolCase):

    def setUp(self):
        super().setUp()
        shutil.copy(os.path.join(REPO, W + 'security/groups.xml'), self._p('groups.xml'))

    def _p(self, name):
        path = os.path.join(self.root, W + 'security', name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def full(self):
        csv_text, rules_xml = _matrix_files()
        _write(self.root, W + 'security/ir.model.access.csv', csv_text)
        _write(self.root, W + 'security/rules.xml', rules_xml)
        return csv_text, rules_xml

    def test_check_security_matrix_day0_and_full(self):
        """T-STAT-09: header-only CSV passes before G1; the full matrix passes with --g1."""
        _write(self.root, W + 'security/ir.model.access.csv',
               'id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n')
        self.assertClean('security_matrix')
        self.assertRule('security_matrix', 'MTX-ACL', g1=True)
        self.full()
        self.assertClean('security_matrix', g1=True)

    def test_check_security_matrix_acl_rejections(self):
        """T-STAT-09: public row, group-less row, log W/D, wrong perms, missing HD-07 note."""
        csv_text, _ = self.full()
        cases = (
            (csv_text + 'x1,x1,model_woow_document,base.group_public,1,0,0,0\n', 'MTX-ACL'),
            (csv_text + 'x2,x2,model_woow_document_tag,,1,0,0,0\n', 'MTX-ACL'),
            (csv_text.replace('model_woow_document_log,woow_document_management.group_woow_document_user,1,0,0,0',
                              'model_woow_document_log,woow_document_management.group_woow_document_user,1,1,0,1')
             .replace('model_woow_document_log,group_woow_document_user,1,0,0,0',
                      'model_woow_document_log,group_woow_document_user,1,1,0,1'), 'MTX-ACL'),
            (csv_text.replace(' HD-07 probe only', ''), 'MTX-HD07'),
        )
        for text, rule in cases:
            with self.subTest(rule=rule, n=cases.index((text, rule))):
                _write(self.root, W + 'security/ir.model.access.csv', text)
                self.assertRule('security_matrix', rule)

    def test_check_security_matrix_rule_rejections(self):
        """T-STAT-09: rule domain, perms, unknown rule, commercial_partner_id, missing in G1."""
        _, rules_xml = self.full()
        cases = (
            (rules_xml.replace("('eff_read_partner_ids', 'in', [user.partner_id.id])]</field>",
                               "('eff_read_partner_ids', 'in', [user.commercial_partner_id.id])]</field>", 1), False),
            (rules_xml.replace('<field name="perm_unlink" eval="False"/></record>',
                               '<field name="perm_unlink" eval="True"/></record>', 1), False),
            (rules_xml.replace('</odoo>', '<record id="rule_extra" model="ir.rule"><field name="model_id" '
                               'ref="model_woow_document"/><field name="domain_force">[]</field></record></odoo>'), False),
            (rules_xml.replace('id="rule_woow_document_log_manager"', 'id="rule_renamed"'), True),
        )
        for text, g1 in cases:
            with self.subTest(n=cases.index((text, g1))):
                _write(self.root, W + 'security/rules.xml', text)
                self.assertRule('security_matrix', 'MTX-RULE', g1=g1)

    def test_check_security_matrix_company_rules(self):
        """T-STAT-09: with --g1 every model with company_id needs its global company rule."""
        _, rules_xml = self.full()
        start = rules_xml.index('<record id="rule_woow_document_tag_company"')
        end = rules_xml.index('</record>', start) + len('</record>')
        _write(self.root, W + 'security/rules.xml', rules_xml[:start] + rules_xml[end:])
        self.assertClean('security_matrix')
        self.assertRule('security_matrix', 'MTX-COMPANY', g1=True)

    def test_check_security_matrix_groups(self):
        """T-STAT-09 / DEC-10 / ER-38: users field and base.group_user implying the user group."""
        self.full()
        path = self._p('groups.xml')
        text = _read(path)
        _write(self.root, W + 'security/groups.xml', text.replace(
            '<field name="comment">Works', '<field name="users" eval="[(4, ref(\'base.user_admin\'))]"/>\n'
            '        <field name="comment">Works', 1))
        self.assertRule('security_matrix', 'MTX-GROUP')
        _write(self.root, W + 'security/groups.xml', text)
        _write(self.root, W + 'data/x.xml', '<odoo><record id="base.group_user" model="res.groups">'
               '<field name="implied_ids" eval="[(4, ref(\'group_woow_document_user\'))]"/></record></odoo>')
        self.assertRule('security_matrix', 'MTX-GROUP')

    def test_group_ids_follow_the_matrix(self):
        """The admin-assignment allowlist of group ids equals the matrix groups."""
        mx = _astlib.load_yaml_file(os.path.join(REPO, 'tools/security_matrix.yaml'))
        self.assertEqual(set(TOOLS_MOD['security_matrix'].GROUP_IDS), set(mx['groups']))

    def test_check_security_matrix_whole_addon(self):
        """T-STAT-09 / T-SEC-79: ACL, rules and group writes outside security/ (DEC-10, ER-38)."""
        ref_user = "ref('woow_document_management.group_woow_document_user')"
        cases = (
            ('data/ir_cron.xml', '<record id="rule_all" model="ir.rule"><field name="model_id" ref="model_woow_document"/>'
             '<field name="groups" eval="[(4, ref(\'base.group_user\'))]"/>'
             '<field name="domain_force">[(1, \'=\', 1)]</field></record>', {'MTX-LOCATION', 'MTX-RULE'}),
            ('data/ir_cron.xml', '<record id="acl_pub" model="ir.model.access"><field name="name">x</field>'
             '<field name="model_id" ref="model_woow_document"/><field name="group_id" ref="base.group_public"/>'
             '<field name="perm_read" eval="1"/></record>', {'MTX-LOCATION', 'MTX-ACL'}),
            ('data/ir_cron.xml', '<record id="base.default_user" model="res.users">'
             '<field name="groups_id" eval="[(4, %s)]"/></record>' % ref_user, {'MTX-GROUP'}),
            ('views/x.xml', '<record id="base.group_portal" model="res.groups"><field name="implied_ids" '
             'eval="[(4, ref(\'woow_document_management.group_woow_document_manager\'))]"/></record>',
             {'MTX-LOCATION', 'MTX-GROUP'}),
            ('data/ir_cron.xml', '<function model="res.groups" name="write" eval="[[ref(\'base.group_user\')], '
             '{\'implied_ids\': [(4, %s)]}]"/>' % ref_user, {'MTX-FUNCTION'}),
            ('data/ir_cron.xml', '<function model="ir.config_parameter" name="set_param" '
             'eval="[\'x\', \'groups_id\']"/>', {'MTX-FUNCTION'}),
            ('data/ir_cron.xml', '<delete model="ir.rule" id="rule_woow_document_company"/>', {'MTX-DELETE'}),
            ('data/ir_cron.xml', '<record id="cat2" model="ir.module.category"><field name="name">x</field></record>',
             {'MTX-LOCATION'}),
            ('security/extra.xml', '<record id="group_x" model="res.groups"><field name="name">x</field></record>',
             {'MTX-LOCATION', 'MTX-GROUP'}),
            ('data/admin_assignment.xml', '<record id="group_woow_document_manager" model="res.groups">'
             '<field name="implied_ids" eval="[(4, ref(\'base.group_portal\'))]"/></record>', {'MTX-LOCATION'}),
            ('data/admin_assignment.xml', '<function model="res.groups" name="write" eval="[[ref('
             '\'woow_document_management.group_woow_document_manager\')], {\'users\': [(4, ref(\'base.user_admin\'))],'
             ' \'implied_ids\': [(4, ref(\'base.group_portal\'))]}]"/>', {'MTX-FUNCTION'}),
            ('data/admin_assignment.xml', '<function model="res.groups" name="write" eval="[[ref('
             '\'base.group_user\')], {\'users\': [(4, ref(\'base.user_admin\'))]}]"/>', {'MTX-FUNCTION'}),
        )
        for relpath, body, rules in cases:
            with self.subTest(body=body[:50]):
                self.full()
                for stale in ('data', 'views', 'security/extra.xml'):
                    target = os.path.join(self.root, W + stale)
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                    elif os.path.exists(target):
                        os.remove(target)
                _write(self.root, W + relpath, '<odoo>%s</odoo>\n' % body)
                found = set(self.rules('security_matrix'))
                self.assertTrue(rules <= found, (rules, found))
        # CSV outside security/
        self.full()
        shutil.rmtree(os.path.join(self.root, W + 'data'), ignore_errors=True)
        _write(self.root, W + 'data/ir.model.access.csv',
               'id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n'
               'x,x,model_woow_document,base.group_portal,1,1,1,1\n')
        self.assertRule('security_matrix', 'MTX-LOCATION')

    def test_check_security_matrix_rule_active_and_global(self):
        """T-STAT-09: a disabled rule (active=False) or a global field is rejected."""
        _, rules_xml = self.full()
        for field in ('<field name="active" eval="False"/>', '<field name="global" eval="True"/>'):
            with self.subTest(field=field):
                _write(self.root, W + 'security/rules.xml',
                       rules_xml.replace('<field name="domain_force">', field + '<field name="domain_force">', 1))
                self.assertRule('security_matrix', 'MTX-RULE')

    def test_xml_acl_omitted_permission_is_false(self):
        """Odoo ACL defaults to false when a permission field is absent."""
        self.full()
        _write(self.root, W + 'security/extra.xml',
               '<odoo><record id="acl_xml" model="ir.model.access">'
               '<field name="model_id" ref="model_woow_document"/>'
               '<field name="group_id" ref="base.group_public"/>'
               '</record></odoo>')
        rows, _, _, _ = TOOLS_MOD['security_matrix'].load_actual(self.root)
        row = next(x for x in rows if x['id'] == 'acl_xml')
        self.assertEqual(row['perms'], '0000')

    def test_admin_assignment_pr14_allowed(self):
        """PR-14: the noupdate admin assignment (function or users-only record) passes."""
        self.full()
        for body in ('<function model="res.groups" name="write" eval="[[ref(\'woow_document_management.'
                     'group_woow_document_manager\')], {\'users\': [(4, ref(\'base.user_admin\'))]}]"/>',
                     '<record id="group_woow_document_manager" model="res.groups">'
                     '<field name="users" eval="[(4, ref(\'base.user_admin\'))]"/></record>'):
            with self.subTest(body=body[:40]):
                _write(self.root, W + 'data/admin_assignment.xml', '<odoo noupdate="1">%s</odoo>\n' % body)
                self.assertClean('security_matrix', g1=True)


# --------------------------------------------------------------------------- CFG

class TestCheckSweepConfig(ToolCase):

    def setUp(self):
        super().setUp()
        for relpath in ['.security/sweep.yaml', '.github/workflows/security.yml', 'LICENSE', 'COPYING',
                        'README.md', 'CLEANROOM.md', 'SECURITY.md', '.gitignore',
                        '.semgrepignore', '.gitattributes']:
            shutil.copy(os.path.join(REPO, relpath), self._dst(relpath))
        cfg = _astlib.load_yaml_file(os.path.join(REPO, '.security/sweep.yaml'))
        for test in cfg['verify']['regression_tests']:
            _write(self.root, test, '')

    def _dst(self, relpath):
        path = os.path.join(self.root, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def edit(self, relpath, old, new):
        text = _read(self._dst(relpath))
        self.assertIn(old, text)
        _write(self.root, relpath, text.replace(old, new, 1))

    def test_check_sweep_config_good(self):
        """T-STAT-10 / T-SEC-P5 (files exist): the repo config passes."""
        self.assertClean('sweep_config')

    def test_semgrepignore_cannot_hide_tests(self):
        self.put('.semgrepignore', 'tests/\n')
        self.assertRule('sweep_config', 'CFG-SCAN')

    def test_check_sweep_config_rejections(self):
        """T-STAT-10: private keys, protected excludes, missing tools, exclude_rules, autofix, ..."""
        cases = (
            ('.security/sweep.yaml', 'version: 1\n', 'version: 1\nreview_units: {per_run: 1}\n', 'CFG-KEYS'),
            ('.security/sweep.yaml', '  - i18n/\n', '  - i18n/\n  - tests/\n', 'CFG-SCAN'),
            ('.security/sweep.yaml', '      rulesets: [p/python, p/secrets]', '      rulesets: [p/python]', 'CFG-SCAN'),
            ('.security/sweep.yaml', '    gitleaks: {}', '    gitleaks: {}\n    bandit: {exclude_rules: [x]}', 'CFG-SCAN'),
            ('.security/sweep.yaml', '  - tools/\n', '', 'CFG-HUMAN'),
            ('.security/sweep.yaml', '  - .security/\n', '', 'CFG-HUMAN'),
            ('.security/sweep.yaml', '  enabled: false', '  enabled: true', 'CFG-AUTOFIX'),
            ('.security/sweep.yaml', '  - tools/tests/test_static_checks.py\n', '', 'CFG-VERIFY'),
            ('.security/sweep.yaml', '  - tools/tests/test_static_checks.py\n',
             '  - tools/tests/test_static_checks.py\n  - woow_document_management_legacy_urls/tests/test_x.py\n', 'CFG-LEGACY'),
        )
        for relpath, old, new, rule in cases:
            with self.subTest(rule=rule, new=new[:40]):
                original = _read(self._dst(relpath))
                self.edit(relpath, old, new)
                self.assertRule('sweep_config', rule)
                _write(self.root, relpath, original)

    def test_security_dir_is_always_human(self):
        """T-SEC-P3 (repo side): .security/ stays in always_human, so any change goes to a human."""
        cfg = _astlib.load_yaml_file(os.path.join(REPO, '.security/sweep.yaml'))
        self.assertIn('.security/', cfg['always_human']['paths'])
        # The 33 required files and the tests/test_reg_<slug>.py files that fixes added.
        self.assertEqual(len(cfg['verify']['regression_tests']), 33 + 17)

    def test_check_workflow_rules(self):
        """T-STAT-10 / sec 8: no pull_request_target, read-only permissions, SHA-pinned actions."""
        wf = '.github/workflows/security.yml'
        text = _read(self._dst(wf))
        for old, new in (('  pull_request:\n', '  pull_request_target:\n'),
                         ('  contents: read', '  contents: write'),
                         ('actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683', 'actions/checkout@v4')):
            with self.subTest(new=new):
                self.assertIn(old, text)
                _write(self.root, wf, text.replace(old, new, 1))
                self.assertRule('sweep_config', 'CFG-WORKFLOW')
        _write(self.root, wf, text)

    def test_repo_hygiene_export_ignore_blocked(self):
        """T-STAT-12 / T-SEC-P4: export-ignore anywhere is rejected (CFG and CLEAN)."""
        self.assertClean('sweep_config')
        _write(self.root, W + '.gitattributes', 'controllers/main.py export-ignore\n')
        self.assertRule('sweep_config', 'CFG-ATTR')
        self.assertRule('cleanroom', 'CLEAN-TREE')

    def test_repo_hygiene_blocked_artefacts(self):
        """T-STAT-12: logs, dumps, browser output and stray screenshots are rejected."""
        for relpath in ('odoo.log', '.playwright-cli/x.json', 'db.dump', 'shot.png', '.env.local'):
            with self.subTest(path=relpath):
                _write(self.root, relpath, 'x')
                self.assertRule('sweep_config', 'CFG-FILES')
                os.remove(os.path.join(self.root, relpath))
        _write(self.root, W + 'static/description/icon.png', 'x')
        self.assertClean('sweep_config')

    def test_private_target_content_rejected(self):
        """sec 9: private target content must not land in the repo."""
        _write(self.root, 'docs/x.yaml', 'context:\n  ' + 'trust_' + 'boundaries:\n  - x\n')
        self.assertRule('sweep_config', 'CFG-TARGET')

    def test_no_gitmodules_or_gitlink(self):
        """T-STAT-20 (R2b-10): .gitmodules, gitlink and symlink entries are rejected."""
        _write(self.root, '.gitmodules', '[submodule "x"]\n')
        self.assertRule('sweep_config', 'CFG-GITLINK')
        os.remove(os.path.join(self.root, '.gitmodules'))
        os.symlink('README.md', os.path.join(self.root, 'link.md'))
        self.assertRule('sweep_config', 'CFG-GITLINK')
        self.assertRule('cleanroom', 'CLEAN-TREE')
        os.remove(os.path.join(self.root, 'link.md'))
        git = shutil.which('git')
        if git is None:
            self.skipTest('git is not installed')
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', HOME=self.root)
        run = lambda *a: subprocess.run([git, '-C', self.root] + list(a), check=True,  # noqa: E731
                                        capture_output=True, env=env)
        run('init', '-q')
        self.assertClean('sweep_config')
        run('update-index', '--add', '--cacheinfo', '160000,%s,vendor/sub' % ('1' * 40))
        self.assertRule('sweep_config', 'CFG-GITLINK')


# --------------------------------------------------------------------------- CLEAN

class TestCheckCleanroom(ToolCase):

    def test_check_cleanroom_good(self):
        """T-STAT-11: headers present, '文件管理' allowed."""
        self.tree('clean_good')
        self.assertClean('cleanroom')

    def test_check_cleanroom_headers(self):
        """T-STAT-11 (ER-33 point 1): .py/.xml/.js/.scss without the WoowTech LGPL-3 header."""
        self.snippet_cases('clean_good', 'clean_snippets', 'cleanroom')

    def test_check_cleanroom_legacy_names(self):
        """T-STAT-11 (ER-33 point 2): legacy technical names, except in CLEANROOM.md and fixtures."""
        self.tree('clean_good')
        legacy = 'sh' + '_document_' + 'management'
        _write(self.root, 'CLEANROOM.md', 'Replaces %s.\n' % legacy)
        _write(self.root, 'tools/tests/fixtures/x.txt', legacy)
        self.assertClean('cleanroom')
        _write(self.root, 'docs/notes.md', 'mentions %s\n' % legacy)
        self.assertRule('cleanroom', 'CLEAN-LEGACY')

    def test_check_cleanroom_phrases(self):
        """T-STAT-11 (ER-33 point 3): listed phrases in .po/.xml/.js."""
        self.tree('clean_good')
        phrases = TOOLS_MOD['cleanroom'].load_phrases(REPO)
        self.assertEqual(len(phrases), 2)
        _write(self.root, W + 'i18n/zh_TW.po', 'msgid "x"\nmsgstr "%s"\n' % phrases[0])
        self.assertRule('cleanroom', 'CLEAN-PHRASE')

    def test_check_cleanroom_static_lib(self):
        """T-STAT-11 (ER-33 point 4): no static/lib."""
        self.tree('clean_good')
        _write(self.root, W + 'static/lib/x.txt', 'x')
        self.assertRule('cleanroom', 'CLEAN-TREE')


# --------------------------------------------------------------------------- manifest and repo

class TestRepo(unittest.TestCase):

    def test_manifest_literals(self):
        """T-STAT-13: one pure-literal manifest; name, licence, depends, data order (ER-11, ER-38)."""
        manifests = [r for r, _p in _astlib.iter_files(REPO) if r.endswith('__manifest__.py')]
        self.assertEqual(manifests, [W + '__manifest__.py'])
        with open(os.path.join(REPO, manifests[0]), encoding='utf-8') as fh:
            manifest = ast.literal_eval(fh.read())
        self.assertEqual(manifest['name'], 'Woow Document Management')
        self.assertEqual(manifest['license'], 'LGPL-3')
        self.assertEqual(manifest['version'], '18.0.1.0.0')
        self.assertEqual(manifest['depends'], ['mail', 'portal'])
        for key in ('external_dependencies', 'post_init_hook', 'pre_init_hook'):
            self.assertNotIn(key, manifest)
        self.assertEqual(manifest['uninstall_hook'], 'uninstall_hook')
        self.assertTrue(manifest['application'])
        data = manifest['data']
        self.assertEqual(data[:2], ['security/groups.xml', 'data/admin_assignment.xml'])
        self.assertEqual(data[-1], 'views/menus.xml')
        self.assertEqual(len(data), len(set(data)))
        for path in data:
            self.assertTrue(os.path.isfile(os.path.join(REPO, W, path)), path)
        self.assertEqual(set(manifest['assets']), {'web.assets_backend', 'web.assets_frontend',
                                                   'web.assets_tests'})

    def test_tests_init_day0(self):
        """IF-SKEL 2.3: tests/__init__.py imports the regression test files of sweep.yaml and the others."""
        cfg = _astlib.load_yaml_file(os.path.join(REPO, '.security/sweep.yaml'))
        expected = sorted(os.path.basename(p)[:-3] for p in cfg['verify']['regression_tests']
                          if p.startswith(W)) + ['test_perf', 'test_core_assumptions_http',
                                                  'test_core_assumptions_access']
        tree = ast.parse(_read(os.path.join(REPO, W + 'tests/__init__.py')))
        imported = sorted(alias.name for node in tree.body if isinstance(node, ast.ImportFrom)
                          for alias in node.names)
        self.assertEqual(imported, sorted(expected))
        self.assertEqual(len(imported), 35 + 17)   # the seventeen tests/test_reg_<slug>.py files

    def test_repo_passes_every_tool(self):
        """T-SEC-75: the repository itself passes the 8 tools (exit 0) without --g1."""
        for name in TOOL_NAMES:
            with self.subTest(tool=name):
                out = subprocess.run([sys.executable, '-I', os.path.join(TOOLS, 'check_%s.py' % name),
                                      '--root', REPO], capture_output=True, text=True, check=False)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_cli_exit_codes(self):
        """IF-STATIC 4.1: 2 when the tool cannot run, 1 on violations."""
        out = subprocess.run([sys.executable, '-I', os.path.join(TOOLS, 'check_sudo.py'),
                              '--root', os.path.join(REPO, 'does-not-exist')],
                             capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 2)
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, W + 'models/x.py', 'def f(rec):\n    return rec.sudo()\n')
            out = subprocess.run([sys.executable, '-I', os.path.join(TOOLS, 'check_sudo.py'), '--root', tmp],
                                 capture_output=True, text=True, check=False)
            self.assertEqual(out.returncode, 1)
            self.assertRegex(out.stdout, r'^woow_document_management/models/x.py:2 SUDO-NOT-ALLOWED ')


class TestServerFieldsDeclared(unittest.TestCase):
    """Every model of the module takes the own default of the fields that only the server sets.

    Create completes the keys it was not given from the defaults of the caller (``ir.default``, which a
    call made with ``sudo()`` keeps) and from the context. A model must therefore declare the fields
    that only the server sets, or that create checks only when they are passed
    (``_woow_own_default_fields``), and inherit the mixin that applies the declaration, directly or
    through the guard mixin. A new model, or a wizard that gets such a field, fails here until it does.
    """

    MIXIN = 'woow.document.server.fields.mixin'
    GUARD = 'woow.document.guard.mixin'
    # Wizards whose fields are all filled in by the user in the form.
    FORM_ONLY = frozenset({
        'woow.document.move.wizard', 'woow.document.purge.wizard',
        'woow.document.restore.wizard', 'woow.document.add.attachment.wizard',
        'woow.document.notify.wizard'})

    @staticmethod
    def _literal(node):
        """The strings of a ``_name`` or ``_inherit`` value (a string or a list of them)."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, (ast.List, ast.Tuple)):
            return [item.value for item in node.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)]
        return []

    def _classes(self, relpath):
        """{class name: (bases, {attribute: value node})} of the module-level classes of a file."""
        tree = ast.parse(_read(os.path.join(REPO, relpath)))
        found = {}
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            attributes = {}
            for statement in node.body:
                if isinstance(statement, ast.Assign) and len(statement.targets) == 1 \
                        and isinstance(statement.targets[0], ast.Name):
                    attributes[statement.targets[0].id] = statement.value
            bases = [ast.unparse(base) for base in node.bases]
            found[node.name] = (bases, attributes)
        return found

    def _models(self):
        """{model name: (relpath, inherited names, declares)} of the own models in models/ and wizards/."""
        found = {}
        for folder in ('models', 'wizards'):
            directory = os.path.join(REPO, W + folder)
            for name in sorted(os.listdir(directory)):
                if not name.endswith('.py') or name == '__init__.py':
                    continue
                relpath = W + folder + '/' + name
                for bases, attributes in self._classes(relpath).values():
                    if not {'models.Model', 'models.TransientModel'} & set(bases) \
                            or '_name' not in attributes:
                        continue
                    model = self._literal(attributes['_name'])[0]
                    found[model] = (relpath, set(self._literal(attributes.get('_inherit', ast.Pass()))),
                                    '_woow_own_default_fields' in attributes)
        return found

    def test_every_model_declares_the_fields_that_only_the_server_sets(self):
        models = self._models()
        self.assertGreaterEqual(len(models), 10)
        for model, (relpath, inherited, declares) in sorted(models.items()):
            with self.subTest(model=model):
                if model in self.FORM_ONLY:
                    self.assertFalse(
                        inherited & {self.MIXIN, self.GUARD} or declares,
                        '%s declares server fields: remove it from FORM_ONLY' % model)
                    continue
                self.assertTrue(
                    inherited & {self.MIXIN, self.GUARD},
                    '%s (%s) must inherit %s' % (model, relpath, self.MIXIN))
                self.assertTrue(
                    declares,
                    '%s (%s) must declare _woow_own_default_fields (frozenset() when every field '
                    'is filled in by its users)' % (model, relpath))
        self.assertEqual(self.FORM_ONLY - set(models), set())

    def test_the_mixins_apply_the_declaration(self):
        """The mixin calls ``own_default_values``; the guard mixin and the attachments use it too."""
        def calls_helper(function):
            return any(isinstance(node, ast.Call) and getattr(node.func, 'id', '') ==
                       'own_default_values' for node in ast.walk(function))

        for relpath, class_name in ((W + 'models/guard_mixin.py', 'WoowServerFieldsMixin'),
                                    (W + 'models/ir_attachment.py', 'IrAttachment')):
            tree = ast.parse(_read(os.path.join(REPO, relpath)))
            cls = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == class_name)
            functions = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                         and node.name == '_add_missing_default_values']
            with self.subTest(file=relpath):
                self.assertEqual(len(functions), 1)
                self.assertTrue(calls_helper(functions[0]))
        guard = self._classes(W + 'models/guard_mixin.py')['WoowDocumentGuardMixin'][1]
        self.assertEqual(self._literal(guard['_inherit']), [self.MIXIN])


class TestTestFilesAreRegistered(unittest.TestCase):
    """The lists that name the test files of the addon agree with the files."""

    def _test_files(self):
        return sorted(name for name in os.listdir(os.path.join(REPO, W + 'tests'))
                      if re.fullmatch(r'test_\w+\.py', name))

    def test_every_test_file_is_imported_by_the_package_or_by_another_test(self):
        imported = set()
        directory = os.path.join(REPO, W + 'tests')
        for name in os.listdir(directory):
            if not name.endswith('.py'):
                continue
            for node in ast.walk(ast.parse(_read(os.path.join(directory, name)))):
                if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None:
                    imported.update(alias.name + '.py' for alias in node.names)
        self.assertEqual(set(self._test_files()) - imported, set())

    def test_every_regression_test_file_is_listed_for_the_gate(self):
        """A ``tests/test_reg_<slug>.py`` of a fix is in sweep.yaml and has a floor in EXPECTED_MIN."""
        cfg = _astlib.load_yaml_file(os.path.join(REPO, '.security/sweep.yaml'))
        floors = json.loads(_read(os.path.join(REPO, W + 'tests/EXPECTED_MIN.json')))['files']
        files = [name for name in self._test_files() if name.startswith('test_reg_')]
        self.assertGreaterEqual(len(files), 7)
        for name in files:
            with self.subTest(file=name):
                self.assertIn(W + 'tests/' + name, cfg['verify']['regression_tests'])
                self.assertIn(name, floors)


class TestYamlSubset(unittest.TestCase):

    SAMPLE = (
        'a: 1\n'
        'b: [x, "y z", {k: v}]\n'
        'c:\n'
        '- one\n'
        '- {route: "/a, /b", methods: [GET], note: "x: y"}\n'
        '- name: n\n'
        '  focus: >-\n'
        '    folded\n'
        '    text\n'
        "d: 'it''s'  # comment\n"
        'e: null\n'
        'f: false\n'
    )

    def test_load_yaml_subset(self):
        data = _astlib.load_yaml(self.SAMPLE)
        self.assertEqual(data['a'], 1)
        self.assertEqual(data['b'], ['x', 'y z', {'k': 'v'}])
        self.assertEqual(data['c'][1], {'route': '/a, /b', 'methods': ['GET'], 'note': 'x: y'})
        self.assertEqual(data['c'][2], {'name': 'n', 'focus': 'folded text'})
        self.assertEqual(data['d'], "it's")
        self.assertIsNone(data['e'])
        self.assertIs(data['f'], False)

    def test_load_yaml_matches_pyyaml_when_available(self):
        try:
            import yaml  # noqa: F401  (not importable under python3 -I)
        except ImportError:
            self.skipTest('PyYAML not importable')
        for relpath in ('.security/sweep.yaml', 'tools/security_matrix.yaml', 'tools/sql_allowlist.yaml',
                        '.github/workflows/security.yml'):
            with self.subTest(path=relpath):
                text = _read(os.path.join(REPO, relpath))
                expected = yaml.safe_load(text)
                if relpath.endswith('security.yml'):
                    expected = {('on' if k is True else k): v for k, v in expected.items()}
                self.assertEqual(_astlib.load_yaml(text), expected)


if __name__ == '__main__':
    unittest.main()
