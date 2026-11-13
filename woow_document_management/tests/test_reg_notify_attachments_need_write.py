# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""A notification attaches files to a record of the module only for a caller who may write it.

``message_notify`` is open to every signed-in user and takes any model and id. Core then stores the
files of the notification on that record, or links the pending ones, as superuser, and checks neither
the record nor its rules, so the module asks for the right to write the record itself. Posts keep
working for a follower who can only read.
"""
import base64
import secrets

from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import JsonRpcException, new_test_user

from ..services.constants import WOOW_MODELS

_USER = 'woow_document_management.group_woow_document_user'


def _fixture(env, suffix, password=None):
    """A folder with a document, one record of every model of the module, and four callers."""
    admin = env.ref('base.user_admin')
    folder = env['woow.document.folder'].sudo().create({
        'name': 'Notify folder %s' % suffix, 'owner_id': admin.id})
    document = env['woow.document'].sudo().create({
        'name': 'notify.txt', 'folder_id': folder.id, 'datas': base64.b64encode(b'ORIGINAL')})
    users = {
        'outsider': new_test_user(env, login='wdoc-notify-outsider-%s' % suffix,
                                  groups='base.group_user', password=password),
        'reader': new_test_user(env, login='wdoc-notify-reader-%s' % suffix, groups=_USER,
                                password=password),
        'editor': new_test_user(env, login='wdoc-notify-editor-%s' % suffix, groups=_USER,
                                password=password),
        'portal': new_test_user(env, login='wdoc-notify-portal-%s' % suffix,
                                groups='base.group_portal', password=password)}
    access = env['woow.document.folder.access'].sudo()
    rows = {level: access.create({'folder_id': folder.id, 'subject_type': 'user',
                                  'user_id': users[who].id, 'level': level})
            for who, level in (('reader', 'read'), ('editor', 'edit'))}
    records = {
        'woow.document': document, 'woow.document.folder': folder,
        'woow.document.folder.access': rows['read'],
        'woow.document.tag': env['woow.document.tag'].sudo().create({'name': 'Notify %s' % suffix}),
        'woow.document.log': env['woow.document.log'].sudo().create({
            'event': 'view', 'result': 'ok', 'channel': 'backend'})}
    assert set(records) == set(WOOW_MODELS)
    env.flush_all()
    return records, users


def _notify(env, user, record, **files):
    """``message_notify`` as ``user``, on an empty recordset of another model, as an RPC call is."""
    return env['res.partner'].with_user(user).message_notify(
        model=record._name, res_id=record.id, body='notice',
        partner_ids=[user.partner_id.id], **files)


def _pending(env, user, **vals):
    """A pending attachment of ``user`` (what the composer of the web client uploads)."""
    return env['ir.attachment'].with_user(user).create(dict({
        'name': 'pending.txt', 'raw': b'planted', 'type': 'binary',
        'res_model': 'mail.compose.message', 'res_id': 0}, **vals))


@tagged('woow_docmgmt_core')
class TestNotificationFilesNeedTheRightToWrite(TransactionCase):

    def setUp(self):
        super().setUp()
        self.records, self.users = _fixture(self.env, 'orm')

    def _shapes(self, user, record):
        """The ways a notification can carry a file, as keyword arguments."""
        shapes = {
            'bytes': {'attachments': [('planted.txt', b'planted')]},
            'a pending attachment': {'attachment_ids': [_pending(self.env, user).id]},
            'a public tokenised one': {'attachment_ids': [
                _pending(self.env, user, public=True, access_token='planted-token').id]}}
        if record._name == 'woow.document':
            # a file that the record itself holds: the one of the document
            shapes['the file of the document'] = {
                'attachment_ids': [record.sudo().attachment_id.id]}
        return shapes

    def test_a_caller_who_may_not_write_the_record_cannot_attach_a_file(self):
        for who in ('outsider', 'reader', 'portal'):
            user = self.users[who]
            for model, record in self.records.items():
                shapes = self._shapes(user, record) if who != 'portal' else {
                    'bytes': {'attachments': [('planted.txt', b'planted')]}}
                for shape, files in shapes.items():
                    with self.subTest(caller=who, model=model, files=shape), \
                            self.assertRaises(AccessError):
                        _notify(self.env, user, record, **files)

    def test_a_record_that_does_not_exist_is_refused_too(self):
        folder = self.records['woow.document.folder']
        for who in ('outsider', 'reader', 'editor'):
            with self.subTest(caller=who), self.assertRaises(AccessError):
                self.env['res.partner'].with_user(self.users[who]).message_notify(
                    model='woow.document.folder', res_id=folder.id + 10 ** 6, body='notice',
                    partner_ids=[self.users[who].partner_id.id],
                    attachments=[('planted.txt', b'planted')])

    def test_a_value_that_is_not_an_id_is_refused(self):
        user = self.users['outsider']
        folder = self.records['woow.document.folder']
        for res_id in (str(folder.id), True, [folder.id], 1.0 * folder.id):
            with self.subTest(res_id=res_id), self.assertRaises(AccessError):
                self.env['res.partner'].with_user(user).message_notify(
                    model='woow.document.folder', res_id=res_id, body='notice',
                    partner_ids=[user.partner_id.id], attachments=[('planted.txt', b'planted')])

    def test_a_caller_who_may_write_the_record_can_still_attach_a_file(self):
        editor = self.users['editor']
        for model in ('woow.document', 'woow.document.folder'):
            record = self.records[model]
            with self.subTest(model=model, files='bytes'):
                message = _notify(self.env, editor, record,
                                  attachments=[('editor-%s.txt' % model, b'by an editor')])
                self.assertEqual(message.sudo().attachment_ids.mapped('res_id'), [record.id])
            with self.subTest(model=model, files='a pending attachment'):
                pending = _pending(self.env, editor)
                _notify(self.env, editor, record, attachment_ids=[pending.id])
                self.assertEqual((pending.sudo().res_model, pending.sudo().res_id),
                                 (model, record.id))

    def test_the_superuser_can_attach_files_to_every_model_of_the_module(self):
        for model, record in self.records.items():
            with self.subTest(model=model):
                message = self.env['res.partner'].sudo().message_notify(
                    model=model, res_id=record.id, body='notice',
                    partner_ids=[self.users['reader'].partner_id.id],
                    attachments=[('system.txt', b'by the system')])
                self.assertEqual(message.attachment_ids.mapped('res_id'), [record.id])

    def test_notifications_without_a_file_and_posts_are_unchanged(self):
        reader = self.users['reader']
        document = self.records['woow.document']
        # a notification without a file, and one with a file on a record of another model
        message = _notify(self.env, reader, document)
        self.assertEqual(message.sudo().message_type, 'user_notification')
        own = reader.partner_id
        message = self.env['res.partner'].with_user(reader).message_notify(
            model='res.partner', res_id=own.id, body='notice', partner_ids=[own.id],
            attachments=[('own.txt', b'about me')])
        self.assertEqual(message.sudo().attachment_ids.mapped('res_id'), [own.id])
        # a follower who can only read posts a comment with a file
        document.with_user(reader).message_subscribe(partner_ids=[own.id])
        posted = document.with_user(reader).message_post(
            body='reply', attachments=[('reply.txt', b'a reply')], message_type='comment',
            subtype_xmlid='mail.mt_comment')
        self.assertEqual(posted.sudo().attachment_ids.mapped('res_id'), [document.id])


@tagged('woow_docmgmt_http')
class TestNotificationFilesNeedTheRightToWriteRpc(HttpCase):

    def setUp(self):
        super().setUp()
        self.password = secrets.token_urlsafe(16)
        self.records, self.users = _fixture(self.env, 'rpc', password=self.password)

    def _notify(self, who, record, **files):
        self.authenticate(self.users[who].login, self.password)
        return self.make_jsonrpc_request(
            '/web/dataset/call_kw/res.partner/message_notify', {
                'model': 'res.partner', 'method': 'message_notify', 'args': [[]],
                'kwargs': dict(files, model=record._name, res_id=record.id, body='notice',
                               partner_ids=[self.users[who].partner_id.id])})

    def _files(self, record):
        return self.env['ir.attachment'].sudo().search([
            ('res_model', '=', record._name), ('res_id', '=', record.id)])

    def test_an_internal_user_without_rights_cannot_plant_a_file_over_rpc(self):
        for who in ('outsider', 'reader'):
            for model in ('woow.document.folder', 'woow.document', 'woow.document.tag'):
                record = self.records[model]
                with self.subTest(caller=who, model=model):
                    with self.assertRaises(JsonRpcException):
                        self._notify(who, record, attachments=[['planted.txt', 'planted']])
                    self.assertFalse(self._files(record))

    def test_an_editor_can_attach_a_file_over_rpc(self):
        folder = self.records['woow.document.folder']
        self._notify('editor', folder, attachments=[['by-an-editor.txt', 'by an editor']])
        self.assertEqual(self._files(folder).mapped('name'), ['by-an-editor.txt'])
