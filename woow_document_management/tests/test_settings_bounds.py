# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Typed settings and write permission tests (T-SET-01 through T-SET-08)."""

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
