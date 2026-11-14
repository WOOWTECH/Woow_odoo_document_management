# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Splitting and merging PDF documents: page ranges, limits, rights, outputs, trash and log."""
import base64
import io
import json
import random
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user
from odoo.tools import pdf as odoo_pdf

from ..models.upload_policy import WoowUploadRejected
from ..services import locks, pdf_tools
from ..services.pdf_tools import PdfToolError
from ..wizards import pdftools_merge_wizard

_USER = 'woow_document_management.group_woow_document_user'
_SPLIT = 'woow.document.pdf.split.wizard'
_MERGE = 'woow.document.pdf.merge.wizard'
_LINE = 'woow.document.pdf.merge.line'
_LETTERS = bytes(97 + value % 26 for value in range(256))
_FULL = 2 * 1024 * 1024        # what the quota sees as stored


def raw_pdf(widths, filler=0):
    """A small valid PDF with one page per width (the page is as wide as the number says).

    ``filler`` bytes of comment are written into the content of each page, to make the file larger.
    """
    count = len(widths)
    kids = ' '.join('%d 0 R' % (3 + 2 * index) for index in range(count))
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               ('<< /Type /Pages /Kids [%s] /Count %d >>' % (kids, count)).encode()]
    for index, width in enumerate(widths):
        # a different filler for each page: equal streams are written only once
        content = b'q Q\n%' + random.Random(index).randbytes(filler).translate(_LETTERS) + b'\n'
        objects.append(('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d 100] /Contents %d 0 R >>'
                        % (width, 4 + 2 * index)).encode())
        objects.append(b'<< /Length %d >>\nstream\n' % len(content) + content + b'endstream')
    out = io.BytesIO()
    out.write(b'%PDF-1.4\n')
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b'%d 0 obj\n' % number + body + b'\nendobj\n')
    xref = out.tell()
    out.write(b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1))
    for offset in offsets:
        out.write(b'%010d 00000 n \n' % offset)
    out.write(b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n'
              % (len(objects) + 1, xref))
    return out.getvalue()


def encrypted_pdf(widths):
    writer = odoo_pdf.PdfFileWriter()
    for width in widths:
        writer.add_blank_page(width=width, height=100)
    writer.encrypt('secret')
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def widths_of(document):
    """The page widths of the stored file of ``document``, in page order."""
    reader = odoo_pdf.PdfReader(io.BytesIO(base64.b64decode(document.datas)), strict=False)
    return [int(float(page.mediabox.width)) for page in reader.pages]


class PdfToolsCase(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        self.admin = env.ref('base.user_admin')
        self.member = new_test_user(env, login='pdf_member', groups=_USER)
        self.reader = new_test_user(env, login='pdf_reader', groups=_USER)
        self.uploader = new_test_user(env, login='pdf_uploader', groups=_USER)
        self.outsider = new_test_user(env, login='pdf_outsider', groups=_USER)
        self.plain = new_test_user(env, login='pdf_plain', groups='base.group_user')
        self.portal = new_test_user(env, login='pdf_portal', groups='base.group_portal')
        folders = env['woow.document.folder'].sudo()
        self.folder = folders.create({'name': 'Pdf source', 'owner_id': self.admin.id})
        self.target = folders.create({'name': 'Pdf target', 'owner_id': self.admin.id})
        self.read_only = folders.create({'name': 'Pdf read only', 'owner_id': self.admin.id})
        self.tag = env['woow.document.tag'].sudo().create({'name': 'pdf-tag'})
        self._grant(self.folder, self.member, 'edit')
        self._grant(self.target, self.member, 'edit')
        self._grant(self.read_only, self.member, 'edit')
        self._grant(self.folder, self.reader, 'read')
        self._grant(self.target, self.reader, 'read')
        self._grant(self.read_only, self.reader, 'read')
        self._grant(self.folder, self.uploader, 'upload')
        self._grant(self.target, self.uploader, 'upload')
        self.documents = env['woow.document'].sudo()

    def _grant(self, folder, user, level):
        self.env['woow.document.folder.access'].sudo().create({
            'folder_id': folder.id, 'subject_type': 'user', 'user_id': user.id, 'level': level})

    def _doc(self, name, data, folder=None, **vals):
        return self.env['woow.document'].sudo().create(dict(
            vals, name=name, folder_id=(folder or self.folder).id,
            datas=base64.b64encode(data)))

    def _pdf(self, name='source.pdf', widths=(101, 102, 103, 104, 105), folder=None, filler=0,
             **vals):
        return self._doc(name, raw_pdf(list(widths), filler), folder, **vals)

    def _split(self, user, doc, **vals):
        return self.env[_SPLIT].with_user(user).create(dict({'document_id': doc.id, 'ranges': '1-2'}, **vals))

    def _merge(self, user, docs, folder=None, name='merged.pdf', **vals):
        lines = [(0, 0, {'document_id': doc.id, 'sequence': 10 * (index + 1)})
                 for index, doc in enumerate(docs)]
        return self.env[_MERGE].with_user(user).create(dict(
            {'line_ids': lines, 'folder_id': (folder or self.target).id, 'name': name}, **vals))

    def _made(self, action):
        """The documents that the notice of a finished wizard opens, oldest first."""
        domain = action['params']['next']['domain']
        return self.env['woow.document'].sudo().with_context(active_test=False).search(
            domain, order='id')

    def _count(self, folder):
        return self.env['woow.document'].sudo().with_context(active_test=False).search_count([
            ('folder_id', '=', folder.id)])

    def _events(self, event):
        return self.env['woow.document.log'].sudo().search([('event', '=', event)], order='id')


@tagged('woow_docmgmt_pdftools')
class TestPageRanges(TransactionCase):

    def test_valid_lists(self):
        self.assertEqual(pdf_tools.parse_ranges('1-3, 4, 5-8', 8), [(1, 3), (4, 4), (5, 8)])
        self.assertEqual(pdf_tools.parse_ranges(' 7 ,2-3 ', 8), [(7, 7), (2, 3)])
        self.assertEqual(pdf_tools.parse_ranges('1;3-4', 8), [(1, 1), (3, 4)])
        self.assertEqual(pdf_tools.parse_ranges('１－２，４', 8), [(1, 2), (4, 4)])
        self.assertEqual(pdf_tools.parse_ranges('1 - 8', 8), [(1, 8)])

    def test_parts_that_share_a_page_are_refused(self):
        for text in ('1-3, 3-5', '1-5, 2', '4, 4', '5-6, 1-5'):
            with self.subTest(text=text), self.assertRaises(PdfToolError) as caught:
                pdf_tools.parse_ranges(text, 8)
            self.assertEqual(caught.exception.code, 'overlap')

    def test_pages_outside_the_file_and_backward_ranges_are_refused(self):
        for text in ('9', '1-9', '0', '0-2', '5-3', '999999'):
            with self.subTest(text=text), self.assertRaises(PdfToolError) as caught:
                pdf_tools.parse_ranges(text, 8)
            self.assertEqual(caught.exception.code, 'range')

    def test_garbage_is_refused(self):
        for text in ('', '   ', 'a', '1-', '-3', '1--3', '1,,3', '1,', ',1', '1 2', '1.5', '1-2-3',
                     '1e2', '1-3; x', 'all', '١-٢', '1' * 7, '1,' * 600, 'x' * 5000, None, 5, b'1'):
            with self.subTest(text=text), self.assertRaises(PdfToolError) as caught:
                pdf_tools.parse_ranges(text, 8)
            self.assertIn(caught.exception.code, ('syntax', 'outputs'))

    def test_too_many_parts_are_refused(self):
        with patch.object(pdf_tools, 'PDF_MAX_OUTPUTS', 3):
            self.assertEqual(len(pdf_tools.parse_ranges('1,2,3', 8)), 3)
            with self.assertRaises(PdfToolError) as caught:
                pdf_tools.parse_ranges('1,2,3,4', 8)
            self.assertEqual(caught.exception.code, 'outputs')
            with self.assertRaises(PdfToolError):
                pdf_tools.single_pages(4)
            self.assertEqual(pdf_tools.single_pages(3), [(1, 1), (2, 2), (3, 3)])

    def test_the_limits_are_the_documented_ones(self):
        self.assertEqual((pdf_tools.PDF_MAX_PAGES, pdf_tools.PDF_MAX_OUTPUTS), (500, 100))

    def test_reading_refuses_what_it_cannot_use(self):
        reader, count = pdf_tools.open_pdf(raw_pdf([101, 102]))
        self.assertEqual(count, 2)
        for label, data, code in (
                ('encrypted', encrypted_pdf([101]), 'encrypted'),
                ('text', b'hello', 'damaged'),
                ('empty', b'', 'damaged'),
                ('cut off', raw_pdf([101, 102])[:90], 'damaged'),
                ('no pages', raw_pdf([]), 'damaged'),
                ('not bytes', 'text', 'damaged')):
            with self.subTest(label), self.assertRaises(PdfToolError) as caught:
                pdf_tools.open_pdf(data)
            self.assertEqual(caught.exception.code, code)
        with patch.object(pdf_tools, 'PDF_MAX_PAGES', 2):
            pdf_tools.open_pdf(raw_pdf([101, 102]))
            with self.assertRaises(PdfToolError) as caught:
                pdf_tools.open_pdf(raw_pdf([101, 102, 103]))
            self.assertEqual(caught.exception.code, 'pages')

    def test_pages_are_written_in_the_order_given(self):
        reader, _count = pdf_tools.open_pdf(raw_pdf([101, 102, 103]))
        data = pdf_tools.write_pdf(pdf_tools.page_range(reader, 3, 3) + pdf_tools.page_range(reader, 1, 2))
        again, count = pdf_tools.open_pdf(data)
        self.assertEqual([int(float(page.mediabox.width)) for page in again.pages], [103, 101, 102])
        self.assertEqual(count, 3)


@tagged('woow_docmgmt_pdftools')
class TestSplit(PdfToolsCase):

    def test_each_range_becomes_a_document_in_the_same_folder(self):
        source = self._pdf(widths=(101, 102, 103, 104, 105, 106, 107, 108),
                           tag_ids=[(6, 0, [self.tag.id])])
        wizard = self._split(self.member, source, ranges='1-3, 4, 5-8')
        made = self._made(wizard.action_split())
        self.assertEqual(len(made), 3)
        self.assertEqual(made.mapped('name'), [
            'source (pages 1-3).pdf', 'source (page 4).pdf', 'source (pages 5-8).pdf'])
        self.assertEqual([widths_of(doc) for doc in made],
                         [[101, 102, 103], [104], [105, 106, 107, 108]])
        for doc in made:
            self.assertEqual(doc.folder_id, self.folder)
            self.assertEqual(doc.tag_ids, self.tag)
            self.assertEqual(doc.mimetype, 'application/pdf')
            self.assertTrue(doc.checksum)
            self.assertEqual(doc.owner_id, self.member)
            self.assertTrue(doc.active)
        self.assertEqual(widths_of(source), list(range(101, 109)))
        self.assertTrue(source.active)
        self.assertEqual(source.name, 'source.pdf')

    def test_one_document_per_page(self):
        source = self._pdf(widths=(101, 102, 103))
        wizard = self._split(self.member, source, mode='pages', ranges=False)
        made = self._made(wizard.action_split())
        self.assertEqual([widths_of(doc) for doc in made], [[101], [102], [103]])
        self.assertEqual(made.mapped('name'), [
            'source (page 1).pdf', 'source (page 2).pdf', 'source (page 3).pdf'])

    def test_more_documents_than_one_upload_call_takes_are_all_made(self):
        limit = self.env['woow.document.settings']._get_settings().max_files_per_upload
        source = self._pdf(widths=range(101, 101 + 2 * limit + 1))
        made = self._made(self._split(self.member, source, mode='pages').action_split())
        self.assertEqual(len(made), 2 * limit + 1)

    def test_the_quota_is_measured_across_all_the_calls_of_one_split(self):
        limit = self.env['woow.document.settings']._get_settings().max_files_per_upload
        source = self._pdf(widths=range(101, 101 + 2 * limit + 1))
        self.env['ir.config_parameter'].sudo().set_param('woow_document_management.quota_mb', '1000')
        locks._reset_tracker(self.env.cr)
        made = self._made(self._split(self.member, source, mode='pages').action_split())
        self.assertEqual(len(made), 2 * limit + 1)

    def test_a_long_name_is_cut_not_refused(self):
        source = self._pdf(name='n' * 195 + '.pdf', widths=(101, 102))
        made = self._made(self._split(self.member, source, ranges='1-2').action_split())
        self.assertLessEqual(len(made.name), 200)
        self.assertTrue(made.name.endswith('(pages 1-2).pdf'))

    def test_the_original_goes_to_the_trash_only_when_asked(self):
        source = self._pdf()
        self._split(self.member, source).action_split()
        self.assertTrue(source.active)
        kept = self._pdf('other.pdf')
        action = self._split(self.member, kept, trash_original=True).action_split()
        self.assertFalse(kept.active)
        self.assertEqual(kept.trashed_by_id, self.member)
        self.assertIn('trash', action['params']['message'])
        self.assertTrue(self._made(action).filtered('active'))

    def test_a_caller_who_cannot_write_the_original_keeps_it(self):
        source = self._pdf(owner_id=self.member.id, shared_user_ids=[(4, self.uploader.id)])
        self.assertFalse(source.with_user(self.uploader)._filtered_access('write'))
        wizard = self._split(self.uploader, source, trash_original=True)
        self.assertFalse(wizard.can_trash)
        action = wizard.action_split()
        self.assertTrue(source.active)
        self.assertEqual(len(self._made(action)), 1)
        self.assertIn('kept', action['params']['message'])
        self.assertTrue(self._split(self.member, source).can_trash)

    def test_a_reader_cannot_split_and_nothing_is_made(self):
        source = self._pdf()
        before = self._count(self.folder)
        with self.assertRaises(AccessError):
            self._split(self.reader, source).action_split()
        self.assertEqual(self._count(self.folder), before)
        self.assertEqual(len(self._events('pdf_split')), 0)

    def test_someone_who_cannot_read_the_source_learns_nothing(self):
        source = self._pdf()
        messages = set()
        for doc_id in (source.id, source.id + 100000):
            with self.assertRaises(AccessError) as caught:
                self.env[_SPLIT].with_user(self.outsider).create({'document_id': doc_id})
            messages.add(str(caught.exception))
        self.assertEqual(len(messages), 1)

    def test_rights_withdrawn_after_the_dialog_was_opened_are_honoured(self):
        source = self._pdf()
        wizard = self._split(self.member, source)
        self.env['woow.document.folder.access'].sudo().search([
            ('folder_id', '=', self.folder.id), ('user_id', '=', self.member.id)]).unlink()
        with self.assertRaises(AccessError):
            wizard.action_split()
        self.assertEqual(self._count(self.folder), 1)
        self.assertEqual(len(self._events('pdf_split')), 0)
    def test_a_document_that_cannot_be_split_is_refused(self):
        text = self._doc('note.txt', b'plain text')
        link = self.env['woow.document'].sudo().create({
            'name': 'a link', 'folder_id': self.folder.id, 'document_type': 'url',
            'url': 'https://example.test/x'})
        for doc in (text, link):
            with self.subTest(doc=doc.name), self.assertRaises(UserError):
                self._split(self.member, doc).action_split()
        gone = self._pdf('gone.pdf')
        gone.sudo().action_trash()
        with self.assertRaises(AccessError):
            self.env[_SPLIT].with_user(self.member).create({'document_id': gone.id})
        wizard = self._split(self.member, self._pdf('later.pdf'))
        wizard.document_id.sudo().action_trash()
        with self.assertRaises(AccessError):
            wizard.action_split()
        self.assertEqual(len(self._events('pdf_split')), 0)

    def test_encrypted_damaged_and_oversized_files_give_a_message(self):
        locked = self._doc('locked.pdf', encrypted_pdf([101, 102]))
        broken = self._doc('broken.pdf', b'%PDF-1.4\n1 0 obj\n<<>>\nendobj\n')
        for doc in (locked, broken):
            with self.subTest(doc=doc.name), self.assertRaises(UserError) as caught:
                self._split(self.member, doc).action_split()
            self.assertNotIn('Traceback', str(caught.exception))
            self.assertNotIn(doc.name, str(caught.exception))
        big = self._pdf('big.pdf', widths=(101, 102, 103))
        with patch.object(pdf_tools, 'PDF_MAX_PAGES', 2), self.assertRaises(UserError):
            self._split(self.member, big, ranges='1').action_split()

    def test_ranges_are_checked_against_the_file(self):
        source = self._pdf(widths=(101, 102, 103))
        before = self._count(self.folder)
        for text in ('4', '1-4', '1-2, 2-3', 'abc', '', False):
            with self.subTest(text=text), self.assertRaises(UserError):
                self._split(self.member, source, ranges=text).action_split()
        self.assertEqual(self._count(self.folder), before)

    def test_the_most_outputs_of_one_split_is_limited(self):
        source = self._pdf(widths=range(101, 106))
        with patch.object(pdf_tools, 'PDF_MAX_OUTPUTS', 4):
            with self.assertRaises(UserError):
                self._split(self.member, source, mode='pages').action_split()
            with self.assertRaises(UserError):
                self._split(self.member, source, ranges='1,2,3,4,5').action_split()
            made = self._made(self._split(self.member, source, ranges='1,2,3,4').action_split())
        self.assertEqual(len(made), 4)

    def test_outputs_follow_the_quota(self):
        source = self._pdf()
        self.env['ir.config_parameter'].sudo().set_param('woow_document_management.quota_mb', '1')
        locks._reset_tracker(self.env.cr)     # the grants of setUp took access locks in this test
        before = self._count(self.folder)
        with patch.object(type(self.env['woow.document.quota']), '_usage_bytes',
                          return_value=_FULL), self.assertRaises(WoowUploadRejected) as caught:
            self._split(self.member, source, trash_original=True).action_split()
        self.assertEqual(caught.exception.code, 'quota')
        self.assertEqual(self._count(self.folder), before)
        self.assertTrue(source.active)

    def test_outputs_follow_the_upload_policy(self):
        source = self._pdf(widths=(101, 102, 103), filler=600_000)
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.internal_max_upload_mb', '1')
        before = self._count(self.folder)
        with self.assertRaises(WoowUploadRejected) as caught:
            self._split(self.member, source, ranges='1-3').action_split()
        self.assertEqual(caught.exception.code, 'size')
        self.assertEqual(self._count(self.folder), before)
        made = self._made(self._split(self.member, source, ranges='2').action_split())
        self.assertEqual(widths_of(made), [102])

    def test_a_failure_halfway_leaves_nothing_behind(self):
        source = self._pdf(widths=(101, 102, 103))
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.max_files_per_upload', '1')
        policy = type(self.env['woow.document.upload.policy'])
        original = policy._validate
        calls = []

        def validate(model, name, data, **kwargs):
            calls.append(name)
            if len(calls) == 2:
                raise WoowUploadRejected('content')
            return original(model, name, data, **kwargs)

        wizard = self._split(self.member, source, mode='pages', trash_original=True)
        with patch.object(policy, '_validate', validate), self.assertRaises(WoowUploadRejected):
            wizard.action_split()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self._count(self.folder), 1)
        self.assertTrue(source.active)
        self.assertEqual(len(self._events('pdf_split')), 0)
    def test_the_log_has_counts_and_ids_only(self):
        source = self._pdf(name='secret-name.pdf')
        made = self._made(self._split(self.member, source, ranges='1-2, 4', trash_original=True)
                          .action_split())
        events = self._events('pdf_split')
        self.assertEqual(len(events), 1)
        event = events
        self.assertEqual(event.user_id, self.member)
        self.assertEqual(event.document_id, source)
        self.assertEqual(event.folder_id, self.folder)
        self.assertEqual(event.channel, 'backend')
        self.assertEqual(event.detail, {'outputs': 2, 'pages': 5, 'trashed': 1,
                                        'output_ids': made.ids})
        self.assertNotIn('secret', json.dumps(event.detail))
        uploads = self._events('upload').filtered(lambda row: row.document_id in made)
        self.assertEqual(len(uploads), 2)
        self.assertEqual(len(self._events('trash').filtered(lambda row: row.document_id == source)), 1)

    def test_default_get_takes_one_selected_document_the_caller_can_read(self):
        source = self._pdf()
        other = self._pdf('other.pdf')
        model = self.env[_SPLIT].with_user(self.member)
        for ids, expected in (([source.id], source), ([source.id, other.id], model.env['woow.document']),
                              ([], model.env['woow.document']), ('x', model.env['woow.document']),
                              ([0], model.env['woow.document']), ([True], model.env['woow.document'])):
            with self.subTest(ids=ids):
                values = model.with_context(active_model='woow.document', active_ids=ids).default_get(
                    ['document_id'])
                self.assertEqual(values.get('document_id', False), expected.id or False)
        hidden = self.env[_SPLIT].with_user(self.outsider).with_context(
            active_model='woow.document', active_ids=[source.id]).default_get(['document_id'])
        self.assertFalse(hidden.get('document_id'))
        wrong_model = model.with_context(active_model='res.partner', active_ids=[source.id])
        self.assertFalse(wrong_model.default_get(['document_id']).get('document_id'))


@tagged('woow_docmgmt_pdftools')
class TestMerge(PdfToolsCase):

    def setUp(self):
        super().setUp()
        self.first = self._pdf('first.pdf', widths=(101, 102))
        self.second = self._pdf('second.pdf', widths=(201,))
        self.third = self._pdf('third.pdf', widths=(301, 302, 303))

    def test_the_documents_are_joined_in_the_order_of_the_lines(self):
        wizard = self._merge(self.member, [self.first, self.second, self.third], name='Joined.pdf')
        made = self._made(wizard.action_merge())
        self.assertEqual(len(made), 1)
        self.assertEqual(made.name, 'Joined.pdf')
        self.assertEqual(made.folder_id, self.target)
        self.assertEqual(made.owner_id, self.member)
        self.assertEqual(made.mimetype, 'application/pdf')
        self.assertEqual(widths_of(made), [101, 102, 201, 301, 302, 303])
        for doc in (self.first, self.second, self.third):
            self.assertTrue(doc.active)

    def test_the_order_follows_the_sequence_not_the_creation(self):
        lines = [(0, 0, {'document_id': self.first.id, 'sequence': 30}),
                 (0, 0, {'document_id': self.second.id, 'sequence': 10}),
                 (0, 0, {'document_id': self.third.id, 'sequence': 20})]
        wizard = self.env[_MERGE].with_user(self.member).create({
            'line_ids': lines, 'folder_id': self.target.id, 'name': 'ordered.pdf'})
        self.assertEqual(widths_of(self._made(wizard.action_merge())),
                         [201, 301, 302, 303, 101, 102])
        reordered = self._merge(self.member, [self.first, self.second])
        reordered.line_ids.filtered(lambda line: line.document_id == self.first).sequence = 99
        self.assertEqual(widths_of(self._made(reordered.action_merge())), [201, 101, 102])

    def test_the_same_document_may_appear_twice(self):
        made = self._made(self._merge(self.member, [self.second, self.first, self.second]).action_merge())
        self.assertEqual(widths_of(made), [201, 101, 102, 201])

    def test_the_name_gets_its_extension(self):
        made = self._made(self._merge(self.member, [self.first, self.second], name=' Report 2026 ')
                          .action_merge())
        self.assertEqual(made.name, 'Report 2026.pdf')
        made = self._made(self._merge(self.member, [self.first, self.second], name='UPPER.PDF')
                          .action_merge())
        self.assertEqual(made.name, 'UPPER.PDF')
        long_name = self._made(self._merge(self.member, [self.first, self.second], name='n' * 200)
                               .action_merge())
        self.assertLessEqual(len(long_name.name), 200)
        self.assertTrue(long_name.name.endswith('.pdf'))
        for bad in ('', '   '):
            with self.subTest(name=bad), self.assertRaises(UserError):
                self._merge(self.member, [self.first, self.second], name=bad).action_merge()
    def test_between_two_and_the_most_files_are_needed(self):
        with self.assertRaises(UserError):
            self._merge(self.member, [self.first]).action_merge()
        with self.assertRaises(UserError):
            self.env[_MERGE].with_user(self.member).create({
                'folder_id': self.target.id, 'name': 'none.pdf'}).action_merge()
        with patch.object(pdftools_merge_wizard, 'PDF_MAX_INPUTS', 2):
            self._merge(self.member, [self.first, self.second]).action_merge()
            with self.assertRaises(UserError):
                self._merge(self.member, [self.first, self.second, self.third]).action_merge()

    def test_the_destination_needs_the_right_to_create(self):
        before = self._count(self.target)
        with self.assertRaises(AccessError):
            self._merge(self.reader, [self.first, self.second], folder=self.target).action_merge()
        self.assertEqual(self._count(self.target), before)
        self.assertEqual(len(self._events('pdf_merge')), 0)
        made = self._made(self._merge(self.member, [self.first, self.second],
                                      folder=self.read_only).action_merge())
        self.assertEqual(made.folder_id, self.read_only)
        trashed = self.env['woow.document.folder'].sudo().create({
            'name': 'Pdf trashed', 'owner_id': self.admin.id})
        self._grant(trashed, self.member, 'edit')
        trashed.action_trash()
        with self.assertRaises(AccessError):
            self._merge(self.member, [self.first, self.second], folder=trashed)
        hidden = self.env['woow.document.folder'].sudo().create({
            'name': 'Pdf hidden', 'owner_id': self.admin.id})
        with self.assertRaises(AccessError) as caught_hidden:
            self._merge(self.member, [self.first, self.second], folder=hidden)
        with self.assertRaises(AccessError) as caught_missing:
            self.env[_MERGE].with_user(self.member).create({
                'folder_id': hidden.id + 100000, 'name': 'x.pdf'})
        self.assertEqual(str(caught_hidden.exception), str(caught_missing.exception))
        self.assertEqual(len(self._events('pdf_merge')), 1)
    def test_an_upload_member_may_merge_into_the_folder_but_a_reader_may_not(self):
        sources = [self._pdf('mine-1.pdf', owner_id=self.uploader.id, widths=(101,)),
                   self._pdf('mine-2.pdf', owner_id=self.uploader.id, widths=(102,))]
        made = self._made(self._merge(self.uploader, sources, folder=self.folder).action_merge())
        self.assertEqual(widths_of(made), [101, 102])
        with self.assertRaises(AccessError):
            self._merge(self.reader, [self.first, self.second], folder=self.target).action_merge()

    def test_sources_the_caller_cannot_read_are_refused_alike(self):
        self._grant(self.target, self.outsider, 'upload')
        messages = set()
        for ids in ([self.first.id, self.second.id], [self.first.id, self.second.id + 100000]):
            with self.assertRaises(AccessError) as caught:
                self.env[_MERGE].with_user(self.outsider).create({
                    'line_ids': [(0, 0, {'document_id': doc_id}) for doc_id in ids],
                    'folder_id': self.target.id, 'name': 'x.pdf'})
            messages.add(str(caught.exception))
        self.assertEqual(len(messages), 1)
        private_folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Pdf private', 'owner_id': self.admin.id})
        private = self._pdf('private.pdf', folder=private_folder)
        with self.assertRaises(AccessError) as caught:
            self._merge(self.member, [self.first, private])
        self.assertEqual({str(caught.exception)}, messages)
        wizard = self._merge(self.member, [self.first, self.second])
        self.env['woow.document.folder.access'].sudo().search([
            ('folder_id', '=', self.folder.id), ('user_id', '=', self.member.id)]).unlink()
        with self.assertRaises(AccessError):
            wizard.action_merge()
        self.assertEqual(self._count(self.target), 0)
    def test_lines_and_rows_belong_to_their_creator(self):
        wizard = self._merge(self.member, [self.first, self.second])
        split = self._split(self.member, self.first)
        self.env.flush_all()
        change = {_LINE: {'sequence': 7}, _SPLIT: {'trash_original': True},
                  _MERGE: {'trash_originals': True}}
        for user in (self.reader, self.admin):
            for record in (wizard, split, wizard.line_ids[0]):
                model = self.env[record._name].with_user(user)
                with self.subTest(user=user.login, model=record._name):
                    self.assertFalse(model.search([('id', '=', record.id)]))
                    with self.assertRaises(AccessError):
                        model.browse(record.id).read(['id'])
                    with self.assertRaises(AccessError):
                        model.browse(record.id).write(change[record._name])
        with self.assertRaises(AccessError):
            wizard.with_user(self.reader).action_merge()
        with self.assertRaises(AccessError):
            split.with_user(self.reader).action_split()
        self.assertEqual(self._count(self.target), 0)
    def test_files_that_cannot_be_joined_give_a_message(self):
        text = self._doc('note.txt', b'plain text')
        locked = self._doc('locked.pdf', encrypted_pdf([101]))
        broken = self._doc('broken.pdf', b'%PDF-1.4\n1 0 obj\n<<>>\nendobj\n')
        for doc in (text, locked, broken):
            with self.subTest(doc=doc.name), self.assertRaises(UserError) as caught:
                self._merge(self.member, [self.first, doc]).action_merge()
            self.assertNotIn(doc.name, str(caught.exception))
        with patch.object(pdf_tools, 'PDF_MAX_PAGES', 2), self.assertRaises(UserError):
            self._merge(self.member, [self.first, self.third]).action_merge()
        link = self.env['woow.document'].sudo().create({
            'name': 'a link', 'folder_id': self.folder.id, 'document_type': 'url',
            'url': 'https://example.test/x'})
        with self.assertRaises(UserError):
            self._merge(self.member, [self.first, link]).action_merge()
        self.assertEqual(self._count(self.target), 0)

    def test_a_trashed_source_is_unavailable(self):
        wizard = self._merge(self.member, [self.first, self.second])
        self.second.sudo().action_trash()
        with self.assertRaises(AccessError):
            wizard.action_merge()
        with self.assertRaises(AccessError):
            self._merge(self.member, [self.first, self.second])

    def test_originals_go_to_the_trash_only_where_the_caller_may_write(self):
        mine = self._pdf('mine.pdf', widths=(101,), owner_id=self.uploader.id)
        theirs = self._pdf('theirs.pdf', widths=(102,), owner_id=self.member.id,
                           shared_user_ids=[(4, self.uploader.id)])
        kept = self._made(self._merge(self.uploader, [mine, theirs], folder=self.target)
                          .action_merge())
        self.assertTrue(mine.active and theirs.active and kept)
        action = self._merge(self.uploader, [mine, theirs], folder=self.target,
                             trash_originals=True).action_merge()
        self.assertFalse(mine.active)
        self.assertTrue(theirs.active)
        self.assertIn('1 of the originals', action['params']['message'])
        action = self._merge(self.member, [self.first, self.second], trash_originals=True).action_merge()
        self.assertFalse(self.first.active or self.second.active)
        self.assertTrue(self.third.active)
        self.assertEqual(self._events('pdf_merge')[-1].detail['trashed'], 2)

    def test_the_parts_cannot_add_up_to_more_than_a_file_may_be(self):
        big = [self._pdf('big-%d.pdf' % index, widths=(101,), filler=700_000) for index in range(2)]
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_document_management.internal_max_upload_mb', '1')
        with self.assertRaises(UserError):
            self._merge(self.member, big).action_merge()
        self.assertEqual(self._count(self.target), 0)
        self.assertEqual(len(self._events('pdf_merge')), 0)

    def test_the_output_follows_the_upload_policy_and_the_quota(self):
        sources = [self._pdf('s-%d.pdf' % index, widths=(101,), filler=400_000) for index in range(2)]
        with patch.object(type(self.env['woow.document.upload.policy']), '_validate',
                          side_effect=WoowUploadRejected('content')), \
                self.assertRaises(WoowUploadRejected):
            self._merge(self.member, sources).action_merge()
        self.assertEqual(self._count(self.target), 0)
        self.env['ir.config_parameter'].sudo().set_param('woow_document_management.quota_mb', '1')
        locks._reset_tracker(self.env.cr)     # the grants of setUp took access locks in this test
        with patch.object(type(self.env['woow.document.quota']), '_usage_bytes',
                          return_value=_FULL), self.assertRaises(WoowUploadRejected) as caught:
            self._merge(self.member, sources, trash_originals=True).action_merge()
        self.assertEqual(caught.exception.code, 'quota')
        self.assertEqual(self._count(self.target), 0)
        self.assertTrue(all(doc.active for doc in sources))

    def test_the_log_has_counts_and_ids_only(self):
        made = self._made(self._merge(self.member, [self.first, self.second, self.third],
                                      name='top-secret-name.pdf').action_merge())
        events = self._events('pdf_merge')
        self.assertEqual(len(events), 1)
        self.assertEqual(events.user_id, self.member)
        self.assertEqual(events.document_id, made)
        self.assertEqual(events.folder_id, self.target)
        self.assertEqual(events.detail, {
            'inputs': 3, 'pages': 6, 'trashed': 0,
            'source_ids': [self.first.id, self.second.id, self.third.id]})
        self.assertNotIn('secret', json.dumps(events.detail))
        self.assertEqual(len(self._events('upload').filtered(lambda row: row.document_id == made)), 1)

    def test_default_get_lists_the_readable_pdf_files_in_order(self):
        text = self._doc('note.txt', b'plain text')
        model = self.env[_MERGE].with_user(self.member)
        values = model.with_context(
            active_model='woow.document',
            active_ids=[self.third.id, text.id, self.first.id, self.third.id]).default_get(
                ['line_ids', 'folder_id'])
        self.assertEqual([command[2]['document_id'] for command in values['line_ids']],
                         [self.third.id, self.first.id])
        self.assertEqual([command[2]['sequence'] for command in values['line_ids']], [10, 20])
        self.assertEqual(values['folder_id'], self.folder.id)
        for ids in ('x', [], [0], [-1], [True], list(range(1, 200))):
            with self.subTest(ids=ids):
                self.assertFalse(model.with_context(active_model='woow.document', active_ids=ids)
                                 .default_get(['line_ids']).get('line_ids'))
        outsider = self.env[_MERGE].with_user(self.outsider).with_context(
            active_model='woow.document', active_ids=[self.first.id]).default_get(
                ['line_ids', 'folder_id'])
        self.assertFalse(outsider.get('line_ids'))
        self.assertFalse(outsider.get('folder_id'))
        self._grant(self.target, self.outsider, 'upload')
        shared = self._pdf('shared.pdf', shared_user_ids=[(4, self.outsider.id)])
        values = self.env[_MERGE].with_user(self.outsider).with_context(
            active_model='woow.document', active_ids=[shared.id]).default_get(
                ['line_ids', 'folder_id'])
        self.assertEqual(len(values['line_ids']), 1)
        self.assertFalse(values.get('folder_id'))


@tagged('woow_docmgmt_pdftools')
class TestEntryPoints(PdfToolsCase):

    def test_only_document_users_can_open_or_run_the_wizards(self):
        doc = self._pdf()
        for user in (self.plain, self.portal):
            for model in (_SPLIT, _MERGE, _LINE):
                with self.subTest(user=user.login, model=model), self.assertRaises(AccessError):
                    self.env[model].with_user(user).create({})
        with self.assertRaises(AccessError):
            doc.with_user(self.plain).action_open_pdf_split_wizard()
        with self.assertRaises(AccessError):
            doc.with_user(self.portal).action_open_pdf_merge_wizard()
        with self.assertRaises(AccessError):
            doc.with_user(self.outsider).action_open_pdf_split_wizard()

    def test_the_buttons_open_the_wizards_with_the_selection(self):
        first, second = self._pdf('a.pdf'), self._pdf('b.pdf')
        action = first.with_user(self.member).action_open_pdf_split_wizard()
        self.assertEqual(action['res_model'], _SPLIT)
        self.assertEqual(action['target'], 'new')
        self.assertEqual(action['context']['active_ids'], [first.id])
        action = (first | second).with_user(self.member).action_open_pdf_merge_wizard()
        self.assertEqual(action['res_model'], _MERGE)
        self.assertEqual(action['context']['active_ids'], [first.id, second.id])
        self.assertEqual(action['context']['active_model'], 'woow.document')
        with self.assertRaises(ValueError):
            (first | second).with_user(self.member).action_open_pdf_split_wizard()
        values = self.env[_MERGE].with_user(self.member).with_context(**action['context']).default_get(
            ['line_ids'])
        self.assertEqual(len(values['line_ids']), 2)

    def test_both_wizards_are_in_the_action_menu_of_the_list_and_the_form(self):
        for name, model in (('split', _SPLIT), ('merge', _MERGE)):
            action = self.env.ref('woow_document_management.action_woow_document_pdf_%s_wizard' % name)
            self.assertEqual(action.res_model, model)
            self.assertEqual(action.binding_model_id.model, 'woow.document')
            self.assertEqual(action.binding_view_types, 'list,form')
            self.assertEqual(action.target, 'new')
            self.assertEqual(action.groups_id, self.env.ref(_USER))

    def test_the_document_form_offers_both_buttons_for_pdf_files_only(self):
        view = self.env.ref('woow_document_management.view_woow_document_document_form_pdftools')
        arch = view.arch_db
        self.assertIn('action_open_pdf_split_wizard', arch)
        self.assertIn('action_open_pdf_merge_wizard', arch)
        self.assertIn("mimetype != 'application/pdf'", arch)
        combined = self.env['woow.document'].with_user(self.member).get_view(
            self.env.ref('woow_document_management.view_woow_document_document_form').id, 'form')
        self.assertIn('action_open_pdf_split_wizard', combined['arch'])
        for model in (_SPLIT, _MERGE):
            form = self.env[model].with_user(self.member).get_view(None, 'form')
            self.assertIn('<form', form['arch'])

    def test_the_wizard_models_are_not_open_to_portal_users(self):
        for model in (_SPLIT, _MERGE, _LINE):
            access = self.env['ir.model.access'].sudo().search([
                ('model_id.model', '=', model), ('group_id', '=', self.env.ref('base.group_portal').id)])
            self.assertFalse(access, model)
            self.assertFalse(self.env['ir.model.access'].sudo().search([
                ('model_id.model', '=', model), ('group_id', '=', False)]), model)
