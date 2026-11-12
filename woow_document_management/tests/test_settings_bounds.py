# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Typed settings and write permission tests (T-SET-01 through T-SET-08)."""
import base64
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
        self.params.set_param(PARAM_PREFIX + 'share_max_days', '999')
        self.params.set_param(PARAM_PREFIX + 'share_link_policy', 'anyone')
        self.params.set_param(PARAM_PREFIX + 'share_require_password', 'maybe')
        value = self.reader._get_settings()
        self.assertEqual(value.internal_max_upload_mb, 95)
        self.assertEqual(value.share_max_days, 90)
        self.assertEqual(value.share_link_policy, 'managers_only')
        self.assertTrue(value.share_require_password)

    def test_missing_param_safe_default(self):
        self.assertEqual(self.reader._get_settings(), WoowDocSettings())

    def test_all_params_registered_for_uninstall(self):
        self.assertEqual(len(_PARAM_KEYS), 30)
        self.assertEqual(set(_PARAM_KEYS), set(WoowDocSettings.__dataclass_fields__))

    def test_set_values_bounds_and_audit(self):
        form = self.env['res.config.settings'].create({'woow_doc_internal_max_upload_mb': 120})
        with self.assertRaises(ValidationError):
            form.set_values()
        form.write({'woow_doc_internal_max_upload_mb': 94})
        form.set_values()
        self.assertEqual(self.reader._get_settings().internal_max_upload_mb, 94)

    def test_share_key_derivation(self):
        keys = self.reader._share_keys()
        self.assertEqual(len(keys.k_share), 32)
        self.assertNotEqual(keys.k_share, keys.k_hint)
        self.assertNotEqual(keys.k_share, keys.k_ip)
        self.assertEqual(keys, self.reader._share_keys())

    def test_read_params_allowlist(self):
        with self.assertRaises(ValueError):
            self.reader._read_params(('mail.catchall.domain',))
        self.assertIn('database.secret', self.reader._read_params(('database.secret',)))

    def test_cross_field_bounds(self):
        with self.assertRaises(ValueError):
            parse_settings({'share_max_days': '3', 'share_default_days': '14'}, strict=True)
        with self.assertRaises(ValueError):
            parse_settings({'quota_warn_pct': '90', 'quota_block_pct': '80'}, strict=True)
        self.assertEqual(parse_settings({'share_max_days': '3'}).share_default_days, 3)

    def test_invalid_base_url_port_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_settings({'share_link_base_url': 'https://example.invalid:abc'}, strict=True)
        self.assertEqual(parse_settings({'share_link_base_url': 'https://example.invalid:abc'}).share_link_base_url, '')

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
        save(woow_doc_share_default_days=7)
        self.assertEqual(self.params.get_param(key), '0')
        save(woow_doc_trash_retention_days=45)
        self.assertEqual(self.params.get_param(key), '45')
