# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The file of a document is never handed to the core indexers.

``attachment_indexation`` (not a dependency, but any site may install it) unpacks and parses the
members of Office and OpenDocument archives, of any file type and at any declared size, when an
attachment is saved. The upload policy only bounds the central directory of an archive, so the
file of a document is saved without indexing: ``index_content`` stays empty.
"""
import base64
from unittest.mock import patch

from odoo.addons.base.models.ir_attachment import IrAttachment as CoreAttachment
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

_USER = 'woow_document_management.group_woow_document_user'


@tagged('woow_docmgmt_files')
class TestDocumentFilesAreNotIndexed(TransactionCase):

    def setUp(self):
        super().setUp()
        self.uploader = new_test_user(self.env, login='wdoc-index-uploader', groups=_USER)
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Index folder', 'owner_id': self.env.ref('base.user_admin').id})
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': self.uploader.id,
            'level': 'edit'})
        self.env.flush_all()
        self.indexed = []
        original = CoreAttachment._index

        def spy(model, bin_data, file_type, checksum=None):
            self.indexed.append(file_type)
            return original(model, bin_data, file_type, checksum=checksum)

        patcher = patch.object(CoreAttachment, '_index', spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _content(self, document):
        return document.sudo().attachment_id.index_content

    def test_a_document_file_is_saved_and_replaced_without_indexing(self):
        # the spy sees what core would index: an attachment of any other record
        plain = self.env['ir.attachment'].create({
            'name': 'plain.txt', 'datas': base64.b64encode(b'indexed words of a plain file')})
        self.assertEqual(self.indexed, ['text/plain'])
        self.assertIn('indexed words', plain.index_content)
        del self.indexed[:]

        document = self.env['woow.document'].with_user(self.uploader).create({
            'name': 'indexed.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'indexed words of a document')})
        self.assertFalse(self._content(document))
        document.with_user(self.uploader).write({
            'datas': base64.b64encode(b'other indexed words of a document')})
        self.env.flush_all()
        self.assertEqual(base64.b64decode(document.sudo().datas),
                         b'other indexed words of a document')
        self.assertFalse(self._content(document))
        self.assertEqual(self.indexed, [])
