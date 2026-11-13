# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Typed settings and write permission tests (T-SET-01 through T-SET-08)."""
import base64
import hashlib
import hmac
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged

from ..models.settings import PARAM_PREFIX, _PARAM_KEYS, WoowDocSettings, parse_settings


@tagged('woow_docmgmt_core')
class TestSettingsBounds(TransactionCase):

    def setUp(self):
        super().setUp()
        self.reader = self.env['woow.document.settings']
        self.params = self.env['ir.config_parameter'].sudo()

    def test_tampered_param_clamped(self):
        self.params.set_param(PARAM_PREFIX + 'internal_max_upload_mb', '999')
        self.params.set_param(PARAM_PREFIX + 'zip_max_files', '99999')
        self.params.set_param(PARAM_PREFIX + 'tag_create_policy', 'anyone')
        self.params.set_param(PARAM_PREFIX + 'log_ip_mode', 'full')
        self.params.set_param(PARAM_PREFIX + 'allow_http_links', 'maybe')
        value = self.reader._get_settings()
        self.assertEqual(value.internal_max_upload_mb, 95)
        self.assertEqual(value.zip_max_files, 2000)
        self.assertEqual(value.tag_create_policy, 'managers')
        self.assertEqual(value.log_ip_mode, 'truncate')
        self.assertFalse(value.allow_http_links)

    def test_missing_param_safe_default(self):
        self.assertEqual(self.reader._get_settings(), WoowDocSettings())

    def test_all_params_registered_for_uninstall(self):
        self.assertEqual(len(_PARAM_KEYS), 21)
        self.assertEqual(set(_PARAM_KEYS), set(WoowDocSettings.__dataclass_fields__))

    def test_set_values_bounds_and_audit(self):
        form = self.env['res.config.settings'].create({'woow_doc_internal_max_upload_mb': 120})
        with self.assertRaises(ValidationError):
            form.set_values()
        form.write({'woow_doc_internal_max_upload_mb': 94})
        form.set_values()
        self.assertEqual(self.reader._get_settings().internal_max_upload_mb, 94)

    def test_audit_ip_key_derivation(self):
        key = self.reader._audit_ip_key()
        self.assertIsInstance(key, bytes)
        self.assertEqual(len(key), 32)
        self.assertEqual(key, self.reader._audit_ip_key())
        # the label is part of the key: the hashes that were stored stay comparable
        secret = self.params.get_param('database.secret')
        self.assertEqual(key, hmac.new(secret.encode(), b'woow_document_management/audit-ip/v1',
                                       hashlib.sha256).digest())
        self.params.set_param('database.secret', secret + '-changed')
        changed = self.reader._audit_ip_key()
        self.assertEqual(len(changed), 32)
        self.assertNotEqual(changed, key)
        self.params.set_param('database.secret', '')
        with self.assertRaises(ValueError):
            self.reader._audit_ip_key()

    def test_read_params_allowlist(self):
        with self.assertRaises(ValueError):
            self.reader._read_params(('mail.catchall.domain',))
        self.assertIn('database.secret', self.reader._read_params(('database.secret',)))

    def test_cross_field_bounds(self):
        with self.assertRaises(ValueError):
            parse_settings({'quota_warn_pct': '90', 'quota_block_pct': '80'}, strict=True)
        lenient = parse_settings({'quota_warn_pct': '90', 'quota_block_pct': '80'})
        self.assertEqual((lenient.quota_warn_pct, lenient.quota_block_pct), (80, 95))

    def test_invalid_base_url_port_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_settings({'notice_base_url': 'https://example.invalid:abc'}, strict=True)
        self.assertEqual(parse_settings({'notice_base_url': 'https://example.invalid:abc'}).notice_base_url, '')

    def test_the_notice_address_is_a_plain_https_address(self):
        self.assertEqual(
            parse_settings({'notice_base_url': ' https://docs.example.test:8443/ '},
                           strict=True).notice_base_url, 'https://docs.example.test:8443')
        for value in ('http://docs.example.test', 'https://user:secret@docs.example.test',
                      'https://docs.example.test/docs', 'https://docs.example.test/?a=1',
                      'https://docs.example.test/#part', 'docs.example.test', 'https://'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_settings({'notice_base_url': value}, strict=True)
                self.assertEqual(parse_settings({'notice_base_url': value}).notice_base_url, '')

    def test_fresh_install_defaults_match_table(self):
        settings = self.reader._get_settings()
        self.assertEqual(settings.internal_max_upload_mb, 95)
        self.assertEqual(settings.portal_max_upload_mb, 25)
        self.assertFalse(settings.allow_root_create)
        public = self.env.ref('base.public_user')
        form = self.env['res.config.settings'].create({})
        with self.assertRaises(AccessError):
            form.with_user(public).set_values()

    def test_a_retention_of_zero_stays_zero_when_the_settings_are_saved(self):
        """Zero turns the automatic purge of the trash off.

        Core stores an integer 0 as no value and deletes the parameter, which then reads back as the
        default of 30 days, and the daily purge deletes what the administrator meant to keep.
        """
        admin = self.env.ref('base.user_admin')
        key = PARAM_PREFIX + 'trash_retention_days'
        folder = self.env['woow.document.folder'].sudo().create({
            'name': 'Retention zero folder', 'owner_id': admin.id})
        document = self.env['woow.document'].sudo().create({
            'name': 'kept.txt', 'folder_id': folder.id, 'datas': base64.b64encode(b'kept')})
        document.with_user(admin).action_trash()
        document.with_context(active_test=False).sudo().write({
            'trashed_date': fields.Datetime.now() - timedelta(days=31)})

        def save(**values):
            self.env['res.config.settings'].with_user(admin).create(values).set_values()

        for before in (False, '45', '0'):
            with self.subTest(before=before):
                self.params.set_param(key, before)
                save(woow_doc_trash_retention_days=0)
                self.assertEqual(self.params.get_param(key), '0')
                self.assertEqual(self.reader._get_settings().trash_retention_days, 0)
                self.assertEqual(self.env['res.config.settings'].with_user(admin).default_get(
                    ['woow_doc_trash_retention_days'])['woow_doc_trash_retention_days'], 0)
                with patch.object(type(self.env['ir.cron']), '_notify_progress',
                                  lambda _cron, **_values: None):
                    self.env['woow.document'].sudo()._cron_purge_trash()
                self.assertTrue(document.with_context(active_test=False).exists())
        # a save of other settings keeps it, and another value is stored as it is
        self.params.set_param(key, '0')
        save(woow_doc_log_retention_days=90)
        self.assertEqual(self.params.get_param(key), '0')
        save(woow_doc_trash_retention_days=45)
        self.assertEqual(self.params.get_param(key), '45')
