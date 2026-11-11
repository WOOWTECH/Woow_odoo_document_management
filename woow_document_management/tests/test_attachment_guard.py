# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import base64

from odoo import Command
from odoo.exceptions import AccessError, MissingError
from odoo.tests import TransactionCase, tagged


@tagged('woow_docmgmt_core')
class TestAttachmentGuard(TransactionCase):

    def test_document_field_cannot_be_forged_or_published(self):
        admin = self.env.ref('base.user_admin')
        root = self.env['woow.document.folder'].sudo().create({'name': 'Attachment root'})
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'Primary image', 'type': 'binary', 'raw': b'file',
            'res_model': 'woow.document.folder', 'res_id': root.id,
            'res_field': 'image_128',
        })
        with self.assertRaises(AccessError):
            attachment.with_user(admin).write({'public': True})
        with self.assertRaises(AccessError):
            attachment.with_user(admin).write({'raw': b'other'})
        with self.assertRaises(AccessError):
            self.env['ir.attachment'].with_user(admin).create({
                'name': 'Forged image', 'type': 'binary', 'raw': b'file',
                'res_model': 'woow.document.folder', 'res_id': root.id,
                'res_field': 'image_128',
            })

    def test_unrelated_attachment_can_be_edited(self):
        admin = self.env.ref('base.user_admin')
        partner = self.env['res.partner'].sudo().create({'name': 'Unrelated target'})
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'Other file', 'type': 'binary', 'raw': b'file',
            'res_model': 'res.partner', 'res_id': partner.id,
        })
        attachment.with_user(admin).write({'name': 'Renamed by admin'})
        self.assertEqual(attachment.name, 'Renamed by admin')
        attachment.with_user(admin).write({'raw': b'other'})
        self.assertEqual(attachment.raw, b'other')

    def test_chatter_attachment_cannot_gain_public_access(self):
        admin = self.env.ref('base.user_admin')
        root = self.env['woow.document.folder'].sudo().create({'name': 'Chatter root'})
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'Discussion file', 'type': 'binary', 'raw': b'file',
            'res_model': 'woow.document.folder', 'res_id': root.id,
        })
        with self.assertRaises(AccessError):
            attachment.with_user(admin).write({'access_token': 'never-public'})
        with self.assertRaises(AccessError):
            attachment.with_user(admin).write({'public': True})

    def test_context_defaults_cannot_forge_document_field_or_public_link(self):
        user = self.env['res.users'].sudo().create({
            'name': 'Restricted attachment user', 'login': 'wdoc-attachment-defaults',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        folder = self.env['woow.document.folder'].sudo().create({'name': 'Restricted folder'})
        attachments = self.env['ir.attachment'].with_user(user)
        for defaults in (
            {'default_res_field': 'image_128'},
            {'default_public': True},
            {'default_access_token': 'untrusted'},
        ):
            with self.subTest(defaults=defaults), self.assertRaises(AccessError):
                attachments.with_context(default_res_model='woow.document.folder',
                                         default_res_id=folder.id, **defaults).create({
                                             'name': 'blocked.txt', 'raw': b'blocked',
                                             'type': 'binary', 'mimetype': 'text/plain',
                                         })
        with self.assertRaises(AccessError):
            attachments.with_context(default_res_model='woow.document.folder',
                                     default_res_id=folder.id).create({
                                         'name': 'blocked.txt', 'raw': b'blocked',
                                         'type': 'binary', 'mimetype': 'text/plain',
                                     })



@tagged('woow_docmgmt_core')
class TestDocumentFileAttachment(TransactionCase):
    """A document is only ever served from the attachment it records."""

    def setUp(self):
        super().setUp()
        group = self.env.ref('woow_document_management.group_woow_document_user')
        users = self.env['res.users'].sudo()
        self.editor = users.create({
            'name': 'File editor', 'login': 'wdoc-file-editor', 'email': 'editor@example.test',
            'groups_id': [Command.set([group.id])]})
        self.reader = users.create({
            'name': 'File reader', 'login': 'wdoc-file-reader', 'email': 'reader@example.test',
            'groups_id': [Command.set([group.id])]})
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'File folder', 'owner_id': self.env.ref('base.user_admin').id})
        for user, level in ((self.editor, 'edit'), (self.reader, 'read')):
            self.env['woow.document.folder.access'].sudo().create({
                'folder_id': self.folder.id, 'subject_type': 'user',
                'user_id': user.id, 'level': level})
        self.document = self.env['woow.document'].sudo().create({
            'name': 'original.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'ORIGINAL-BYTES')})
        self.env.flush_all()
        self.original = self.document.sudo().attachment_id
        self.assertTrue(self.original)

    def _file_attachments(self):
        return self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'woow.document'), ('res_id', '=', self.document.id),
            ('res_field', '=', 'datas')])

    def _assert_original_file(self):
        self.assertEqual(self._file_attachments(), self.original)
        self.assertTrue(self.env['ir.attachment']._woow_document_file_is_unique(self.document.id))
        self.env.invalidate_all()
        self.assertEqual(base64.b64decode(self.document.with_user(self.reader).datas),
                         b'ORIGINAL-BYTES')

    def test_field_attachment_cannot_be_moved_onto_a_document_in_two_steps(self):
        attachments = self.env['ir.attachment'].with_user(self.editor)
        for vals in ({'type': 'binary', 'raw': b'FORGED-BYTES'},
                     {'type': 'url', 'url': 'https://example.invalid/landing'}):
            with self.subTest(kind=vals['type']):
                forged = attachments.create(dict(
                    vals, name='datas', res_model='ir.attachment', res_field='datas'))
                with self.assertRaises(AccessError):
                    forged.write({'res_model': 'woow.document', 'res_id': self.document.id})
                with self.assertRaises(AccessError):
                    forged.write({'res_model': 'woow.document.folder', 'res_id': self.folder.id})
                self._assert_original_file()
        # A plain attachment may still become a chatter attachment of a writable document.
        plain = attachments.create({'name': 'note.txt', 'raw': b'plain', 'type': 'binary',
                                    'res_model': 'mail.compose.message', 'res_id': 0})
        plain.write({'res_model': 'woow.document', 'res_id': self.document.id})
        self.assertEqual((plain.res_model, plain.res_id), ('woow.document', self.document.id))
        self._assert_original_file()

    def test_follower_cannot_post_an_attachment_that_backs_a_field(self):
        document = self.document.with_user(self.reader)
        document.message_subscribe(partner_ids=[self.reader.partner_id.id])
        attachments = self.env['ir.attachment'].with_user(self.reader)
        pending = attachments.create({
            'name': 'datas', 'raw': b'FORGED-VIA-POST', 'type': 'binary',
            'res_model': 'mail.compose.message', 'res_id': 0, 'res_field': 'datas'})
        with self.assertRaises(AccessError):
            document.message_post(body='forged', attachment_ids=[pending.id],
                                  message_type='comment', subtype_xmlid='mail.mt_comment')
        self._assert_original_file()
        plain = attachments.create({
            'name': 'reply.txt', 'raw': b'reply', 'type': 'binary',
            'res_model': 'mail.compose.message', 'res_id': 0})
        message = document.message_post(body='reply', attachment_ids=[plain.id],
                                        message_type='comment', subtype_xmlid='mail.mt_comment')
        self.assertEqual(message.sudo().attachment_ids, plain)
        self._assert_original_file()

    def test_a_second_file_attachment_is_never_streamed(self):
        forged = self.env['ir.attachment'].sudo().create({
            'name': 'datas', 'type': 'binary', 'raw': b'FORGED-BYTES',
            'res_model': 'woow.document', 'res_id': self.document.id, 'res_field': 'datas'})
        self.assertFalse(self.env['ir.attachment']._woow_document_file_is_unique(self.document.id))
        for user in (self.reader, self.env.ref('base.user_admin')):
            with self.subTest(user=user.login), self.assertRaises(MissingError):
                self.env['ir.binary'].with_user(user)._record_to_stream(
                    self.document.with_user(user), 'datas')
        forged.unlink()
        self._assert_original_file()
