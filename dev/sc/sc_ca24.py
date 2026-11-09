# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""CA-24: install and search a trigram-named model without pg_trgm rights."""

import secrets
import tempfile
from pathlib import Path

from sc_ca_runtime import ScenarioError, run


def _source(directory, module, role, marker):
    source = Path(directory) / module
    source.mkdir()
    (source / '__manifest__.py').write_text(
        "{'name': 'CA24 disposable trigram', 'version': '18.0.1.0.0', "
        "'depends': ['base'], 'license': 'LGPL-3', 'installable': True, "
        "'post_init_hook': 'post_init_hook'}\n")
    (source / '__init__.py').write_text(
        "from . import models\n\n"
        "def post_init_hook(env):\n"
        "    env.cr.execute(\"SELECT current_user, "
        "has_database_privilege(current_user, current_database(), 'CREATE'), "
        "EXISTS (SELECT 1 FROM pg_extension WHERE extname='pg_trgm')\")\n"
        "    if env.cr.fetchone() != ('%s', False, False):\n" % role +
        "        raise RuntimeError('CA24_ROLE_OR_EXTENSION_CHANGED')\n"
        "    record = env['%s.record'].create({'name': '%s'})\n" % (module, marker) +
        "    found = env['%s.record'].search([('name', 'ilike', '%%needle%%')])\n" % module +
        "    if found.ids != [record.id]:\n"
        "        raise RuntimeError('CA24_NAME_SEARCH_FAILED')\n")
    (source / 'models.py').write_text(
        "from odoo import fields, models\n\n"
        "class TrigramProbe(models.Model):\n"
        "    _name = '%s.record'\n" % module +
        "    _description = 'CA24 disposable trigram probe'\n"
        "    name = fields.Char(required=True, index='trigram')\n")
    return source


def check(sandbox):
    module = 'wdoc_ca24_' + secrets.token_hex(5)
    role = 'wdoc_ca24_role_' + secrets.token_hex(5)
    password = secrets.token_hex(20)
    marker = 'ca24needle' + secrets.token_hex(5)
    synced = False
    role_created = False
    try:
        with tempfile.TemporaryDirectory(prefix='wdoc-ca24-') as directory:
            source = _source(directory, module, role, marker)
            sandbox.call('sync', str(source), module, timeout=180)
            synced = True
            sandbox.copy()
            sandbox.call('psql', sandbox.db, '-XAtc',
                         'DROP EXTENSION IF EXISTS pg_trgm CASCADE', timeout=180)
            sandbox.call('psql', 'postgres', '-XAtc',
                         "CREATE ROLE %s LOGIN PASSWORD '%s' NOSUPERUSER "
                         "NOCREATEDB NOCREATEROLE NOREPLICATION" % (role, password))
            role_created = True
            sandbox.call('psql', 'postgres', '-XAtc',
                         'REVOKE CREATE ON DATABASE %s FROM PUBLIC' % sandbox.db)
            sandbox.call('psql', 'postgres', '-XAtc',
                         'GRANT CONNECT ON DATABASE %s TO %s' % (sandbox.db, role))
            sandbox.call('psql', sandbox.db, '-XAtc',
                         'GRANT USAGE, CREATE ON SCHEMA public TO %s' % role)
            sandbox.call('psql', sandbox.db, '-XAtc',
                         'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO %s' % role)
            sandbox.call('psql', sandbox.db, '-XAtc',
                         'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO %s' % role)
            privilege = sandbox.call('psql', sandbox.db, '-XAtc',
                                     "SELECT rolsuper, "
                                     "has_database_privilege(rolname, current_database(), 'CREATE'), "
                                     "EXISTS (SELECT 1 FROM pg_extension WHERE extname='pg_trgm') "
                                     "FROM pg_roles WHERE rolname='%s'" % role)
            if privilege.strip() != 'f|f|f':
                raise ScenarioError('extension_privilege_not_removed')

            installed = sandbox.call('odoo', sandbox.db, '-i', module,
                                     '--db_user=' + role, '--db_password=' + password,
                                     timeout=600)
            if 'RC 0' not in installed.splitlines():
                raise ScenarioError('install_result')
            state = sandbox.call('psql', sandbox.db, '-XAtc',
                                 "SELECT state FROM ir_module_module WHERE name='%s'" % module)
            if state.strip() != 'installed':
                raise ScenarioError('module_not_installed')

            found = sandbox.call('psql', sandbox.db, '-XAtc',
                                 "SELECT count(*) FROM %s_record WHERE name='%s'" %
                                 (module, marker))
            if found.strip() != '1':
                raise ScenarioError('post_init_search_unverified')
    finally:
        cleanup_error = None
        if synced:
            try:
                sandbox.call('unsync', module, timeout=180)
            except ScenarioError:
                cleanup_error = ScenarioError('unsync_failed')
        if role_created:
            try:
                sandbox.cleanup()
                sandbox.copy_attempted = False
                sandbox.call('psql', 'postgres', '-XAtc', 'DROP ROLE %s' % role)
            except ScenarioError:
                cleanup_error = ScenarioError('role_cleanup_failed')
        if cleanup_error:
            raise cleanup_error


if __name__ == '__main__':
    raise SystemExit(run('T-CA-24', check))
