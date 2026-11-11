# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Server-side upload validation, including content and suffix mismatch."""

import base64
import io
import struct
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from odoo import Command
from odoo.tests import TransactionCase, tagged

from ..models import upload_policy
from ..models.upload_policy import WoowUploadRejected


@tagged('woow_docmgmt_files')
class TestRenamePolicy(TransactionCase):
    """renaming a document follows the upload filename policy."""

    def setUp(self):
        super().setUp()
        self.admin = self.env.ref('base.user_admin')
        self.folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Rename folder', 'owner_id': self.admin.id})
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), 'red').save(stream, format='PNG')
        self.png = base64.b64encode(stream.getvalue())
        documents = self.env['woow.document'].sudo()
        self.image = documents.create({
            'name': 'picture.png', 'folder_id': self.folder.id, 'datas': self.png})
        self.text = documents.create({
            'name': 'notes.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'hello')})
        self.other = documents.create({
            'name': 'other.txt', 'folder_id': self.folder.id,
            'datas': base64.b64encode(b'world')})
        self.link = documents.create({
            'name': 'Site', 'folder_id': self.folder.id, 'document_type': 'url',
            'url': 'https://example.test/page'})

    def test_rename_rejects_blocked_unlisted_and_mismatched_extensions(self):
        image = self.image.with_user(self.admin)
        for name, code in (('page.html', 'extension'), ('picture.jpg', 'mime'),
                           ('picture', 'extension'), ('picture.unknownext', 'extension'),
                           ('...', 'filename')):
            with self.subTest(name=name), self.assertRaises(WoowUploadRejected) as caught:
                image.write({'name': name})
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.image.name, 'picture.png')
        self.assertEqual(self.image.mimetype, 'image/png')

    def test_rename_sanitizes_like_upload_and_keeps_matching_extensions(self):
        image = self.image.with_user(self.admin)
        image.write({'name': '\u202ephoto\u202c.png'})
        self.assertEqual(self.image.name, 'photo.png')
        image.write({'name': 'a' * 300 + '.png'})
        self.assertEqual(len(self.image.name), 200)
        self.assertTrue(self.image.name.endswith('.png'))
        image.write({'name': '../CON.png'})
        self.assertEqual(self.image.name, '_CON.png')
        # text/plain content may carry either listed extension of its type.
        self.text.with_user(self.admin).write({'name': 'notes.csv'})
        self.assertEqual(self.text.name, 'notes.csv')
        self.assertEqual(self.text.mimetype, 'text/plain')

    def test_link_rename_only_applies_the_name_rules(self):
        link = self.link.with_user(self.admin)
        link.write({'name': 'Company\x07 site'})
        self.assertEqual(self.link.name, 'Company site')
        link.write({'name': 'index.html'})
        self.assertEqual(self.link.name, 'index.html')
        with self.assertRaises(WoowUploadRejected) as caught:
            link.write({'name': '  '})
        self.assertEqual(caught.exception.code, 'filename')
        self.assertEqual(self.link.name, 'index.html')

    def test_batch_rename_and_unrelated_writes_keep_legacy_names(self):
        self.env.cr.execute('UPDATE woow_document SET name = %s WHERE id = %s',
                            ('legacy\u202e.html', self.text.id))
        self.env.invalidate_all()
        self.text.with_user(self.admin).write({'description': '<p>touched</p>'})
        self.text.with_user(self.admin).write({'name': 'legacy\u202e.html'})
        self.assertEqual(self.text.name, 'legacy\u202e.html')
        mixed = (self.image | self.text).with_user(self.admin)
        with self.assertRaises(WoowUploadRejected) as caught:
            mixed.write({'name': 'shared.png'})
        self.assertEqual(caught.exception.code, 'mime')
        self.assertEqual(self.image.name, 'picture.png')
        self.assertEqual(self.text.name, 'legacy\u202e.html')
        texts = (self.text | self.other).with_user(self.admin)
        texts.write({'name': ' renamed\u202e.txt'})
        self.assertEqual(set(texts.mapped('name')), {'renamed.txt'})

    def test_rename_with_new_content_keeps_the_upload_path(self):
        image = self.image.with_user(self.admin)
        with self.assertRaises(WoowUploadRejected) as caught:
            image.write({'name': 'replaced.html', 'datas': self.png})
        self.assertEqual(caught.exception.code, 'extension')
        image.write({'name': 'replaced.png', 'datas': self.png})
        self.assertEqual((self.image.name, self.image.mimetype), ('replaced.png', 'image/png'))
        image.write({'name': 'swapped.txt', 'datas': base64.b64encode(b'now text')})
        self.assertEqual((self.image.name, self.image.mimetype), ('swapped.txt', 'text/plain'))


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

    @staticmethod
    def _office_zip(entries, *, zip64=None, classic_total=None, classic_size=None):
        """A .docx-shaped ZIP whose end records can be made to disagree.

        ``zip64`` inserts a ZIP64 end record and locator declaring that many headers
        (they are all written); the classic end record keeps ``classic_total`` and
        ``classic_size`` when given.
        """
        names = [b'[Content_Types].xml', b'word/document.xml'] + [
            b'pad/%05d' % index for index in range(max(0, entries - 2))]
        local, headers = b'', b''
        for name in names:
            offset = len(local)
            local += struct.pack('<4s5H3I2H', b'PK\x03\x04', 20, 0, 0, 0, 0, 0, 0, 0,
                                 len(name), 0) + name
            headers += struct.pack('<4s6H3I5H2I', b'PK\x01\x02', 45, 20, 0, 0, 0, 0, 0, 0, 0,
                                   len(name), 0, 0, 0, 0, 0, offset) + name
        directory_at = len(local)
        tail = b''
        if zip64 is not None:
            record_at = directory_at + len(headers)
            tail = struct.pack('<4sQHHIIQQQQ', b'PK\x06\x06', 44, 45, 45, 0, 0, zip64, zip64,
                               len(headers), directory_at)
            tail += struct.pack('<4sIQI', b'PK\x06\x07', 0, record_at, 1)
        total = len(names) if classic_total is None else classic_total
        size = len(headers) if classic_size is None else classic_size
        end = struct.pack('<4s4H2IH', b'PK\x05\x06', 0, 0, total, total, size,
                          directory_at + len(headers) - size, 0)
        return local + headers + tail + end

    def test_zip64_end_record_cannot_lift_the_directory_bound(self):
        crafted = self._office_zip(5000, zip64=5000, classic_total=1, classic_size=46 + 19)
        self.assertEqual(len(zipfile.ZipFile(io.BytesIO(crafted)).infolist()), 5000)
        for name in ('crafted.docx', 'crafted.odt', 'crafted.pdf', 'crafted.txt'):
            with self.subTest(name=name), \
                    patch.object(upload_policy.zipfile, 'ZipFile') as parser, \
                    self.assertRaises(WoowUploadRejected) as caught:
                self.policy._validate(name, crafted)
            self.assertEqual(caught.exception.code, 'mime')
            parser.assert_not_called()
        # The classic record alone may not under-declare the headers ZipFile walks.
        walked = self._office_zip(5000, classic_total=1)
        self.assertEqual(len(zipfile.ZipFile(io.BytesIO(walked)).infolist()), 5000)
        with patch.object(upload_policy.zipfile, 'ZipFile') as parser, \
                self.assertRaises(WoowUploadRejected) as caught:
            self.policy._validate('walked.docx', walked)
        self.assertEqual(caught.exception.code, 'mime')
        parser.assert_not_called()

    def test_small_consistent_zip64_and_plain_archives_are_accepted(self):
        for archive in (self._office_zip(10, zip64=10), self._office_zip(10)):
            with self.subTest(zip64=b'PK\x06\x06' in archive):
                self.assertEqual(len(zipfile.ZipFile(io.BytesIO(archive)).infolist()), 10)
                self.assertEqual(
                    self.policy._validate('consistent.docx', archive).mimetype,
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        dangling = self._office_zip(10)
        end = dangling.rfind(b'PK\x05\x06')
        dangling = dangling[:end] + struct.pack('<4sIQI', b'PK\x06\x07', 0, 0, 1) + dangling[end:]
        with self.assertRaises(WoowUploadRejected) as caught:
            self.policy._validate('dangling.docx', dangling)
        self.assertEqual(caught.exception.code, 'mime')
