# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

"""Runtime checks for Odoo core behavior on the pinned production image.

Each check protects a design assumption. Keep the CA number in its test name
so the release gate can distinguish executed checks from planned checks.
"""

import inspect
from unittest.mock import Mock, patch

from odoo import Command, http
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mimetypes
from odoo.tools.sql import create_index

from ..models.upload_policy import WoowUploadRejected


@tagged('woow_docmgmt_core_assumptions')
class TestCoreAssumptions(TransactionCase):

    def test_ca01_attachment_check_rejects_portal(self):
        portal = self.env['res.users'].sudo().create({
            'name': 'Core portal fixture', 'login': 'wdoc-core-portal',
            'groups_id': [Command.set([self.env.ref('base.group_portal').id])],
        })
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'Core check fixture', 'raw': b'data', 'public': True,
        })
        with self.assertRaises(AccessError):
            attachment.with_user(portal).check('read')
        with self.assertRaises(AccessError):
            attachment.with_user(self.env.ref('base.public_user')).check('read')

    def test_ca03_search_adds_res_field_false(self):
        partner = self.env['res.partner'].sudo().create({'name': 'Core binary fixture'})
        field_attachment = self.env['ir.attachment'].sudo().create({
            'name': 'Core field fixture', 'raw': b'field-data',
            'res_model': 'res.partner', 'res_id': partner.id,
            'res_field': 'image_128',
        })
        user_attachments = self.env['ir.attachment'].with_user(self.env.ref('base.user_admin'))
        self.assertNotIn(field_attachment.id, user_attachments.search([]).ids)
        self.assertIn(field_attachment.id, user_attachments.search([
            ('res_field', '=', 'image_128'), ('id', '=', field_attachment.id),
        ]).ids)

    def test_ca04_unknown_res_model_open_to_internal(self):
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'Unregistered model fixture', 'raw': b'orphan',
            'res_model': 'woow.unknown.core.fixture', 'res_id': 42,
        })
        employee = self.env['res.users'].sudo().create({
            'name': 'Core employee fixture', 'login': 'wdoc-core-employee',
            'groups_id': [Command.set([self.env.ref('base.group_user').id])],
        })
        found = self.env['ir.attachment'].with_user(employee).search([
            ('id', '=', attachment.id),
        ])
        self.assertEqual(found.id, attachment.id)

    def test_ca17_libmagic_present(self):
        self.assertIsNotNone(mimetypes.magic)
        pdf_bytes = b'%PDF-1.7\n1 0 obj\n'
        text_bytes = b'plain text payload'
        # These checks intentionally have no filename: the classification comes
        # from libmagic's byte signature, not an extension supplied by a caller.
        self.assertEqual(mimetypes.guess_mimetype(pdf_bytes), 'application/pdf')
        self.assertEqual(mimetypes.guess_mimetype(text_bytes), 'text/plain')

        # A caller cannot make PDF bytes acceptable as text (or plain text as a
        # PDF) merely by choosing a matching-looking filename.
        policy = self.env['woow.document.upload.policy']
        for name, data in (('notes.txt', pdf_bytes), ('report.pdf', text_bytes)):
            with self.subTest(name=name), self.assertRaises(WoowUploadRejected) as caught:
                policy._validate(name, data)
            self.assertEqual(caught.exception.code, 'mime')

    def test_ca12_stored_compute_and_related_sudo_defaults(self):
        # These are the concrete fields whose recomputation relies on the
        # framework default, including related fields crossing a hidden M2O.
        for model_name, field_names in {
            'woow.document.folder': (
                'complete_name', 'in_trash',
                'has_restricted_descendant', 'subtree_legal_hold',
                'eff_read_user_ids', 'eff_upload_user_ids',
                'eff_edit_user_ids', 'eff_manage_user_ids',
                'eff_read_group_ids', 'eff_upload_group_ids',
                'eff_edit_group_ids', 'eff_manage_group_ids',
                'eff_read_partner_ids', 'eff_upload_partner_ids',
            ),
            'woow.document': ('attachment_id',),
        }.items():
            for field_name in field_names:
                with self.subTest(model=model_name, field=field_name):
                    field = self.env[model_name]._fields[field_name]
                    self.assertTrue(field.store)
                    self.assertTrue(field.compute)
                    self.assertFalse(field.related)
                    self.assertTrue(field.compute_sudo)

        for model_name, field_names in {
            'woow.document': ('file_size', 'checksum', 'company_id', 'folder_in_trash'),
            'woow.document.folder.access': ('company_id',),
        }.items():
            for field_name in field_names:
                with self.subTest(model=model_name, field=field_name):
                    field = self.env[model_name]._fields[field_name]
                    self.assertTrue(field.store)
                    self.assertTrue(field.related)
                    self.assertTrue(field.compute_sudo)

        # A non-stored ordinary compute is the negative control: sudo is
        # conditional on storage or relatedness, not universal to computes.
        ordinary = self.env['woow.document']._fields['woow_user_can_write']
        self.assertFalse(ordinary.store)
        self.assertTrue(ordinary.compute)
        self.assertFalse(ordinary.compute_sudo)

    def test_ca13_file_write_deduplicates_checksum(self):
        payload = b'core-dedup-fixture-20261108'
        attachment_model = self.env['ir.attachment'].sudo()
        first = attachment_model.create({'name': 'dedup-a.txt', 'raw': payload})
        second = attachment_model.create({'name': 'dedup-b.txt', 'raw': payload})
        self.assertNotEqual(first.id, second.id)
        expected_checksum = attachment_model._compute_checksum(payload)
        self.assertEqual(first.checksum, expected_checksum)
        self.assertEqual(second.checksum, expected_checksum)
        store_fname = first.store_fname
        self.assertTrue(store_fname)
        self.assertEqual(second.store_fname, store_fname)
        self.assertEqual(attachment_model._file_read(store_fname), payload)
        self.assertEqual(first.raw, payload)
        self.assertEqual(second.raw, payload)

        first.unlink()
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(second.store_fname, store_fname)
        self.assertEqual(second.raw, payload)
        self.assertEqual(attachment_model._file_read(store_fname), payload)

    def test_ca18_cron_root_and_notify_progress_signature(self):
        root = self.env.ref('base.user_root')
        job = self.env.ref(
            'woow_document_management.ir_cron_woow_document_purge_trash')
        self.assertEqual(job.user_id, root)
        self.assertEqual(job.model_id.model, 'woow.document')
        signature = inspect.signature(type(self.env['ir.cron'])._notify_progress)
        self.assertIn('done', signature.parameters)
        self.assertIn('remaining', signature.parameters)

        # Direct trigger uses the configured cron user and follows the same
        # callback path as the scheduler. Keep the real notifier in the call
        # chain while recording the counts supplied by the purge job.
        cron_model = self.env['ir.cron']
        real_notify_progress = type(cron_model)._notify_progress
        progress = []

        def record_progress(cron_self, *, done, remaining):
            progress.append((cron_self.env.uid, cron_self.env.su, done, remaining))
            return real_notify_progress(cron_self, done=done, remaining=remaining)

        self.patch(type(cron_model), '_notify_progress', record_progress)
        job.method_direct_trigger()
        self.assertEqual(len(progress), 1)
        uid, is_su, done, remaining = progress[0]
        self.assertEqual(uid, root.id)
        self.assertTrue(is_su)
        self.assertGreaterEqual(done, 0)
        self.assertGreaterEqual(remaining, 0)

    def test_ca09_stream_nosniff_csp(self):
        stream = http.Stream(type='data', data=b'probe', mimetype='text/plain')
        response = http.Response(b'probe', mimetype='text/plain')
        request = Mock()
        request.httprequest.environ = {}
        with patch.object(http, 'request', request), patch.object(
                http, '_send_file', return_value=response):
            result = stream.get_response(content_security_policy="script-src 'none'")
        self.assertEqual(result.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(result.headers['Content-Security-Policy'], "script-src 'none'")

    def test_ca30_stream_default_csp_and_private_cache(self):
        stream = http.Stream(type='data', data=b'probe', mimetype='text/html', public=False)
        response = http.Response(b'probe', mimetype='text/html')
        response.cache_control.public = True
        request = Mock()
        request.httprequest.environ = {}
        with patch.object(http, 'request', request), patch.object(
                http, '_send_file', return_value=response):
            result = stream.get_response()
        self.assertEqual(result.headers['Content-Security-Policy'], "default-src 'none'")
        self.assertEqual(result.headers['X-Content-Type-Options'], 'nosniff')
        self.assertFalse(result.cache_control.public)

    def test_ca26_create_index_signature(self):
        signature = inspect.signature(create_index)
        self.assertEqual(list(signature.parameters)[:4],
                         ['cr', 'indexname', 'tablename', 'expressions'])
        self.assertEqual(signature.parameters['method'].default, 'btree')
        self.assertEqual(signature.parameters['where'].default, '')

        # Exercise the addon's real model initializer and inspect PostgreSQL's
        # catalog result, including the partial predicate used by the product.
        self.env['woow.document.folder'].init()
        self.env.cr.execute("""
            SELECT i.indisunique, pg_get_indexdef(i.indexrelid),
                   pg_get_expr(i.indpred, i.indrelid)
              FROM pg_index i
              JOIN pg_class idx ON idx.oid = i.indexrelid
             WHERE idx.relname = %s
        """, ('woow_document_folder_name_uniq',))
        index = self.env.cr.fetchone()
        self.assertIsNotNone(index, 'folder initializer did not create its index')
        is_unique, definition, predicate = index
        self.assertTrue(is_unique)
        self.assertIn('COALESCE(parent_id, 0)', definition)
        self.assertIn('COALESCE(company_id, 0)', definition)
        self.assertIn('lower((name)::text)', definition)
        self.assertEqual(predicate, 'active')

    def test_ca33_with_user_not_su_and_session_dirty_on_change(self):
        admin = self.env.ref('base.user_admin')
        self.assertFalse(self.env['res.partner'].sudo().with_user(admin).env.su)
        session = http.Session({'grant': {'id': 4}}, 'core-session-fixture')
        self.assertFalse(session.is_dirty)
        session['grant'] = {'id': 4}
        self.assertFalse(session.is_dirty)
        session['grant'] = {'id': 5}
        self.assertTrue(session.is_dirty)

    def test_ca35_check_contents_forces_text_plain(self):
        employee = self.env['res.users'].sudo().create({
            'name': 'Core MIME fixture', 'login': 'wdoc-core-mime',
            'groups_id': [Command.set([self.env.ref('base.group_user').id])],
        })
        self.assertFalse(self.env['ir.ui.view'].with_user(employee).has_access('write'))
        attachment = self.env['ir.attachment'].with_user(employee).create({
            'name': 'untrusted.html', 'raw': b'<html><body>probe</body></html>',
            'mimetype': 'text/html',
        })
        self.assertEqual(attachment.mimetype, 'text/plain')
