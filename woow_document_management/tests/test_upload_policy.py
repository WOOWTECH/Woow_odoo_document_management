# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Server-side upload validation, including content and suffix mismatch."""

import io
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from odoo import Command
from odoo.tests import TransactionCase, tagged

from ..models import upload_policy
from ..models.upload_policy import WoowUploadRejected


@tagged('woow_docmgmt_files')
class TestUploadPolicy(TransactionCase):

    def setUp(self):
        super().setUp()
        self.policy = self.env['woow.document.upload.policy']

    def test_valid_image_and_text(self):
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), 'red').save(stream, format='PNG')
        content = stream.getvalue()
        result = self.policy._validate('../image.png', content)
        self.assertEqual((result.name, result.mimetype, result.size),
                         ('image.png', 'image/png', len(content)))
        text = self.policy._validate('notes.txt', b'hello world\n')
        self.assertEqual(text.mimetype, 'text/plain')
        self.assertTrue(self.policy._inline_allowed('image/png; charset=binary'))

    def test_blocked_suffix_and_mime_disagreement(self):
        for name, content, expected in (
                ('test.svg', b'<svg/>', 'extension'),
                ('test.png', b'<html>payload</html>', 'mime'),
                ('../...', b'hello', 'filename')):
            with self.subTest(name=name), self.assertRaises(WoowUploadRejected) as caught:
                self.policy._validate(name, content)
            self.assertEqual(caught.exception.code, expected)

    def test_actual_length_obeys_typed_setting(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.internal_max_upload_mb', '1')
        with self.assertRaises(WoowUploadRejected) as caught:
            self.policy._validate('large.txt', b'a' * (1024 * 1024 + 1))
        self.assertEqual(caught.exception.code, 'size')

    def test_served_type_never_reflects_unrecognised_mime(self):
        self.assertEqual(self.policy._served_mimetype(
            SimpleNamespace(mimetype='text/html')), 'application/octet-stream')
        self.assertEqual(self.policy._served_mimetype(
            SimpleNamespace(mimetype='text/plain')), 'text/plain; charset=utf-8')

    def test_portal_requires_libmagic_and_public_cannot_upload(self):
        portal = self.env['res.users'].create({
            'name': 'Upload portal fixture', 'login': 'doc-portal-fixture',
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])],
        })
        with patch('odoo.tools.mimetypes.magic', None):
            with self.assertRaises(WoowUploadRejected) as caught:
                self.policy.with_user(portal)._validate('notes.txt', b'hello')
            self.assertEqual(caught.exception.code, 'no_libmagic')
        with self.assertRaises(WoowUploadRejected) as caught:
            self.policy.with_user(self.env.ref('base.public_user'))._validate('notes.txt', b'hello')
        self.assertEqual(caught.exception.code, 'channel')

    def test_ooxml_type_uses_complete_zip_directory(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('[Content_Types].xml', 'x' * 1200)
            archive.writestr('word/document.xml', '<document/>')
        result = self.policy._validate('report.docx', stream.getvalue())
        self.assertEqual(result.mimetype,
                         'application/vnd.openxmlformats-officedocument.wordprocessingml.document')

    def test_odf_mimetype_member_is_bounded(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            archive.writestr('content.xml', '<document/>')
        self.assertEqual(self.policy._validate('report.odt', stream.getvalue()).mimetype,
                         'application/vnd.oasis.opendocument.text')
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('mimetype', b'x' * (1024 * 1024))
        with self.assertRaises(WoowUploadRejected) as caught:
            self.policy._validate('bomb.odt', stream.getvalue())
        self.assertEqual(caught.exception.code, 'mime')

    def test_excessive_zip_entries_rejected_before_parser(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('[Content_Types].xml', '<Types/>')
            archive.writestr('word/document.xml', '<document/>')
            for number in range(4097):
                archive.writestr('empty/%04d' % number, b'')
        with patch.object(upload_policy.zipfile, 'ZipFile') as parser:
            with self.assertRaises(WoowUploadRejected) as caught:
                self.policy._validate('too-many.docx', stream.getvalue())
            self.assertEqual(caught.exception.code, 'mime')
            parser.assert_not_called()
