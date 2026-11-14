# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Filing rules: files attached to a kind of record are also filed as documents (lane autofile)."""
import base64
from contextlib import contextmanager
from unittest.mock import patch

from odoo import Command, api
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from .common import plant_default
from ..hooks import uninstall_hook
from ..models import autofile, quota as quota_module
from ..models.autofile import IrAttachmentAutoFile
from ..models.document import SOURCE_CTX_KEY, _SOURCE_SENTINEL
from ..models.upload_policy import WoowUploadRejected
from ..services.constants import CTX_AUTO_FILE, WOOW_MODELS

_MANAGER = 'woow_document_management.group_woow_document_manager'
_USER = 'woow_document_management.group_woow_document_user'


class _Fixture:
    """A document administrator, a plain internal user, a folder and a partner to attach to."""

    def _fixture(self):
        env = self.env
        self.manager = new_test_user(env, login='autofile-manager', groups=_MANAGER)
        self.member = new_test_user(env, login='autofile-member', groups=_USER)
        # the person who attaches the file: no document right at all, may write partners
        self.plain = new_test_user(env, login='autofile-plain',
                                   groups='base.group_user,base.group_partner_manager')
        self.portal = new_test_user(env, login='autofile-portal', groups='base.group_portal')
        self.folder = env['woow.document.folder'].sudo().create({
            'name': 'Autofile folder', 'owner_id': self.manager.id})
        self.tag = env['woow.document.tag'].sudo().create({'name': 'Autofile tag'})
        self.partner = env['res.partner'].create({'name': 'Autofile partner'})
        self.kind = env['ir.model']._get('res.partner')
        self.rules = env['woow.document.auto.rule']
        self.attachments = env['ir.attachment']

    def _rule(self, user=None, **extra):
        values = dict({'name': 'Autofile rule', 'model_id': self.kind.id,
                       'folder_id': self.folder.id}, **extra)
        return self.rules.with_user(user or self.manager).create(values)

    def _second_company(self, name):
        """A company that the document administrator works in as well."""
        other = self.env['res.company'].sudo().create({'name': name})
        self.manager.sudo().company_ids = [Command.link(other.id)]
        return other

    def _attach(self, user=None, name='note.txt', content=b'hello filing', record=None, **extra):
        values = dict({'name': name, 'raw': content, 'type': 'binary',
                       'res_model': 'res.partner', 'res_id': (record or self.partner).id}, **extra)
        return self.attachments.with_user(user or self.plain).create(values)

    def _documents(self, **domain):
        return self.env['woow.document'].with_context(active_test=False).search(
            [('source_res_model', '=', 'res.partner'), ('source_res_id', '=', self.partner.id)]
            + [(key, '=', value) for key, value in domain.items()])


@tagged('woow_docmgmt_autofile')
class TestFilingOnCreate(_Fixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self._fixture()

    def test_a_file_attached_by_a_user_without_any_document_right_is_filed(self):
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': self.member.id,
            'level': 'upload'})
        rule = self._rule(tag_ids=[Command.set([self.tag.id])], owner_id=self.member.id)
        attachment = self._attach()
        document = self._documents()
        self.assertEqual(len(document), 1)
        self.assertEqual(document.name, 'note.txt')
        self.assertEqual(document.folder_id, self.folder)
        self.assertEqual(document.sudo().tag_ids, self.tag)
        self.assertEqual(document.owner_id, self.member)
        self.assertEqual(document.source_res_model, 'res.partner')
        self.assertEqual(document.source_res_id, self.partner.id)
        self.assertEqual(document.source_attachment_id, attachment)
        self.assertEqual(document.auto_rule_id, rule)
        self.assertEqual(base64.b64decode(document.datas), b'hello filing')
        # the stored file is shared by checksum
        self.assertEqual(document.attachment_id.store_fname, attachment.store_fname)
        self.assertEqual(document.attachment_id.checksum, attachment.checksum)
        # the audit row carries ids only
        self.env.flush_all()
        log = self.env['woow.document.log'].search([('event', '=', 'auto_file')])
        self.assertEqual(len(log), 1)
        self.assertEqual(log.document_id, document)
        self.assertEqual(log.folder_id, self.folder)
        self.assertEqual(log.user_id, self.plain)
        self.assertEqual(log.detail, {'rule': rule.id})
        # the person who attached the file learns nothing of the document
        with self.assertRaises(AccessError):
            document.with_user(self.plain).read(['name'])

    def test_a_document_without_owner_is_filed_when_the_rule_names_none(self):
        self._rule()
        self._attach()
        self.assertFalse(self._documents().owner_id)

    def test_two_rules_of_one_kind_each_file_their_own_document(self):
        other = self.env['woow.document.folder'].sudo().create({
            'name': 'Second autofile folder', 'owner_id': self.manager.id})
        first = self._rule()
        second = self._rule(name='Second', folder_id=other.id)
        self._attach()
        documents = self._documents()
        self.assertEqual(len(documents), 2)
        self.assertEqual(documents.mapped('auto_rule_id'), first | second)
        self.assertEqual(documents.mapped('folder_id'), self.folder | other)

    def test_kinds_without_a_rule_are_not_filed(self):
        self._rule()
        company = self.env.company
        self.attachments.create({'name': 'other.txt', 'raw': b'other kind', 'type': 'binary',
                                 'res_model': 'res.company', 'res_id': company.id})
        self.assertFalse(self.env['woow.document'].search([('source_res_model', '=', 'res.company')]))
        self.assertFalse(self.env['woow.document'].search([]))

    def test_attachments_that_are_not_plain_files_of_a_record_are_not_filed(self):
        self._rule()
        self._attach(name='link', type='url', raw=False, url='https://example.com/x')
        self._attach(name='public.txt', public=True)
        self._attach(name='empty.txt', content=b'')
        self.attachments.create({'name': 'free.txt', 'raw': b'no record', 'type': 'binary'})
        self.attachments.create({'name': 'zero.txt', 'raw': b'zero', 'type': 'binary',
                                 'res_model': 'res.partner', 'res_id': 0})
        self.partner.image_1920 = base64.b64encode(
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00'
            b'\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7'
            b'5\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82')
        field_file = self.attachments.search([
            ('res_model', '=', 'res.partner'), ('res_id', '=', self.partner.id),
            ('res_field', '=', 'image_1920')])
        self.assertTrue(field_file)
        self.assertFalse(self.env['woow.document'].search([]))
        self._attach(name='real.txt')
        self.assertEqual(self._documents().mapped('name'), ['real.txt'])

    def test_a_file_the_policy_refuses_does_not_break_the_attachment(self):
        self._rule()
        attachment = self._attach(name='program.exe', content=b'MZ bad')
        self.assertTrue(attachment.exists())
        self.assertFalse(self._documents())
        # and the next one is filed normally
        self._attach(name='fine.txt')
        self.assertEqual(self._documents().mapped('name'), ['fine.txt'])

    def test_a_file_above_the_size_limit_is_not_read_and_not_filed(self):
        self._rule()
        self.env['ir.config_parameter'].set_param(
            'woow_document_management.internal_max_upload_mb', '1')
        attachment = self._attach(name='big.txt', content=b'x' * (1024 * 1024 + 1))
        self.assertTrue(attachment.exists())
        self.assertFalse(self._documents())

    def test_a_full_quota_does_not_break_the_attachment(self):
        self._rule()
        self.env['ir.config_parameter'].set_param('woow_document_management.quota_mb', '1')
        quota = type(self.env['woow.document.quota'])
        # the stored bytes that the quota measures are those of other connections' commits
        with patch.object(quota, '_usage_bytes', return_value=10 * 1024 * 1024):
            attachment = self._attach(name='quota.txt', content=b'quota')
            self.assertTrue(attachment.exists())
            self.assertFalse(self._documents(name='quota.txt'))
            # the same quota refuses a person who uploads a document by hand
            with self.assertRaises(WoowUploadRejected):
                self.env['woow.document'].with_user(self.manager).create({
                    'name': 'direct.txt', 'folder_id': self.folder.id,
                    'datas': base64.b64encode(b'direct')})

    def test_a_folder_in_the_trash_does_not_break_the_attachment(self):
        self._rule()
        self.folder.sudo().action_trash()
        attachment = self._attach()
        self.assertTrue(attachment.exists())
        self.assertFalse(self._documents())

    def test_any_failure_of_the_filing_is_swallowed_after_its_savepoint_is_rolled_back(self):
        self._rule()
        document_model = type(self.env['woow.document'])
        original = document_model.create

        def broken(model, vals_list):
            original(model, vals_list)         # the document and its file are written ...
            raise RuntimeError('boom')         # ... and the filing fails afterwards

        with patch.object(document_model, 'create', broken):
            attachment = self._attach(name='boom.txt')
        self.assertTrue(attachment.exists())
        self.assertFalse(self._documents())
        # nothing of the failed filing is left (no half document, no stray file of it)
        self.assertFalse(self.env['woow.document'].search([('name', '=', 'boom.txt')]))
        self.assertFalse(self.attachments.search([('res_model', '=', 'woow.document'),
                                                  ('name', '=', 'boom.txt')]))
        self._attach(name='after.txt')
        self.assertEqual(self._documents().mapped('name'), ['after.txt'])

    def test_filing_never_triggers_filing(self):
        rule = self._rule()
        self._attach()
        self.assertEqual(len(self._documents()), 1)
        # the files of the documents are attachments of the module's own model, never refiled
        self.assertFalse(self.attachments.search([('res_model', '=', 'woow.document')]) - (
            self._documents().attachment_id))
        # inside a filing no rule runs, whoever the attachment is for
        inside = rule._woow_elevated().env['ir.attachment']
        inside.create({'name': 'inner.txt', 'raw': b'inner', 'type': 'binary',
                       'res_model': 'res.partner', 'res_id': self.partner.id})
        self.assertEqual(self._documents().mapped('name'), ['note.txt'])

    def test_a_context_flag_of_a_client_does_not_switch_filing_off(self):
        self._rule()
        for value in (True, 1, 'True', autofile._AUTO_FILE_SENTINEL.__class__):
            with self.subTest(value=value):
                self.attachments.with_user(self.plain).with_context(**{CTX_AUTO_FILE: value}).create({
                    'name': 'flag-%s.txt' % id(value), 'raw': b'flag', 'type': 'binary',
                    'res_model': 'res.partner', 'res_id': self.partner.id})
        self.assertEqual(len(self._documents()), 4)

    def test_the_company_of_the_folder_limits_what_a_rule_files(self):
        other = self._second_company('Autofile other company')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Other company folder', 'owner_id': False, 'company_id': other.id})
        self._rule(user=self.env.ref('base.user_root'), folder_id=folder.id)
        mine = self.env['res.partner'].create({'name': 'Mine', 'company_id': self.env.company.id})
        theirs = self.env['res.partner'].create({'name': 'Theirs', 'company_id': other.id})
        self._attach(name='mine.txt', record=mine)
        self.assertFalse(self.env['woow.document'].search([('name', '=', 'mine.txt')]))
        self.attachments.sudo().with_company(other).create({
            'name': 'theirs.txt', 'raw': b'theirs', 'type': 'binary',
            'res_model': 'res.partner', 'res_id': theirs.id})
        self.assertEqual(self.env['woow.document'].search([]).mapped('name'), ['theirs.txt'])

    def test_one_call_files_no_more_than_one_batch(self):
        self._rule()
        with patch.object(autofile, 'AUTO_FILE_BACKFILL_BATCH', 3):
            self.attachments.create([
                {'name': 'many-%d.txt' % number, 'raw': b'many %d' % number, 'type': 'binary',
                 'res_model': 'res.partner', 'res_id': self.partner.id} for number in range(5)])
        self.assertEqual(len(self._documents()), 3)


    def test_a_file_of_a_chatter_post_is_filed_when_the_post_moves_it_onto_the_record(self):
        self._rule()
        pending = self.attachments.with_user(self.plain).create({
            'name': 'chat.txt', 'raw': b'from the chatter', 'type': 'binary',
            'res_model': 'mail.compose.message', 'res_id': 0})
        self.assertFalse(self._documents())
        self.partner.with_user(self.plain).message_post(
            body='with a file', attachment_ids=[pending.id])
        self.assertEqual(pending.res_model, 'res.partner')
        document = self._documents()
        self.assertEqual(document.mapped('name'), ['chat.txt'])
        self.assertEqual(document.source_attachment_id, pending)

    def test_a_file_that_lands_on_a_record_later_is_filed_once(self):
        self._rule()
        attachment = self._attach(name='later.txt', res_id=0, record=self.partner)
        attachment.res_id = 0
        self.assertFalse(self._documents())
        attachment.write({'res_id': self.partner.id})
        self.assertEqual(self._documents().mapped('name'), ['later.txt'])
        attachment.write({'res_id': self.partner.id, 'res_model': 'res.partner'})
        attachment.write({'name': 'renamed.txt'})
        self.assertEqual(len(self._documents()), 1)

    def test_a_write_to_another_field_costs_nothing_and_files_nothing(self):
        self._rule()
        attachment = self._attach(name='plain.txt')
        self.partner.sudo().write({'name': 'Renamed autofile partner'})
        attachment.write({'description': 'x'})
        self.assertEqual(len(self._documents()), 1)


@tagged('woow_docmgmt_autofile')
class TestRuleCache(_Fixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self._fixture()

    def test_the_cache_follows_every_change_of_a_rule(self):
        state = self.rules._woow_auto_state
        self.assertEqual(state(), ({}, False))
        rule = self._rule()
        self.assertEqual(state()[0], {'res.partner': ((rule.id, self.env.company.id),)})
        self.assertTrue(state()[1])
        self._attach(name='one.txt')
        rule.active = False
        self.assertEqual(state()[0], {})
        self._attach(name='two.txt')
        rule.active = True
        self._attach(name='three.txt')
        rule.trash_on_delete = False
        self.assertFalse(state()[1])
        rule.model_id = self.env['ir.model']._get('res.company')
        self.assertEqual(state()[0], {'res.company': ((rule.id, self.env.company.id),)})
        self._attach(name='four.txt')
        rule.unlink()
        self.assertEqual(state(), ({}, False))
        self._attach(name='five.txt')
        self.assertEqual(sorted(self._documents().mapped('name')), ['one.txt', 'three.txt'])

    def test_a_rule_that_is_created_in_a_company_keeps_its_company_in_the_cache(self):
        other = self._second_company('Cache company')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Cache folder', 'owner_id': False, 'company_id': other.id})
        rule = self._rule(user=self.env.ref('base.user_root'), folder_id=folder.id)
        self.assertEqual(self.rules._woow_auto_state()[0], {'res.partner': ((rule.id, other.id),)})

    @contextmanager
    def _hook_removed(self):
        """``create`` and ``unlink`` of the attachment without the hook of this module.

        The rest of the chain stays as it is (other installed modules extend ``ir.attachment`` too
        and may be loaded after this one), so the only difference between a call with and without
        this context is the hook itself.
        """
        @api.model_create_multi
        def create(this, vals_list):
            return super(IrAttachmentAutoFile, this).create(vals_list)

        def unlink(this):
            return super(IrAttachmentAutoFile, this).unlink()

        with patch.object(IrAttachmentAutoFile, 'create', create), \
                patch.object(IrAttachmentAutoFile, 'unlink', unlink):
            yield

    def test_the_query_count_of_an_attachment_without_any_rule_is_unchanged(self):
        model = self.attachments.with_user(self.plain)
        values = {'name': 'count.txt', 'raw': b'count', 'type': 'binary',
                  'res_model': 'res.partner', 'res_id': self.partner.id}
        model.create(dict(values))                      # warm every cache of the core
        self.env.flush_all()

        def queries(call):
            self.env.flush_all()
            before = self.env.cr.sql_log_count
            call()
            self.env.flush_all()
            return self.env.cr.sql_log_count - before

        with self._hook_removed():
            baseline = queries(lambda: model.create([dict(values)]))
        with_hook = queries(lambda: model.create([dict(values)]))
        self.assertEqual(with_hook, baseline)
        # the very first call after a change of the rules reads them once
        self.env.registry.clear_cache()
        with self._hook_removed():
            cold_baseline = queries(lambda: model.create([dict(values)]))
        self.env.registry.clear_cache()
        cold = queries(lambda: model.create([dict(values)]))
        # the one read of the rules, inside a savepoint of its own
        self.assertEqual(cold - cold_baseline, 3)
        # deleting an attachment costs nothing either when no rule exists
        rows = [model.create(dict(values)) for _index in range(3)]
        self.env.invalidate_all()
        queries(lambda: rows[0].unlink())                  # warms what both calls below read
        self.env.invalidate_all()
        with self._hook_removed():
            baseline = queries(lambda: rows[1].unlink())
        self.env.invalidate_all()
        with_hook = queries(lambda: rows[2].unlink())
        self.assertEqual(with_hook, baseline)


@tagged('woow_docmgmt_autofile')
class TestNeverBreaksAnAttachment(_Fixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self._fixture()

    def test_rules_that_cannot_be_read_do_not_break_attachments(self):
        self._rule()
        rule_model = type(self.rules)
        with patch.object(rule_model, '_woow_auto_state', side_effect=RuntimeError('no table')):
            attachment = self._attach(name='blind.txt')
            attachment.write({'res_id': self.partner.id})
            attachment.unlink()
        self.assertFalse(self._documents())

    def test_the_uninstall_hook_removes_the_folders_with_their_rules(self):
        self._rule()
        self._attach()
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_document_management.uninstall_confirm',
                         params.get_param('database.uuid'))
        uninstall_hook(self.env)
        self.assertFalse(self.env['woow.document.auto.rule'].with_context(active_test=False).search([]))
        self.assertFalse(self.folder.exists())


@tagged('woow_docmgmt_autofile')
class TestTrashWithTheOriginal(_Fixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self._fixture()

    def test_deleting_the_original_moves_the_filed_document_to_the_trash(self):
        self._rule()
        attachment = self._attach()
        document = self._documents()
        self.assertTrue(document.active)
        attachment.with_user(self.plain).unlink()
        document = self._documents()
        self.assertEqual(len(document), 1)
        self.assertFalse(document.active)
        self.assertTrue(document.sudo().trashed_date)
        self.assertFalse(document.source_attachment_id)
        self.env.flush_all()
        self.assertTrue(self.env['woow.document.log'].search([
            ('event', '=', 'trash'), ('document_id', '=', document.id)]))

    def test_a_document_under_legal_hold_stays(self):
        self._rule()
        attachment = self._attach()
        self._documents().sudo().action_set_legal_hold(True)
        attachment.unlink()
        self.assertTrue(self._documents().active)

    def test_a_rule_that_does_not_ask_for_it_leaves_the_document(self):
        self._rule(trash_on_delete=False)
        attachment = self._attach()
        attachment.unlink()
        self.assertTrue(self._documents().active)

    def test_only_the_documents_of_the_deleted_attachments_go(self):
        self._rule()
        first = self._attach(name='first.txt')
        self._attach(name='second.txt')
        first.unlink()
        self.assertEqual(self._documents(active=True).mapped('name'), ['second.txt'])
        self.assertEqual(self._documents(active=False).mapped('name'), ['first.txt'])

    def test_a_failure_of_the_trash_does_not_stop_the_deletion(self):
        self._rule()
        attachment = self._attach()
        document_model = type(self.env['woow.document'])
        with patch.object(document_model, 'action_trash', side_effect=RuntimeError('boom')):
            attachment.unlink()
        self.assertFalse(attachment.exists())
        self.assertTrue(self._documents().active)

    def test_the_documents_of_an_archived_rule_still_follow_their_rule(self):
        rule = self._rule()
        attachment = self._attach()
        rule.active = False
        attachment.unlink()
        self.assertFalse(self._documents().active)

    def test_one_query_finds_the_documents_and_none_when_no_rule_asks(self):
        rule = self._rule(trash_on_delete=False)
        rows = [self._attach(name='row-%d.txt' % index) for index in range(2)]
        self.env.flush_all()
        before = self.env.cr.sql_log_count
        self.assertEqual(rule._woow_auto_state()[1], False)
        rows[0].unlink()
        none_asked = self.env.cr.sql_log_count - before
        rule.trash_on_delete = True
        self.env.flush_all()
        before = self.env.cr.sql_log_count
        ids = self.env['woow.document.auto.rule']._woow_trash_filed(rows[1].ids)
        self.assertEqual(self.env.cr.sql_log_count - before, 1)
        self.assertEqual(len(ids), 1)
        self.assertGreater(none_asked, 0)


@tagged('woow_docmgmt_autofile')
class TestBackfill(_Fixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self._fixture()

    def _wizard(self, rule, user=None):
        return self.env['woow.document.autofile.wizard'].with_user(user or self.manager).create({
            'rule_id': rule.id})

    def test_the_backfill_files_batches_and_can_be_run_again(self):
        for number in range(7):
            self._attach(name='old-%d.txt' % number, content=b'old %d' % number)
        self._attach(name='old.exe', content=b'MZ bad')
        rule = self._rule()
        self.assertEqual(len(self._documents()), 0)
        with patch.object(autofile, 'AUTO_FILE_BACKFILL_BATCH', 3):
            wizard = self._wizard(rule)
            counts = []
            for _run in range(4):
                action = wizard.action_file()
                self.assertEqual(action['res_model'], 'woow.document.autofile.wizard')
                self.assertEqual(action['res_id'], wizard.id)
                counts.append((wizard.looked, wizard.filed, wizard.refused, wizard.left))
        self.assertEqual(counts, [(3, 3, 0, 5), (3, 3, 0, 2), (2, 1, 1, 0), (1, 0, 1, 0)])
        self.assertEqual(len(self._documents()), 7)
        self.assertEqual(rule.filed_count, 7)
        # idempotent: a whole pass later nothing is filed twice
        with patch.object(autofile, 'AUTO_FILE_BACKFILL_BATCH', 3):
            wizard.action_file()
            wizard.action_file()
        self.assertEqual(len(self._documents()), 7)
        self.assertEqual(sorted(self._documents().mapped('name')),
                         ['old-%d.txt' % number for number in range(7)])
        self.assertEqual(self._documents().mapped('auto_rule_id'), rule)
        self.env.flush_all()
        self.assertEqual(self.env['woow.document.log'].search_count([('event', '=', 'auto_file')]), 7)

    def test_the_backfill_skips_what_a_rule_would_not_file_and_what_is_trashed(self):
        self._attach(name='plain.txt')
        self._attach(name='public.txt', public=True)
        self._attach(name='empty.txt', content=b'')
        self.attachments.create({'name': 'other.txt', 'raw': b'other', 'type': 'binary',
                                 'res_model': 'res.company', 'res_id': self.env.company.id})
        rule = self._rule()
        wizard = self._wizard(rule)
        wizard.action_file()
        self.assertEqual((wizard.looked, wizard.filed), (1, 1))
        self._documents().sudo().action_trash()
        wizard.action_file()
        self.assertEqual((wizard.looked, wizard.filed), (0, 0))
        self.assertEqual(len(self._documents()), 1)

    def test_an_archived_rule_is_not_run(self):
        rule = self._rule()
        rule.active = False
        with self.assertRaises(UserError):
            self._wizard(rule).action_file()

    def test_the_backfill_reads_attachments_the_administrator_cannot_open(self):
        # the attachments of the kind are another person's: the administrator opens none of them
        self.attachments.create({'name': 'secret.txt', 'raw': b'secret', 'type': 'binary',
                                 'res_model': 'res.company', 'res_id': self.env.company.id})
        rule = self._rule(model_id=self.env['ir.model']._get('res.company').id)
        wizard = self._wizard(rule)
        wizard.action_file()
        self.assertEqual(wizard.filed, 1)

    def test_the_wizard_opens_from_the_rule(self):
        rule = self._rule()
        action = rule.with_user(self.manager).action_open_backfill()
        self.assertEqual(action['res_model'], 'woow.document.autofile.wizard')
        wizard = self.env['woow.document.autofile.wizard'].browse(action['res_id'])
        self.assertEqual(wizard.rule_id, rule)


@tagged('woow_docmgmt_autofile')
class TestWhoManagesRules(_Fixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self._fixture()
        self.rule = self._rule()

    def test_only_document_administrators_read_or_change_rules(self):
        self.assertEqual(self.rules.with_user(self.manager).search([]), self.rule)
        for user in (self.member, self.plain, self.portal):
            with self.subTest(user=user.login):
                model = self.rules.with_user(user)
                with self.assertRaises(AccessError):
                    model.search([])
                with self.assertRaises(AccessError):
                    self.rule.with_user(user).read(['name'])
                with self.assertRaises(AccessError):
                    model.create({'name': 'x', 'model_id': self.kind.id,
                                  'folder_id': self.folder.id})
                with self.assertRaises(AccessError):
                    self.rule.with_user(user).write({'name': 'changed'})
                with self.assertRaises(AccessError):
                    self.rule.with_user(user).unlink()
                with self.assertRaises(AccessError):
                    self.rule.with_user(user).action_open_backfill()
                with self.assertRaises(AccessError):
                    self.env['woow.document.autofile.wizard'].with_user(user).create(
                        {'rule_id': self.rule.id})
        self.assertEqual(self.rule.name, 'Autofile rule')

    def test_the_server_fields_of_a_rule_are_not_set_by_a_caller(self):
        rule = self._rule()
        other = self.env['ir.model']._get('res.company')
        for values in ({'res_model': 'ir.attachment'}, {'company_id': self.env.company.id},
                       {'backfill_cursor': 5}):
            with self.subTest(values=values):
                with self.assertRaises(AccessError):
                    rule.with_user(self.manager).write(values)
                with self.assertRaises(AccessError):
                    self._rule(**dict(values, model_id=other.id))
        self.assertEqual(rule.res_model, 'res.partner')
        self.assertEqual(rule.backfill_cursor, 0)

    def test_a_rule_of_another_company_is_not_visible(self):
        other = self._second_company('Rules company')
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Rules company folder', 'owner_id': False, 'company_id': other.id})
        hidden = self._rule(user=self.env.ref('base.user_root'), name='Hidden', folder_id=folder.id)
        self.assertEqual(hidden.company_id, other)
        main = self.env.company
        both = self.rules.with_user(self.manager).with_context(
            allowed_company_ids=[main.id, other.id])
        only_main = self.rules.with_user(self.manager).with_context(allowed_company_ids=[main.id])
        self.assertIn(hidden, both.search([]))
        self.assertNotIn(hidden, only_main.search([]))
        with self.assertRaises(AccessError):
            hidden.with_user(self.manager).with_context(allowed_company_ids=[main.id]).read(['name'])

    def test_the_wizard_of_one_administrator_is_not_another_ones(self):
        second = new_test_user(self.env, login='autofile-manager-2', groups=_MANAGER)
        wizard = self.env['woow.document.autofile.wizard'].with_user(self.manager).create({
            'rule_id': self.rule.id})
        with self.assertRaises(AccessError):
            wizard.with_user(second).read(['looked'])
        with self.assertRaises(AccessError):
            wizard.with_user(second).action_file()

    def test_the_link_fields_of_a_document_are_set_by_the_server_only(self):
        attachment = self._attach()
        document = self._documents()
        for values in ({'source_attachment_id': attachment.id}, {'auto_rule_id': self.rule.id},
                       {'source_attachment_id': False}):
            with self.subTest(values=values):
                with self.assertRaises(AccessError):
                    document.with_user(self.manager).write(values)
                with self.assertRaises(AccessError):
                    self.env['woow.document'].with_user(self.manager).create(dict(
                        values, name='forged.txt', folder_id=self.folder.id,
                        datas=base64.b64encode(b'forged')))
        # and only a document administrator reads them
        with self.assertRaises(AccessError):
            document.with_user(self.member).read(['source_attachment_id'])
        self.assertEqual(document.with_user(self.manager).source_attachment_id, attachment)
        with self.assertRaises(AccessError):
            attachment.with_user(self.plain).read(['woow_filed_document_ids'])

    def test_the_ids_of_the_filed_documents_are_not_defaulted_by_a_caller(self):
        attachment = self._attach()
        plant_default(self.env, 'woow.document', 'auto_rule_id', self.rule.id, user=self.manager)
        document = self.env['woow.document'].with_user(self.manager).create({
            'name': 'plain.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'plain')})
        self.assertFalse(document.auto_rule_id)
        self.assertFalse(document.source_attachment_id)
        self.assertTrue(attachment.exists())

    def test_the_model_is_one_of_the_modules_and_has_no_open_attachments(self):
        self.assertIn('woow.document.auto.rule', WOOW_MODELS)
        with self.assertRaises(AccessError):
            self.attachments.with_user(self.plain).create({
                'name': 'x.txt', 'raw': b'x', 'type': 'binary', 'public': True,
                'res_model': 'woow.document.auto.rule', 'res_id': self.rule.id})

    def test_a_portal_user_gets_nothing_of_the_new_models(self):
        for model in ('woow.document.auto.rule', 'woow.document.autofile.wizard'):
            with self.subTest(model=model):
                self.assertFalse(self.env[model].with_user(self.portal).has_access('read'))
                self.assertFalse(self.env[model].with_user(self.portal).has_access('create'))
        self.assertNotIn('source_attachment_id', self.env['woow.document'].with_user(
            self.portal).fields_get())


@tagged('woow_docmgmt_autofile')
class TestRuleValidation(_Fixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self._fixture()

    def test_only_the_kinds_of_other_apps_can_be_named(self):
        for name in ('ir.attachment', 'ir.model', 'mail.message', 'mail.thread', 'woow.document',
                     'woow.document.folder', 'woow.document.auto.rule',
                     'woow.document.move.wizard', 'base.language.install'):
            with self.subTest(model=name):
                with self.assertRaises(ValidationError):
                    self._rule(model_id=self.env['ir.model']._get(name).id)
        self.assertTrue(self._rule(model_id=self.env['ir.model']._get('res.company').id))

    def test_the_owner_is_a_document_user_who_may_upload_to_the_folder(self):
        stranger = new_test_user(self.env, login='autofile-stranger', groups='base.group_user')
        for owner in (stranger, self.portal, self.member):
            with self.subTest(owner=owner.login):
                with self.assertRaises(ValidationError):
                    self._rule(owner_id=owner.id)
        access = self.env['woow.document.folder.access'].sudo()
        row = access.create({'folder_id': self.folder.id, 'subject_type': 'user',
                             'user_id': self.member.id, 'level': 'read'})
        with self.assertRaises(ValidationError):
            self._rule(owner_id=self.member.id)
        row.level = 'upload'
        self.assertEqual(self._rule(owner_id=self.member.id).owner_id, self.member)
        self.assertEqual(self._rule(owner_id=self.manager.id).owner_id, self.manager)

    def test_an_owner_who_stopped_being_acceptable_is_left_off_the_filed_document(self):
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': self.member.id,
            'level': 'upload'})
        self._rule(owner_id=self.member.id)
        self._attach(name='kept.txt')
        self.assertEqual(self._documents().owner_id, self.member)
        self.env['woow.document.folder.access'].sudo().search(
            [('folder_id', '=', self.folder.id)]).unlink()
        self._attach(name='left.txt')
        self.assertFalse(self._documents(name='left.txt').owner_id)

    def test_the_tags_belong_to_the_company_of_the_folder(self):
        other = self.env['res.company'].sudo().create({'name': 'Tag company'})
        tag = self.env['woow.document.tag'].sudo().create({'name': 'Other tag', 'company_id': other.id})
        with self.assertRaises(ValidationError):
            self._rule(tag_ids=[Command.set([tag.id])])

    def test_a_rule_cannot_be_duplicated_and_follows_its_folder(self):
        rule = self._rule()
        with self.assertRaises(UserError):
            rule.copy()
        self.folder.sudo().action_trash()
        self.folder.sudo()._woow_purge()
        self.assertFalse(rule.exists())
        self.assertEqual(self.rules._woow_auto_state(), ({}, False))

    def test_changes_of_a_rule_are_audited_with_ids_only(self):
        rule = self._rule()
        rule.name = 'Renamed'
        rule.unlink()
        self.env.flush_all()
        rows = self.env['woow.document.log'].search([('event', '=', 'settings_change')])
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row.detail, {'rule': rule.id})
            self.assertEqual(row.folder_id, self.folder)


@tagged('woow_docmgmt_autofile')
class TestReviewFixes(_Fixture, TransactionCase):
    """What the reviewers of the lane found: the company of the record, what the caller may read,
    the quota lock, records that are gone, memory, files imported by hand and the kind picker."""

    def setUp(self):
        super().setUp()
        self._fixture()
        self.main = self.env.company
        self.other = self._second_company('Autofile record company')
        self.folder_main = self.env['woow.document.folder'].sudo().create({
            'name': 'Main company folder', 'owner_id': False, 'company_id': self.main.id})
        self.partner_other = self.env['res.partner'].create({
            'name': 'Other company partner', 'company_id': self.other.id})
        self.root = self.env.ref('base.user_root')

    def _on(self, record, name, **extra):
        values = dict({'name': name, 'raw': name.encode(), 'type': 'binary',
                       'res_model': record._name, 'res_id': record.id}, **extra)
        return self.attachments.sudo().create(values)

    def _filed(self, record):
        return self.env['woow.document'].with_context(active_test=False).search([
            ('source_res_model', '=', record._name), ('source_res_id', '=', record.id)]).mapped('name')

    # -- the company of the record -----------------------------------------------------------------
    def test_the_company_of_the_record_decides_and_not_the_company_of_the_attachment(self):
        self._rule(user=self.root, folder_id=self.folder_main.id)
        # a file of a record of the other company never lands in a folder of the main company,
        # whatever company the attachment says: the one of its maker, none, or a forged one
        self._on(self.partner_other, 'default.txt')
        self._on(self.partner_other, 'none.txt', company_id=False)
        self._on(self.partner_other, 'forged.txt', company_id=self.main.id)
        self.attachments.sudo().with_company(self.main).create({
            'name': 'multi.txt', 'raw': b'multi', 'type': 'binary',
            'res_model': 'res.partner', 'res_id': self.partner_other.id})
        self.assertEqual(self._filed(self.partner_other), [])

    def test_a_record_of_the_company_of_the_rule_is_filed_whatever_the_attachment_says(self):
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Record company folder', 'owner_id': False, 'company_id': self.other.id})
        self._rule(user=self.root, folder_id=folder.id)
        self._on(self.partner_other, 'in-main.txt', company_id=self.main.id)
        self._on(self.partner_other, 'in-none.txt', company_id=False)
        self.assertEqual(sorted(self._filed(self.partner_other)), ['in-main.txt', 'in-none.txt'])
        # a record of no company goes to the rule of any company
        self._on(self.partner, 'shared.txt', company_id=self.other.id)
        self.assertEqual(self._filed(self.partner), ['shared.txt'])

    def test_the_backfill_judges_the_company_of_the_record_too(self):
        self._on(self.partner_other, 'old-other.txt', company_id=self.main.id)
        self._on(self.partner, 'old-shared.txt')
        rule = self._rule(folder_id=self.folder_main.id)
        wizard = self.env['woow.document.autofile.wizard'].with_user(self.manager).create({
            'rule_id': rule.id})
        wizard.action_file()
        self.assertEqual((wizard.looked, wizard.filed, wizard.skipped), (2, 1, 1))
        self.assertEqual(self._filed(self.partner_other), [])
        self.assertEqual(self._filed(self.partner), ['old-shared.txt'])

    # -- what the caller may read ----------------------------------------------------------------------
    def test_a_rule_cannot_name_a_kind_that_its_maker_cannot_read(self):
        kind = self.env['ir.model']._get('res.users.log')
        self.assertFalse(self.env['res.users.log'].with_user(self.manager).has_access('read'))
        with self.assertRaises(ValidationError):
            self._rule(model_id=kind.id)
        rule = self._rule()
        with self.assertRaises(ValidationError):
            rule.with_user(self.manager).write({'model_id': kind.id})

    def test_the_backfill_refuses_a_kind_that_its_caller_cannot_read(self):
        kind = self.env['ir.model']._get('res.users.log')
        rule = self._rule(user=self.root, model_id=kind.id)
        self.env['ir.attachment'].sudo().create({
            'name': 'secret.txt', 'raw': b'top secret', 'type': 'binary',
            'res_model': 'res.users.log', 'res_id': 1})
        wizard = self.env['woow.document.autofile.wizard'].with_user(self.manager).create({
            'rule_id': rule.id})
        with self.assertRaises(AccessError):
            wizard.action_file()
        self.assertFalse(self.env['woow.document'].search([('name', '=', 'secret.txt')]))

    def test_the_backfill_files_only_the_records_that_its_caller_may_read(self):
        hidden = self.env['res.partner'].create({'name': 'Hidden partner'})
        self._on(hidden, 'hidden.txt')
        self._on(self.partner, 'visible.txt')
        self.env['ir.rule'].sudo().create({
            'name': 'Autofile test: no hidden partner', 'model_id': self.kind.id,
            'domain_force': "[('id', '!=', %d)]" % hidden.id})
        rule = self._rule()
        wizard = self.env['woow.document.autofile.wizard'].with_user(self.manager).create({
            'rule_id': rule.id})
        wizard.action_file()
        self.assertEqual((wizard.looked, wizard.filed, wizard.skipped), (2, 1, 1))
        self.assertEqual(self._filed(hidden), [])
        self.assertEqual(self._filed(self.partner), ['visible.txt'])

    # -- records that do not exist -------------------------------------------------------------------------
    def test_a_file_of_a_record_that_does_not_exist_is_not_filed(self):
        self._rule()
        ghost = self.attachments.sudo().create({
            'name': 'ghost.txt', 'raw': b'ghost', 'type': 'binary',
            'res_model': 'res.partner', 'res_id': 987654321})
        self.assertTrue(ghost.exists())
        self.assertFalse(self.env['woow.document'].search([('name', '=', 'ghost.txt')]))
        # the same when an own free attachment is moved onto a record that is not there
        free = self.attachments.sudo().create({'name': 'moved.txt', 'raw': b'moved', 'type': 'binary'})
        free.write({'res_model': 'res.partner', 'res_id': 987654322})
        self.assertFalse(self.env['woow.document'].search([('name', '=', 'moved.txt')]))
        rule = self.rules.search([])
        wizard = self.env['woow.document.autofile.wizard'].with_user(self.manager).create({
            'rule_id': rule.id})
        wizard.action_file()
        self.assertEqual((wizard.filed, wizard.skipped), (0, 2))

    # -- the quota lock --------------------------------------------------------------------------------------
    def test_a_filing_does_not_hold_the_quota_lock_of_the_site(self):
        self._rule()
        self.env['ir.config_parameter'].set_param('woow_document_management.quota_mb', '100000')
        with patch.object(quota_module, 'lock', wraps=quota_module.lock) as spy:
            self._attach(name='quota-lock.txt')
            self.assertEqual(self._documents().mapped('name'), ['quota-lock.txt'])
            spy.assert_not_called()             # no lock of the quota, held to the end of the caller
            # a document that is uploaded by hand does take it
            self.env['woow.document'].with_user(self.manager).create({
                'name': 'by-hand.txt', 'folder_id': self.folder.id,
                'datas': base64.b64encode(b'by hand')})
            self.assertTrue(spy.called)

    def test_the_quota_marker_is_not_a_context_flag_of_a_client(self):
        from ..models.quota import UNCHARGED_KEY
        self.env['ir.config_parameter'].set_param('woow_document_management.quota_mb', '1')
        quota = type(self.env['woow.document.quota'])
        with patch.object(quota, '_usage_bytes', return_value=10 * 1024 * 1024):
            for value in (True, 1, 'True', object()):
                with self.subTest(value=value), self.assertRaises(WoowUploadRejected):
                    self.env['woow.document'].with_user(self.manager).with_context(
                        **{UNCHARGED_KEY: value}).create({
                            'name': 'direct.txt', 'folder_id': self.folder.id,
                            'datas': base64.b64encode(b'direct')})

    # -- memory --------------------------------------------------------------------------------------------------
    def test_the_backfill_does_not_keep_the_content_of_the_files_in_the_cache(self):
        files = [self._on(self.partner, 'cache-%d.txt' % number) for number in range(3)]
        rule = self._rule()
        rule.with_user(self.manager)._woow_backfill()
        for attachment in files:
            self.assertFalse(self.env.cache.contains(attachment, attachment._fields['raw']))
        self.assertEqual(len(self._filed(self.partner)), 3)

    def test_a_backfill_run_stops_at_the_byte_limit_and_the_next_run_goes_on(self):
        for number in range(4):
            self._on(self.partner, 'size-%d.txt' % number, raw=b'x' * 100 + b'%d' % number)
        rule = self._rule()
        with patch.object(autofile, '_BACKFILL_MAX_BYTES', 250):
            first = rule.with_user(self.manager)._woow_backfill()
            second = rule.with_user(self.manager)._woow_backfill()
            third = rule.with_user(self.manager)._woow_backfill()
        self.assertEqual((first['looked'], first['filed'], first['left']), (2, 2, 2))
        self.assertEqual((second['looked'], second['filed'], second['left']), (2, 2, 0))
        self.assertEqual((third['looked'], third['filed']), (0, 0))
        self.assertEqual(len(self._filed(self.partner)), 4)

    # -- files that the attachment wizard of 18.0.1 imported ------------------------------------------
    def test_a_file_imported_by_hand_before_is_not_filed_a_second_time(self):
        old = self._on(self.partner, 'imported.txt')
        self._on(self.partner, 'fresh.txt')
        self.env['woow.document'].sudo().with_context(**{SOURCE_CTX_KEY: _SOURCE_SENTINEL}).create({
            'name': 'imported.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(old.raw),
            'source_res_model': 'res.partner', 'source_res_id': self.partner.id})
        rule = self._rule()
        wizard = self.env['woow.document.autofile.wizard'].with_user(self.manager).create({
            'rule_id': rule.id})
        wizard.action_file()
        self.assertEqual((wizard.looked, wizard.filed, wizard.skipped), (2, 1, 1))
        self.assertEqual(sorted(self._filed(self.partner)), ['fresh.txt', 'imported.txt'])
        # a file that lands on the record later is judged the same way
        again = self.attachments.sudo().create({'name': 'imported.txt', 'raw': old.raw, 'type': 'binary'})
        again.write({'res_model': 'res.partner', 'res_id': self.partner.id})
        self.assertEqual(sorted(self._filed(self.partner)), ['fresh.txt', 'imported.txt'])

    # -- the kind picker ---------------------------------------------------------------------------------------
    def test_a_document_administrator_without_technical_rights_picks_the_kind_by_name(self):
        self.assertFalse(self.manager.has_group('base.group_system'))
        with self.assertRaises(AccessError):
            self.env['ir.model'].with_user(self.manager).name_search('partner')
        choices = dict(self.rules.with_user(self.manager)._woow_kind_choices())
        self.assertIn('res.partner', choices)
        self.assertNotIn('res.users.log', choices)          # the manager cannot read it
        for name in ('ir.attachment', 'mail.message', 'woow.document', 'woow.document.auto.rule'):
            self.assertNotIn(name, choices)
        rule = self.rules.with_user(self.manager).create({
            'name': 'By name', 'kind_model': 'res.partner', 'folder_id': self.folder.id})
        self.assertEqual(rule.res_model, 'res.partner')
        self.assertEqual(rule.kind_model, 'res.partner')
        self.assertEqual(rule.model_id, self.kind)
        rule.write({'kind_model': 'res.company'})
        self.assertEqual(rule.res_model, 'res.company')
        for name in ('ir.attachment', 'no.such.model', 'woow.document'):
            with self.subTest(kind=name), self.assertRaises(ValidationError):
                self.rules.with_user(self.manager).create({
                    'name': 'Refused', 'kind_model': name, 'folder_id': self.folder.id})

    def test_the_rule_form_does_not_search_the_model_catalogue(self):
        view = self.env.ref('woow_document_management.view_woow_document_auto_rule_form')
        form = self.env['woow.document.auto.rule'].with_user(self.manager).get_view(view.id)
        self.assertIn('kind_model', form['arch'])
        search = self.env['woow.document.auto.rule'].with_user(self.manager).get_view(
            self.env.ref('woow_document_management.view_woow_document_auto_rule_search').id,
            'search')
        self.assertNotIn('name="model_id"', search['arch'])
