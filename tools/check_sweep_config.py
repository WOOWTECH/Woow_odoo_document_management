# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""CFG: sweep config, CI workflow and repo hygiene (security_and_sweep 8, 9, 10, 12; ER-23).

.security/sweep.yaml
  CFG-KEYS      top-level keys only version, scan, always_human, autofix, verify (no private keys)
  CFG-SCAN      scan.exclude only i18n/ and **/__pycache__/; semgrep rulesets include p/python and
                p/secrets; no exclude_rules; gitleaks present
  CFG-HUMAN     always_human.paths superset of the odoo stack defaults and the repo additions;
                severities include critical and high; topics include the repo topics
  CFG-AUTOFIX   autofix disabled, max_prs_per_run 0
  CFG-VERIFY    verify.regression_tests contains the 36 required files, unique, all present
  CFG-LEGACY    no path of the two cancelled modules (ER-02)
.github/workflows/*.yml
  CFG-WORKFLOW  no pull_request_target; top-level permissions contents: read; every action pinned
                to a 40-hex commit SHA
Repo hygiene (T-STAT-12, T-STAT-20, B-3)
  CFG-ATTR      no export-ignore / export-subst in any .gitattributes
  CFG-GITLINK   no .gitmodules, no gitlink (160000) or symlink (120000) entries, no symlinks
  CFG-FILES     required repo files present; .gitignore lists the blocked artefacts; no blocked
                artefact (logs, dumps, keys, .env, odoo.conf, browser test output, agent dirs,
                sweep runs, screenshots outside static/description and static/src/img)
  CFG-TARGET    no private target content in the repo
The odoo stack defaults are restated here (copied 2026-11-07, paths and ruleset names only).
"""
from __future__ import annotations

import fnmatch
import os
import re
import stat
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astlib as A  # noqa: E402

SWEEP = '.security/sweep.yaml'
ALLOWED_TOP = {'version', 'scan', 'always_human', 'autofix', 'verify'}
STACK_EXCLUDE_ALLOWED = {'i18n/', '**/__pycache__/'}
STACK_RULESETS = {'p/python', 'p/secrets'}
STACK_ALWAYS_HUMAN = ['.security/', 'security/', 'migrations/', '__manifest__.py', '*/__manifest__.py']
STACK_SEVERITIES = {'critical', 'high'}
REPO_TOPICS = {'token', 'password', 'sudo', 'access_token', 'session', 'mail.template', 'ir.attachment'}
W = 'woow_document_management/'
REPO_ALWAYS_HUMAN = [
    '*/hooks.py', '.gitattributes', '*/.gitattributes', '.gitmodules', '.github/', 'tools/',
    W + 'models/ir_attachment.py', W + 'models/guard_mixin.py', W + 'models/mail_thread_guard.py',
    W + 'models/document_share.py', W + 'models/share_service.py', W + 'models/document_log.py',
    W + 'models/settings.py', W + 'models/quota.py', W + 'controllers/share.py',
    W + 'controllers/streaming.py', W + 'services/tokens.py', W + 'services/client_ip.py',
    W + 'services/folding.py', W + 'services/locks.py', W + 'static/src/share/',
    W + 'tests/EXPECTED_MIN.json',
]
REGRESSION_TESTS = [W + 'tests/%s.py' % name for name in (
    'test_settings_bounds', 'test_client_ip', 'test_upload_policy', 'test_naming', 'test_quota',
    'test_locks', 'test_core_models', 'test_security_acl', 'test_multicompany',
    'test_attachment_guard', 'test_mail_thread_guard', 'test_uninstall',
    'test_display_name_folding', 'test_folding_service', 'test_url_policy', 'test_zip_builder',
    'test_internal_routes', 'test_native_routes', 'test_share_token', 'test_share_routes',
    'test_mail_share', 'test_trash_lifecycle', 'test_audit_log', 'test_cron', 'test_portal_routes',
    'test_portal_upload', 'test_attachment_overview', 'test_add_attachment', 'test_backend_views',
    'test_backend_wizards', 'test_i18n_complete', 'test_record_rules', 'test_core_assumptions',
    'test_rpc_exposure', 'test_escaping')] + ['tools/tests/test_static_checks.py']
LEGACY_RE = re.compile(r'woow_document_management_(legacy_urls|migrate_sh)')
REQUIRED_FILES = ['LICENSE', 'COPYING', 'README.md', 'CLEANROOM.md', 'SECURITY.md', '.gitignore',
                  '.semgrepignore',
                  '.gitattributes', SWEEP]
GITIGNORE_REQUIRED = ['.playwright-cli/', 'playwright-report/', 'test-results/', '*.log', '*.sql',
                      '*.dump', '.env*', 'odoo.conf', '*.pem', '*.key', '.claude/', '.omnigent*/',
                      'runs/', 'report.md', '__pycache__/']
BLOCKED_DIR_PARTS = ('.playwright-cli', 'playwright-report', 'test-results', '.claude', 'runs')
BLOCKED_FILE_GLOBS = ('*.log', '*.sql', '*.dump', '.env', '.env.*', 'odoo.conf', '*.pem', '*.key',
                      'report.md')
IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')
IMAGE_DIRS = ('/static/description/', '/static/src/img/')
TARGET_MARKERS = ('review_' + 'focus:', 'trust_' + 'boundaries:')
SHA_USES = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$')


def _list(value):
    return value if isinstance(value, list) else []


def check_sweep(root, out):
    path = os.path.join(root, SWEEP)
    if not os.path.exists(path):
        out.append(A.Violation(SWEEP, 1, 'CFG-FILES', '%s is missing' % SWEEP))
        return
    text = A.read_text(path)
    for n, line in enumerate(text.split('\n'), 1):
        if LEGACY_RE.search(line):
            out.append(A.Violation(SWEEP, n, 'CFG-LEGACY', 'cancelled module path (ER-02)'))
    try:
        cfg = A.load_yaml(text)
    except A.YamlError as exc:
        out.append(A.Violation(SWEEP, 1, 'CFG-KEYS', 'cannot parse: %s' % exc))
        return
    if not isinstance(cfg, dict):
        out.append(A.Violation(SWEEP, 1, 'CFG-KEYS', 'top level must be a mapping'))
        return
    extra = set(cfg) - ALLOWED_TOP
    if extra:
        out.append(A.Violation(SWEEP, 1, 'CFG-KEYS', 'keys not allowed: %s' % ', '.join(sorted(map(str, extra)))))
    if cfg.get('version') != 1:
        out.append(A.Violation(SWEEP, 1, 'CFG-KEYS', 'version must be 1'))
    scan = cfg.get('scan') if isinstance(cfg.get('scan'), dict) else {}
    excl = set(_list(scan.get('exclude')))
    if not excl <= STACK_EXCLUDE_ALLOWED:
        out.append(A.Violation(SWEEP, 1, 'CFG-SCAN', 'scan.exclude may only hold %s (found %s)' % (
            sorted(STACK_EXCLUDE_ALLOWED), sorted(excl - STACK_EXCLUDE_ALLOWED))))
    tools = scan.get('tools') if isinstance(scan.get('tools'), dict) else None
    if tools is None:
        out.append(A.Violation(SWEEP, 1, 'CFG-SCAN', 'scan.tools must restate semgrep and gitleaks'))
    else:
        semgrep = tools.get('semgrep') if isinstance(tools.get('semgrep'), dict) else {}
        if not STACK_RULESETS <= set(_list(semgrep.get('rulesets'))):
            out.append(A.Violation(SWEEP, 1, 'CFG-SCAN', 'semgrep rulesets must include p/python and p/secrets'))
        for name, conf in tools.items():
            if isinstance(conf, dict) and 'exclude_rules' in conf:
                out.append(A.Violation(SWEEP, 1, 'CFG-SCAN', '%s.exclude_rules needs a human decision in the private target' % name))
        if not isinstance(tools.get('gitleaks'), dict):
            out.append(A.Violation(SWEEP, 1, 'CFG-SCAN', 'gitleaks must be configured'))
    human = cfg.get('always_human') if isinstance(cfg.get('always_human'), dict) else {}
    paths = set(_list(human.get('paths')))
    missing = [p for p in STACK_ALWAYS_HUMAN + REPO_ALWAYS_HUMAN if p not in paths]
    if missing:
        out.append(A.Violation(SWEEP, 1, 'CFG-HUMAN', 'always_human.paths misses %s' % ', '.join(missing)))
    if not STACK_SEVERITIES <= set(_list(human.get('severities'))):
        out.append(A.Violation(SWEEP, 1, 'CFG-HUMAN', 'severities must include critical and high'))
    if not REPO_TOPICS <= set(_list(human.get('topics'))):
        out.append(A.Violation(SWEEP, 1, 'CFG-HUMAN', 'topics must include %s' % sorted(REPO_TOPICS)))
    autofix = cfg.get('autofix') if isinstance(cfg.get('autofix'), dict) else {}
    if autofix.get('enabled') is not False or autofix.get('max_prs_per_run') != 0:
        out.append(A.Violation(SWEEP, 1, 'CFG-AUTOFIX', 'autofix must be disabled with max_prs_per_run 0'))
    verify = cfg.get('verify') if isinstance(cfg.get('verify'), dict) else {}
    tests = _list(verify.get('regression_tests'))
    if len(tests) != len(set(tests)):
        out.append(A.Violation(SWEEP, 1, 'CFG-VERIFY', 'regression_tests has duplicates'))
    for t in REGRESSION_TESTS:
        if t not in tests:
            out.append(A.Violation(SWEEP, 1, 'CFG-VERIFY', 'regression_tests misses %s' % t))
    for t in tests:
        if not isinstance(t, str) or not os.path.isfile(os.path.join(root, t)):
            out.append(A.Violation(SWEEP, 1, 'CFG-VERIFY', 'regression test %s does not exist' % t))


def check_workflows(root, out):
    wdir = os.path.join(root, '.github', 'workflows')
    if not os.path.isdir(wdir):
        out.append(A.Violation('.github/workflows', 1, 'CFG-WORKFLOW', 'CI workflow is missing'))
        return
    names = sorted(n for n in os.listdir(wdir) if n.endswith(('.yml', '.yaml')))
    if not names:
        out.append(A.Violation('.github/workflows', 1, 'CFG-WORKFLOW', 'CI workflow is missing'))
    for name in names:
        relpath = '.github/workflows/' + name
        text = A.read_text(os.path.join(wdir, name))
        for n, line in enumerate(text.split('\n'), 1):
            code = line.split('#', 1)[0]
            if 'pull_request_target' in code:
                out.append(A.Violation(relpath, n, 'CFG-WORKFLOW', 'pull_request_target is not allowed'))
            m = re.match(r'^\s*(?:-\s*)?uses:\s*["\']?([^"\'\s]+)', code)
            if m and not m.group(1).startswith('./') and not SHA_USES.match(m.group(1)):
                out.append(A.Violation(relpath, n, 'CFG-WORKFLOW', 'action %s must be pinned to a 40-hex SHA' % m.group(1)))
            if re.match(r'^\s*[a-z-]+:\s*write\b', code):
                out.append(A.Violation(relpath, n, 'CFG-WORKFLOW', 'write permissions are not allowed'))
        try:
            data = A.load_yaml(text)
        except A.YamlError as exc:
            out.append(A.Violation(relpath, 1, 'CFG-WORKFLOW', 'cannot parse: %s' % exc))
            continue
        perms = data.get('permissions') if isinstance(data, dict) else None
        if perms != {'contents': 'read'}:
            out.append(A.Violation(relpath, 1, 'CFG-WORKFLOW', 'top-level permissions must be {contents: read}'))


def _repo_files(root):
    """(relpath, abspath) of the files that would be committed."""
    if A.is_git_root(root):
        res = subprocess.run(['git', '-C', root, 'ls-files', '-co', '--exclude-standard', '-z'],
                             capture_output=True, check=True, timeout=60)
        names = sorted(n for n in res.stdout.decode('utf-8', 'replace').split('\0') if n)
        return [(n, os.path.join(root, n)) for n in names if not A.is_fixture(n)]
    return A.iter_files(root)


def check_hygiene(root, out):
    for req in REQUIRED_FILES:
        if not os.path.isfile(os.path.join(root, req)):
            out.append(A.Violation(req, 1, 'CFG-FILES', 'required file %s is missing' % req))
    si = os.path.join(root, '.semgrepignore')
    if os.path.isfile(si):
        for n, line in enumerate(A.read_text(si).splitlines(), 1):
            if line.strip() and not line.lstrip().startswith('#'):
                out.append(A.Violation('.semgrepignore', n, 'CFG-SCAN',
                                       'Semgrep may not exclude source or test files'))
    gi = os.path.join(root, '.gitignore')
    if os.path.isfile(gi):
        entries = {line.strip() for line in A.read_text(gi).split('\n')}
        for req in GITIGNORE_REQUIRED:
            if req not in entries:
                out.append(A.Violation('.gitignore', 1, 'CFG-FILES', '.gitignore misses %s' % req))
    # symlinks anywhere in the work tree (os.walk does not follow them)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != '.git']
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            r = A.rel(root, path)
            if A.is_fixture(r):
                continue
            if os.path.islink(path):
                out.append(A.Violation(r, 1, 'CFG-GITLINK', 'symlinks are not allowed'))
            if name == '.gitmodules':
                out.append(A.Violation(r, 1, 'CFG-GITLINK', '.gitmodules is not allowed (R2b-10)'))
    if A.is_git_root(root):
        for mode, path in A.git_index_entries(root):
            if A.is_fixture(path):
                continue
            if mode == '160000':
                out.append(A.Violation(path, 1, 'CFG-GITLINK', 'gitlink (submodule) entries are not allowed'))
            elif mode == '120000':
                out.append(A.Violation(path, 1, 'CFG-GITLINK', 'symlink entries are not allowed'))
    for relpath, abspath in _repo_files(root):
        if os.path.islink(abspath):
            continue
        parts = relpath.split('/')
        base = parts[-1]
        if base == '.gitattributes':
            for n, line in enumerate((A.try_read_text(abspath) or '').split('\n'), 1):
                if re.search(r'export-(ignore|subst)', line.split('#', 1)[0]):
                    out.append(A.Violation(relpath, n, 'CFG-ATTR', 'export-ignore / export-subst are not allowed (B-3)'))
        if any(p in BLOCKED_DIR_PARTS or p.startswith('.omnigent') for p in parts[:-1]) \
                or any(fnmatch.fnmatch(base, g) for g in BLOCKED_FILE_GLOBS):
            out.append(A.Violation(relpath, 1, 'CFG-FILES', 'blocked artefact must not be committed'))
        if base.lower().endswith(IMAGE_EXTS) and not any(d in '/' + relpath for d in IMAGE_DIRS):
            out.append(A.Violation(relpath, 1, 'CFG-FILES', 'images only in static/description or static/src/img'))
        try:
            if os.path.getsize(abspath) > 5 * 1024 * 1024 or not stat.S_ISREG(os.stat(abspath).st_mode):
                continue
        except OSError:
            continue
        text = A.try_read_text(abspath)
        if text and any(m in text for m in TARGET_MARKERS):
            for n, line in enumerate(text.split('\n'), 1):
                if any(line.lstrip().startswith(m) for m in TARGET_MARKERS):
                    out.append(A.Violation(relpath, n, 'CFG-TARGET', 'private target content must not be in the repo'))
                    break


def check(root, args):
    out = []
    check_sweep(root, out)
    check_workflows(root, out)
    check_hygiene(root, out)
    return out


def main(argv=None):
    return A.run_tool(check, __doc__.splitlines()[0], argv)


if __name__ == '__main__':
    sys.exit(main())
