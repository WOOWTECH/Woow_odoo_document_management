# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

from odoo.exceptions import AccessError
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
