# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""A notification row does not name a folder or a document to someone who cannot read it (I-08).

Core lets every internal user read ``mail.notification`` and builds the label of its message
as superuser, from the name stored on the message. The label of a message of a folder or a
document therefore follows the rights of the real caller, in every read that builds one.
"""
import base64
import secrets

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'
_RESTRICTED = {'woow.document.folder': 'Restricted folder', 'woow.document': 'Restricted document'}


@tagged('woow_docmgmt_core')
class TestNotificationLabelFollowsTheReader(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        tag = secrets.token_hex(4)
        cls.company_two = env['res.company'].sudo().create({'name': 'Label company %s' % tag})
        # The author posts; B is mentioned and reads; E reads without being mentioned; C has no
        # document group; D belongs to another company; F has a grant on another folder only.
        cls.author = new_test_user(env, login='label_a_' + tag, groups=_MANAGER)
        cls.mentioned = new_test_user(env, login='label_b_' + tag, groups=_USER)
        cls.plain = new_test_user(env, login='label_c_' + tag, groups='base.group_user')
        cls.foreign = new_test_user(
            env, login='label_d_' + tag, groups='base.group_user', company_id=cls.company_two.id,
            company_ids=[Command.set([cls.company_two.id])])
        cls.reader = new_test_user(env, login='label_e_' + tag, groups=_USER)
        cls.unrelated = new_test_user(env, login='label_f_' + tag, groups=_USER)
        folders = env['woow.document.folder'].sudo()
        cls.folder = folders.create({'name': 'Label folder %s' % tag, 'owner_id': cls.author.id})
        other = folders.create({'name': 'Label other %s' % tag, 'owner_id': cls.author.id})
        access = env['woow.document.folder.access'].sudo()
        for user, folder in ((cls.mentioned, cls.folder), (cls.reader, cls.folder),
                             (cls.unrelated, other)):
            access.create({'folder_id': folder.id, 'subject_type': 'user', 'user_id': user.id,
                           'level': 'read'})
        cls.document = env['woow.document'].sudo().create({
            'name': 'label-doc-%s.txt' % tag, 'folder_id': cls.folder.id,
            'datas': base64.b64encode(b'label fixture')})
        env.flush_all()
        cls.names = {'woow.document.folder': cls.folder.name, 'woow.document': cls.document.name}

    def _post(self, record, **kwargs):
        message = record.with_user(self.author).message_post(
            body='label comment', message_type='comment', subtype_xmlid='mail.mt_comment',
            **kwargs)
        self.env.flush_all()
        return message

    def _notifications(self, user):
        return self.env['mail.notification'].with_user(user)

    def _labels(self, user, ids):
        """The label of each message in ``ids`` as every kind of read of a notification gives it."""
        notifications = self._notifications(user)
        spec = {'mail_message_id': {'fields': {'display_name': {}}}}
        domain = [('mail_message_id', 'in', ids)]
        rows = notifications.search_read(domain, ['mail_message_id'])
        found = notifications.search(domain)
        return {
            'search_read': {row['mail_message_id'][0]: row['mail_message_id'][1] for row in rows},
            'read': {row['mail_message_id'][0]: row['mail_message_id'][1]
                     for row in found.read(['mail_message_id'])},
            'read_group': {group['mail_message_id'][0]: group['mail_message_id'][1]
                           for group in notifications.read_group(
                               domain, ['mail_message_id'], ['mail_message_id'])},
            'web_read_group': {group['mail_message_id'][0]: group['mail_message_id'][1]
                               for group in notifications.web_read_group(
                                   domain, ['mail_message_id'], ['mail_message_id'])['groups']},
            'web_search_read': {
                row['mail_message_id']['id']: row['mail_message_id']['display_name']
                for row in notifications.web_search_read(domain, spec)['records']},
        }

    def _check_labels(self, messages):
        """Who reads the name, and who reads the restricted label, in every kind of read."""
        by_id = {message.id: message.sudo().model for message in messages}
        for user, may_read in ((self.author, True), (self.mentioned, True), (self.reader, True),
                               (self.plain, False), (self.foreign, False),
                               (self.unrelated, False)):
            for kind, labels in self._labels(user, list(by_id)).items():
                with self.subTest(user=user.login, read=kind):
                    self.assertEqual(set(labels), set(by_id))
                    for message_id, model in by_id.items():
                        self.assertEqual(
                            labels[message_id],
                            self.names[model] if may_read else _RESTRICTED[model])

    def test_a_mention_names_the_record_only_to_those_who_may_read_it(self):
        messages = self.env['mail.message']
        for record in (self.folder, self.document):
            messages |= self._post(record, partner_ids=[self.mentioned.partner_id.id])
        self.assertEqual(
            set(self.env['mail.notification'].sudo().search(
                [('mail_message_id', 'in', messages.ids)]).res_partner_id.ids),
            {self.mentioned.partner_id.id})
        self._check_labels(messages)
        # what the module already refused stays refused
        self.assertEqual(self.env['mail.message'].with_user(self.plain).search_count(
            [('id', 'in', messages.ids)]), 0)
        with self.assertRaises(AccessError):
            messages.with_user(self.plain).read(['record_name'])

    def test_a_follower_notification_follows_the_same_rule(self):
        messages = self.env['mail.message']
        for record in (self.folder, self.document):
            record.with_user(self.author).message_subscribe(
                partner_ids=[self.mentioned.partner_id.id])
            messages |= self._post(record)
        self.assertTrue(self.env['mail.notification'].sudo().search(
            [('mail_message_id', 'in', messages.ids)]))
        self._check_labels(messages)

    def test_a_label_is_computed_for_the_caller_when_the_cache_is_shared(self):
        """One recordset read as superuser first must not hand its labels to the next reader."""
        message = self._post(self.folder, partner_ids=[self.mentioned.partner_id.id])
        shared = message.sudo()
        self.assertEqual(shared.display_name, self.folder.name)
        self.assertEqual(message.with_user(self.plain).sudo().display_name, 'Restricted folder')
        self.assertEqual(message.with_user(self.reader).sudo().display_name, self.folder.name)
