# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""T-CA-06: uninstall a temporary mail.thread model in a disposable DB."""

import secrets
import tempfile
from pathlib import Path

from sc_ca_runtime import ScenarioError, run


TABLES = {
    'attachment': ('ir_attachment', 'res_model'),
    'message': ('mail_message', 'model'),
    'follower': ('mail_followers', 'res_model'),
    'activity': ('mail_activity', 'res_model'),
}


def _module_source(directory, module):
    source = Path(directory) / module
    source.mkdir()
    (source / '__manifest__.py').write_text(
        "{'name': 'CA06 disposable model', 'version': '18.0.1.0.0', "
        "'depends': ['mail'], 'license': 'LGPL-3', 'installable': True}\n")
    (source / '__init__.py').write_text('from . import models\n')
    (source / 'models.py').write_text(
        "from odoo import fields, models\n\n"
        "class DisposableThread(models.Model):\n"
        "    _name = '%s.record'\n" % module +
        "    _description = 'CA06 disposable thread'\n"
        "    _inherit = ['mail.thread', 'mail.activity.mixin']\n"
        "    name = fields.Char(required=True)\n")
    return source


def _ids(sandbox, table, column, model):
    output = sandbox.call('psql', sandbox.db, '-XAtc',
                          "SELECT id FROM %s WHERE %s='%s' ORDER BY id" %
                          (table, column, model))
    try:
        return [int(line) for line in output.splitlines() if line]
    except ValueError as exc:
        raise ScenarioError('invalid_sql_result') from exc


def check(sandbox):
    module = 'wdoc_ca06_' + secrets.token_hex(5)
    model = module + '.record'
    synced = False
    try:
        with tempfile.TemporaryDirectory(prefix='wdoc-ca06-') as directory:
            source = _module_source(directory, module)
            sandbox.call('sync', str(source), module, timeout=180)
            synced = True
            sandbox.copy()
            installed = sandbox.call('odoo', sandbox.db, '-i', module, timeout=600)
            if 'RC 0' not in installed.splitlines():
                raise ScenarioError('install_result')

            fixture = """import sys
model = '%s'
record = env[model].sudo().create({'name': 'CA06 disposable record'})
partner = env['res.partner'].sudo().create({'name': 'CA06 disposable partner'})
env['ir.attachment'].sudo().create({
    'name': 'ca06.txt', 'raw': b'ca06', 'res_model': model, 'res_id': record.id})
env['mail.message'].sudo().create({
    'model': model, 'res_id': record.id, 'message_type': 'comment',
    'body': 'CA06 disposable message'})
record.message_subscribe(partner_ids=[partner.id])
record.activity_schedule('mail.mail_activity_data_todo', summary='CA06 disposable activity')
env.cr.commit()
sys.stdout.write('CA06_FIXTURE_READY\\n')
""" % model
            output = sandbox.call('shell', sandbox.db, input=fixture, timeout=180)
            if output.splitlines().count('CA06_FIXTURE_READY') != 1:
                raise ScenarioError('fixture_result')

            before = {}
            for kind, (table, column) in TABLES.items():
                before[kind] = _ids(sandbox, table, column, model)
                if not before[kind]:
                    raise ScenarioError('%s_fixture_missing' % kind)

            uninstall = """import sys
module = env['ir.module.module'].sudo().search([
    ('name', '=', '%s'), ('state', '=', 'installed')], limit=1)
if not module:
    raise RuntimeError('CA06_MODULE_NOT_INSTALLED')
module.button_immediate_uninstall()
sys.stdout.write('CA06_UNINSTALLED\\n')
""" % module
            output = sandbox.call('shell', sandbox.db, input=uninstall, timeout=600)
            if output.splitlines().count('CA06_UNINSTALLED') != 1:
                raise ScenarioError('uninstall_result')
            state = sandbox.call('psql', sandbox.db, '-XAtc',
                                 "SELECT state FROM ir_module_module WHERE name='%s'" % module)
            if state.strip() != 'uninstalled':
                raise ScenarioError('module_still_installed')

            for kind, (table, column) in TABLES.items():
                remaining = _ids(sandbox, table, column, model)
                if remaining:
                    raise ScenarioError('%s_model_rows_remain' % kind)
                ids = ','.join(str(identifier) for identifier in before[kind])
                count = sandbox.call('psql', sandbox.db, '-XAtc',
                                     'SELECT count(*) FROM %s WHERE id IN (%s)' % (table, ids))
                if count.strip() != '0':
                    raise ScenarioError('%s_fixture_rows_remain' % kind)
    finally:
        if synced:
            sandbox.call('unsync', module, timeout=180)


if __name__ == '__main__':
    raise SystemExit(run('T-CA-06', check))
