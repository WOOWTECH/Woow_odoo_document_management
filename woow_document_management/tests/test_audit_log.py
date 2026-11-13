# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import base64
import secrets

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..services.client_ip import hmac_ip

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'


@tagged('woow_docmgmt_core')
class TestAuditLog(TransactionCase):

    def test_direct_folder_hold_change_is_audited(self):
        folder = self.env['woow.document.folder'].sudo().create({'name': 'Held folder fixture'})
        manager = self.env['res.users'].sudo().create({
            'name': 'Hold audit manager', 'login': 'wdoc-hold-audit',
            'groups_id': [Command.set([
                self.env.ref('woow_document_management.group_woow_document_manager').id])],
        })
        folder.with_user(manager).write({'legal_hold': True})
        events = self.env['woow.document.log'].sudo().search([
            ('folder_id', '=', folder.id), ('event', '=', 'legal_hold')])
        self.assertEqual(len(events), 1)
        self.assertEqual(events.detail, {'hold': True})

    def test_member_create_change_and_delete_are_audited(self):
        folder = self.env['woow.document.folder'].sudo().create({'name': 'Member audit fixture'})
        member = self.env['res.users'].sudo().create({
            'name': 'Member audit user', 'login': 'wdoc-member-audit',
            'groups_id': [Command.set([
                self.env.ref('woow_document_management.group_woow_document_user').id])],
        })
        line = self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'user',
            'user_id': member.id, 'level': 'read',
        })
        line.write({'level': 'upload'})
        line.unlink()
        events = self.env['woow.document.log'].sudo().search([
            ('folder_id', '=', folder.id), ('event', '=', 'access_change')])
        self.assertEqual(len(events), 3)

    def test_ok_events_are_append_only_for_normal_users(self):
        log = self.env['woow.document.log']
        self.assertEqual(log._log_event('upload', channel='backend', detail={'files': 1}), 1)
        event = log.search([('event', '=', 'upload')], limit=1)
        self.assertEqual(event.detail, {'files': 1})
        admin = self.env.ref('base.user_admin')
        with self.assertRaises(AccessError):
            event.with_user(admin).write({'detail': {'files': 2}})
        with self.assertRaises(AccessError):
            event.with_user(admin).unlink()

    def test_an_event_may_name_a_record_without_a_company(self):
        """A folder shared across companies has no company; its events are kept without one."""
        log = self.env['woow.document.log']
        self.assertEqual(log._log_event('upload', channel='backend', company_id=False), 1)
        self.assertEqual(log._log_event('upload', channel='backend', company_id=None), 1)
        self.assertEqual(log._log_event('upload', channel='backend',
                                        company_id=self.env.company.id), 1)
        rows = log.sudo().search([('event', '=', 'upload')], order='id')
        self.assertEqual(rows.mapped('company_id'),
                         self.env['res.company'].browse() | self.env.company | self.env.company)
        for value in ('1', 1.0, True, [1]):
            with self.subTest(company_id=value), self.assertRaises(ValueError):
                log._log_event('upload', channel='backend', company_id=value)

    def test_audit_detail_rejects_strings(self):
        with self.assertRaises(ValueError):
            self.env['woow.document.log']._log_event(
                'upload', channel='backend', detail={'name': 'private.txt'})

    def test_user_agent_control_characters_are_dropped_before_writing(self):
        """PostgreSQL text cannot hold NUL: the header is cleaned before the row is written."""
        log = self.env['woow.document.log']
        agent = 'Mozilla\x00/5.0\r\n\t(\x1b[31mX11)\x7f' + 'y' * 300
        self.assertEqual(log._log_event('upload', channel='backend', user_agent=agent), 1)
        cleaned = ('Mozilla/5.0([31mX11)' + 'y' * 300)[:256]
        self.env.flush_all()
        self.env.cr.execute("""SELECT event, result, user_agent FROM woow_document_log
                               WHERE user_agent LIKE %s ORDER BY event""", ('Mozilla%',))
        self.assertEqual(self.env.cr.fetchall(), [('upload', 'ok', cleaned)])
        self.assertEqual(len(cleaned), 256)

    def test_events_and_channels_outside_the_vocabulary_are_refused(self):
        log = self.env['woow.document.log']
        before = log.sudo().search_count([])
        for event in ('share_open', 'rate_limited', 'bogus', ''):
            with self.subTest(event=event), self.assertRaises(ValueError):
                log._log_event(event, channel='backend')
        for channel in ('share', 'bogus', ''):
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                log._log_event('upload', channel=channel)
        self.assertEqual(log.sudo().search_count([]), before)
        self.assertEqual(log._log_event('upload', channel='cron'), 1)

    def test_audit_rows_are_read_by_their_user_or_the_company_administrators(self):
        """R-L0, R-L1, R-L2 and the column groups: who reads which audit rows and columns."""
        log = self.env['woow.document.log']
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.log_ip_mode', 'truncate')
        company_a = self.env.company
        company_b = self.env['res.company'].sudo().create({'name': 'Audit reader company B'})
        user = new_test_user(self.env, login='audit_reader', groups=_USER)
        other = new_test_user(self.env, login='audit_reader_other', groups=_USER)
        manager = new_test_user(self.env, login='audit_reader_manager', groups=_MANAGER)
        manager_ab = new_test_user(
            self.env, login='audit_reader_manager_ab', groups=_MANAGER,
            company_ids=[Command.set([company_a.id, company_b.id])])
        portal = new_test_user(self.env, login='audit_reader_portal', groups='base.group_portal')
        plain = new_test_user(self.env, login='audit_reader_plain', groups='base.group_user')

        def logged(owner, company_id, **values):
            """The one row that ``_log_event`` writes for ``owner``."""
            newest = log.sudo().search([], limit=1).id or 0
            log._log_event('download', channel='backend', user_id=owner.id,
                           company_id=company_id, **values)
            row = log.sudo().search([('id', '>', newest)])
            self.assertEqual(len(row), 1)
            return row

        mine = logged(user, company_a.id, ip='203.0.113.5', user_agent='audit-reader-agent',
                      detail={'files': 1})
        theirs = logged(other, company_a.id)
        no_company = logged(user, False)
        elsewhere = logged(user, company_b.id)
        no_user = logged(other, company_a.id)
        no_user.sudo().write({'user_id': False})    # the row of a deleted user keeps no user
        portal_row = logged(portal, company_a.id)
        rows = mine | theirs | no_company | elsewhere | no_user | portal_row

        def seen(reader):
            return log.with_user(reader).search([('id', 'in', rows.ids)])

        # R-L1: a document user reads the rows of their own actions and no other (R-L0 still
        # hides the rows of a company they do not work in, and R-L1 the row of no user).
        self.assertEqual(seen(user), mine | no_company)
        self.assertEqual(seen(other), theirs)
        self.assertEqual(log.with_user(user).search_count([('id', 'in', rows.ids)]), 2)
        for reader, hidden in ((user, theirs), (user, no_user), (user, elsewhere),
                               (other, mine), (manager, elsewhere)):
            with self.subTest(reader=reader.login, row=hidden.id), self.assertRaises(AccessError):
                hidden.with_user(reader).read(['event'])
        self.assertEqual(mine.with_user(user).read(['event'])[0]['event'], 'download')
        # R-L2 with R-L0: a document administrator reads every row of the companies they work in.
        self.assertEqual(seen(manager), rows - elsewhere)
        self.assertEqual(seen(manager_ab), rows)
        # A portal user reads none, not even the row that records their own download.
        as_portal = log.with_user(portal)
        self.assertFalse(as_portal.has_access('read'))
        with self.assertRaises(AccessError):
            as_portal.search([('id', 'in', rows.ids)])
        with self.assertRaises(AccessError):
            portal_row.with_user(portal).read(['event'])
        # An internal user who is in neither document group has no access to the log either.
        as_plain = log.with_user(plain)
        self.assertFalse(as_plain.has_access('read'))
        with self.assertRaises(AccessError):
            as_plain.search([('id', 'in', rows.ids)])
        # The address columns are for administrators: a user reads their own row without them.
        digest = hmac_ip(self.env['woow.document.settings']._audit_ip_key(), '203.0.113.5')
        columns = ('ip_hash', 'ip_display', 'user_agent')
        for reader in (manager, manager_ab):
            values = mine.with_user(reader).read(columns)[0]
            self.assertEqual((values['ip_hash'], values['ip_display'], values['user_agent']),
                             (digest, '203.0.113.0/24', 'audit-reader-agent'))
        for column in columns:
            with self.subTest(column=column), self.assertRaises(AccessError):
                mine.with_user(user).read([column])
        # Nobody writes the log through the ORM. The append-only test above holds the
        # administrator's write and unlink; here a user and an administrator cannot create a
        # row, and a user can neither change nor delete one.
        for reader in (user, manager):
            with self.subTest(create=reader.login), self.assertRaises(AccessError):
                log.with_user(reader).create({'event': 'upload', 'channel': 'backend'})
        with self.assertRaises(AccessError):
            mine.with_user(user).write({'detail': {'files': 2}})
        with self.assertRaises(AccessError):
            mine.with_user(user).unlink()
        self.assertTrue(mine.exists())
        self.assertEqual(mine.sudo().detail, {'files': 1})


@tagged('woow_docmgmt_http')
class TestRecordedSource(HttpCase):
    """The address of a signed-in download is recorded as the settings say."""

    def setUp(self):
        super().setUp()
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Source audit folder', 'owner_id': self.env.ref('base.user_admin').id})
        self.doc = self.env['woow.document'].sudo().create({
            'name': 'source.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'source-content')})
        self.params = self.env['ir.config_parameter'].sudo()
        self.authenticate('admin', 'admin')

    def _digest(self, ip):
        return hmac_ip(self.env['woow.document.settings']._audit_ip_key(), ip)

    def _download(self, path=None, headers=None):
        """The one audit row that a download of the fixture leaves."""
        log = self.env['woow.document.log'].sudo()
        domain = [('event', '=', 'download'), ('document_id', '=', self.doc.id)]
        before = log.search(domain)
        response = self.url_open(path or '/woow_document/doc/%d/download' % self.doc.id,
                                 headers=headers)
        self.assertEqual(response.status_code, 200)
        row = log.search(domain) - before
        self.assertEqual(len(row), 1)
        return row

    def test_the_recorded_source_follows_the_log_mode(self):
        digest = self._digest('127.0.0.1')
        for mode, shown in (('truncate', '127.0.0.0/24'), ('hash', digest), ('none', False)):
            with self.subTest(mode=mode):
                self.params.set_param('woow_document_management.log_ip_mode', mode)
                row = self._download()
                self.assertEqual(row.channel, 'backend')
                self.assertRegex(row.ip_hash, '^[0-9a-f]{64}$')
                self.assertEqual(row.ip_hash, digest)
                self.assertEqual(row.ip_display, shown)

    def test_only_a_trusted_proxy_names_the_recorded_source(self):
        self.params.set_param('woow_document_management.client_ip_header', 'X-Real-IP')
        named = {'X-Real-IP': '203.0.113.9'}
        for trusted, source, shown in (('127.0.0.1/32', '203.0.113.9', '203.0.113.0/24'),
                                       ('', '127.0.0.1', '127.0.0.0/24'),
                                       ('10.0.0.0/8', '127.0.0.1', '127.0.0.0/24')):
            with self.subTest(trusted=trusted):
                self.params.set_param('woow_document_management.trusted_proxies', trusted)
                row = self._download(headers=named)
                self.assertEqual(row.ip_hash, self._digest(source))
                self.assertEqual(row.ip_display, shown)
        # without the header setting the forwarded address is not read, trusted or not
        self.params.set_param('woow_document_management.client_ip_header', '')
        self.params.set_param('woow_document_management.trusted_proxies', '127.0.0.1/32')
        self.assertEqual(self._download(headers=named).ip_hash, self._digest('127.0.0.1'))

    def test_a_portal_download_is_recorded_with_the_portal_channel(self):
        password = secrets.token_urlsafe(16)
        partner = self.env['res.partner'].sudo().create({'name': 'Source audit reader'})
        user = self.env['res.users'].sudo().create({
            'name': 'Source audit reader', 'login': 'wdoc-audit-portal', 'password': password,
            'partner_id': partner.id,
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])]})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.doc.folder_id.id, 'subject_type': 'partner',
            'partner_id': partner.id, 'level': 'read'})
        self.authenticate(user.login, password)
        row = self._download(path='/my/docs/file/%d/download' % self.doc.id)
        self.assertEqual((row.channel, row.user_id), ('portal', user))
        self.assertEqual(row.folder_id, self.doc.folder_id)
        self.assertEqual(row.ip_hash, self._digest('127.0.0.1'))
        self.assertEqual(row.ip_display, '127.0.0.0/24')


@tagged('woow_docmgmt_core')
class TestMemberChangeAudit(TransactionCase):
    """Member changes are recorded once per folder, naming only that folder's lines."""

    def test_each_folder_row_names_only_its_own_lines(self):
        admin = self.env.ref('base.user_admin')
        group = self.env.ref('woow_document_management.group_woow_document_user')
        users = self.env['res.users'].sudo().create([{
            'name': 'Audit member %d' % index, 'login': 'wdoc-audit-member-%d' % index,
            'groups_id': [Command.set([group.id])]} for index in range(2)])
        folders = self.env['woow.document.folder'].sudo().create([{
            'name': 'Audit folder %d' % index, 'owner_id': admin.id} for index in range(3)])
        lines = self.env['woow.document.folder.access'].sudo().create([{
            'folder_id': folder.id, 'subject_type': 'user', 'user_id': user.id,
            'level': 'read'} for folder in folders for user in users])
        log = self.env['woow.document.log'].sudo()

        def rows():
            return {row.folder_id.id: row.detail['member_ids'] for row in log.search([
                ('event', '=', 'access_change'), ('folder_id', 'in', folders.ids),
                ('id', '>', mark)])}

        expected = {folder.id: sorted(lines.filtered(lambda line: line.folder_id == folder).ids)
                    for folder in folders}
        mark = max(log.search([]).ids or [0])
        lines.with_user(admin).write({'level': 'upload'})
        self.assertEqual(rows(), expected)
        mark = max(log.search([]).ids or [0])
        moved = lines.filtered(lambda line: line.folder_id == folders[0])
        target = self.env['woow.document.folder'].sudo().create({
            'name': 'Audit target', 'owner_id': admin.id})
        moved.with_user(admin).write({'folder_id': target.id})
        self.assertEqual(log.search([('event', '=', 'access_change'), ('id', '>', mark)]).mapped(
            lambda row: (row.folder_id.id, row.detail['member_ids'])),
            [(target.id, sorted(moved.ids)), (folders[0].id, sorted(moved.ids))])
        mark = max(log.search([]).ids or [0])
        remaining = lines - moved
        remaining.with_user(admin).unlink()
        self.assertEqual(rows(), {folder.id: expected[folder.id] for folder in folders[1:]})

    def test_audit_id_lists_are_bounded(self):
        log = self.env['woow.document.log']
        log._log_event('access_change', channel='backend', detail={'member_ids': list(range(500))})
        with self.assertRaises(ValueError):
            log._log_event('access_change', channel='backend',
                           detail={'member_ids': list(range(501))})
