# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Independent behavioral probes for Odoo 18 core access assumptions.

The CA numbers correspond to the design test plan.  These tests deliberately
use ORM operations, rather than inspecting implementation source text.
"""

import base64
import os
from types import SimpleNamespace
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('woow_docmgmt_core_assumptions_access')
class TestCoreAssumptionsAccess(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.member_group = cls.env.ref('woow_document_management.group_woow_document_user')
        cls.member = cls.env['res.users'].sudo().create({
            'name': 'CA access member', 'login': 'wdoc-ca-access-member',
            'groups_id': [Command.set([cls.member_group.id])],
        })
        cls.folder = cls.env['woow.document.folder'].sudo().create({
            'name': 'CA access root', 'owner_id': cls.env.ref('base.user_admin').id,
        })

    def _grant(self, folder, level='read'):
        return self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'user',
            'user_id': self.member.id, 'level': level,
        })

    def test_ca02_res_field_check_maps_to_record(self):
        """CA-02: field attachment read follows its record; create/unlink need write."""
        self._grant(self.folder)
        self.env.flush_all()
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'ca-field.bin', 'raw': b'ca-field-payload',
            'res_model': 'woow.document.folder', 'res_id': self.folder.id,
            'res_field': 'image_128',
        })
        checked = attachment.with_user(self.member)
        checked.check('read')  # Odoo returns None on successful checks.
        for mode in ('create', 'unlink'):
            with self.subTest(mode=mode), self.assertRaises(AccessError):
                checked.check(mode)
        self.assertTrue(attachment.exists())

    def test_ca05_unlink_rows_before_attachments(self):
        """CA-05: deleting a document succeeds despite its restrict FK to attachment."""
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'CA unlink folder', 'owner_id': self.env.ref('base.user_admin').id,
        })
        document = self.env['woow.document'].sudo().create({
            'name': 'ca-unlink.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'CA unlink payload'),
        })
        self.env.flush_all()
        attachment_id = document.attachment_id.id
        self.assertTrue(attachment_id, 'fixture must have a stored binary attachment')
        document_id = document.id
        document.unlink()
        self.assertFalse(self.env['woow.document'].sudo().browse(document_id).exists())
        self.assertFalse(self.env['ir.attachment'].sudo().browse(attachment_id).exists())

    def test_ca07_binary_attachment_sudo(self):
        """CA-07: portal Binary create/read works despite no attachment ACL."""
        portal = self.env['res.users'].sudo().create({
            'name': 'CA binary portal', 'login': 'wdoc-ca-binary-portal',
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])],
        })
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'partner',
            'partner_id': portal.partner_id.id, 'level': 'upload',
        })
        self.env.flush_all()
        payload = b'CA portal binary payload'
        document = self.env['woow.document'].with_user(portal).create({
            'name': 'ca-portal.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(payload),
        })
        self.assertEqual(base64.b64decode(document.datas), payload)
        attachment = document.sudo().attachment_id
        self.assertTrue(attachment)
        with self.assertRaises(AccessError):
            attachment.with_user(portal).check('read')

    def test_ca08_record_to_stream_checks_access(self):
        """CA-08: route lookup checks record access before path streaming."""
        self._grant(self.folder)
        hidden_folder = self.env['woow.document.folder'].sudo().create({
            'name': 'CA stream hidden folder',
            'owner_id': self.env.ref('base.user_admin').id,
        })
        payload = b'CA stream payload'
        documents = self.env['woow.document'].sudo()
        visible = documents.create({
            'name': 'ca-visible.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(payload),
        })
        hidden = documents.create({
            'name': 'ca-hidden.txt', 'folder_id': hidden_folder.id,
            'datas': base64.b64encode(payload),
        })
        self.env.flush_all()
        visible_as_member = visible.with_user(self.member)
        hidden_as_member = hidden.with_user(self.member)
        self.assertTrue(visible_as_member.has_access('read'))
        self.assertFalse(hidden_as_member.has_access('read'))
        binary = self.env['ir.binary'].with_user(self.member)
        with self.assertRaises(AccessError):
            binary._find_record(res_model='woow.document', res_id=hidden.id, field='datas')
        checked = binary._find_record(res_model='woow.document', res_id=visible.id, field='datas')
        self.assertEqual(checked.id, visible.id)
        # _to_http_stream resolves the filestore through request.db, even in a TC.
        with patch('odoo.addons.base.models.ir_attachment.request',
                   SimpleNamespace(db=self.env.cr.dbname)):
            stream = binary._get_stream_from(
                checked, 'datas', filename='ca-visible.txt')
            self.assertEqual(stream.type, 'path')
            self.assertTrue(os.path.isfile(stream.path))
            self.assertEqual(stream.size, len(payload))
            with open(stream.path, 'rb') as source:
                self.assertEqual(source.read(), payload)
            # _get_stream_from is a low-level converter and does not check record rules.
            # The addon's direct route checks access itself before calling it.
            self.assertFalse(hidden_as_member.has_access('read'))
            hidden_stream = binary._get_stream_from(
                hidden_as_member, 'datas', filename='ca-hidden.txt')
            self.assertEqual(hidden_stream.type, 'path')
            self.assertEqual(hidden_stream.size, len(payload))
            with open(hidden_stream.path, 'rb') as source:
                self.assertEqual(source.read(), payload)
            self.assertFalse(hidden_as_member.has_access('read'))

    def test_ca10_create_skips_field_groups(self):
        """CA-10: core create accepts a hidden field that core write rejects."""
        messages = self.env['mail.message'].with_user(self.member)
        self.assertNotIn('tracking_value_ids', messages.fields_get())
        message = messages.create({
            'body': 'CA field group probe', 'message_type': 'comment',
            'tracking_value_ids': [Command.clear()],
        })
        self.assertFalse(message.sudo().tracking_value_ids)
        with self.assertRaises(AccessError):
            message.write({'tracking_value_ids': [Command.clear()]})

    def test_ca11_domain_on_forbidden_field_raises_rule_ok(self):
        """CA-11: a user's hidden-field domain fails, but an ir.rule may use it."""
        self._grant(self.folder)
        self.env.flush_all()
        folders = self.env['woow.document.folder'].with_user(self.member)
        with self.assertRaises(AccessError):
            folders.search([('legal_hold', '=', False)])
        model = self.env['ir.model']._get('woow.document.folder')
        rule = self.env['ir.rule'].sudo().create({
            'name': 'CA hidden field rule', 'model_id': model.id,
            'groups': [Command.set([self.member_group.id])],
            'domain_force': "[('legal_hold', '=', False)]",
            'perm_read': True, 'perm_write': False,
            'perm_create': False, 'perm_unlink': False,
        })
        self.assertTrue(rule.exists())
        self.assertIn(self.folder.id, folders.search([('id', '=', self.folder.id)]).ids)

    def test_ca19_m2o_convert_to_read_sudo_keeps_uid(self):
        """CA-19: a readable child can expose its hidden parent's name via M2O."""
        child = self.env['woow.document.folder'].sudo().create({
            'name': 'CA visible child', 'parent_id': self.folder.id,
            'inherit_access': False, 'owner_id': self.env.ref('base.user_admin').id,
        })
        self._grant(child)
        self.env.flush_all()
        folders = self.env['woow.document.folder'].with_user(self.member)
        self.assertEqual(folders.search_count([('id', '=', self.folder.id)]), 0)
        self.assertEqual(folders.search_count([('id', '=', child.id)]), 1)
        before_uid = folders.env.uid
        parent_id, parent_name = folders.browse(child.id).read(['parent_id'])[0]['parent_id']
        self.assertEqual(parent_id, self.folder.id)
        self.assertEqual(parent_name, self.folder.with_user(self.member).sudo().display_name)
        self.assertEqual(folders.env.uid, before_uid)
        self.assertFalse(folders.env.su)

    def test_ca25_mail_unlink_deletes_message(self):
        """CA-25: deleting a non-notification mail also deletes its message."""
        mail = self.env['mail.mail'].sudo().create({
            'subject': 'CA mail cascade', 'body_html': '<p>probe</p>',
            'email_to': 'recipient@example.test', 'is_notification': False,
        })
        message_id = mail.mail_message_id.id
        self.assertTrue(message_id)
        self.assertTrue(self.env['mail.message'].sudo().browse(message_id).exists())
        mail.unlink()
        self.assertFalse(mail.exists())
        self.assertFalse(self.env['mail.message'].sudo().browse(message_id).exists())

    def test_ca27_mail_context_flags(self):
        """CA-27: creating a mail.thread record with all three flags is silent."""
        folder = self.env['woow.document.folder'].sudo().with_context(
            tracking_disable=True, mail_create_nolog=True,
            mail_create_nosubscribe=True,
        ).create({'name': 'CA silent folder', 'owner_id': self.member.id})
        messages = self.env['mail.message'].sudo().search([
            ('model', '=', folder._name), ('res_id', '=', folder.id),
        ])
        followers = self.env['mail.followers'].sudo().search([
            ('res_model', '=', folder._name), ('res_id', '=', folder.id),
        ])
        self.assertFalse(messages)
        self.assertFalse(followers)
        # A separate ordinary thread proves that chatter persists for this model.
        normal = self.env['woow.document.folder'].sudo().create({
            'name': 'CA normal folder', 'owner_id': self.member.id,
        })
        normal.message_post(body='CA positive control')
        tracked_count = self.env['mail.message'].sudo().search_count([
            ('model', '=', normal._name), ('res_id', '=', normal.id),
        ])
        self.assertGreater(tracked_count, 0)
        folder.with_context(tracking_disable=True).write({'name': 'CA silent rename'})
        self.env.flush_all()
        self.assertFalse(self.env['mail.message'].sudo().search([
            ('model', '=', folder._name), ('res_id', '=', folder.id),
        ]))
