# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""An email reply is accepted on a document or a folder only from an internal user who can read it.

Core posts an incoming reply as OdooBot: ``message_post`` runs with the user of the gateway, so the
guards of the module that look at the user who posts (``_deny_non_internal`` and the like) judge
OdooBot and let every sender in. Anyone who holds the Message-Id of a message of the record, a
portal contact, an unknown address or a contact that staff added as a follower, could write
messages and files into the chatter by email, where the web and RPC refuse the same people (DEC-11).

``message_update`` of the two models is where core hands over the sender, before it posts and before
anything is written: it gets the author partner that core found for the From address. The module
refuses, with one error for every cause, unless that partner carries the address of the mail and has
an active internal user who passes the read rules of the record. These tests drive the real entry
(``message_process`` with the raw text of a mail, as fetchmail and the mail gateway script do) for
every kind of sender, both routes to an existing record (a reply to a message of it, and the record
that the caller names), both models and an alias that points at a record, and check that a refusal
leaves nothing behind: no message, no file, no follower, no notification.
"""
import base64
import secrets
from contextlib import closing, contextmanager
from email.message import EmailMessage
from unittest.mock import patch

from odoo import Command
from odoo.addons.mail.models.mail_thread import MailThread
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user
from odoo.tools import email_normalize


_MODELS = ('woow.document', 'woow.document.folder')
_USER = 'woow_document_management.group_woow_document_user'
_MANAGER = 'woow_document_management.group_woow_document_manager'
_NO_SIGNUP_MAIL = {'no_reset_password': True}


def _raw(sender, parent=None, to='docs@example.test', attach=False, extra=()):
    """(Message-Id, bytes) of a mail from ``sender``, a reply to ``parent`` when it is given."""
    mail = EmailMessage()
    message_id = '<%s@example.test>' % secrets.token_hex(12)
    mail['From'] = sender
    mail['To'] = to
    mail['Subject'] = 'Re: inbound %s' % secrets.token_hex(3)
    mail['Message-Id'] = message_id
    if parent:
        mail['In-Reply-To'] = parent
        mail['References'] = parent
    for name, value in extra:
        mail[name] = value
    mail.set_content('Inbound body %s' % secrets.token_hex(4))
    if attach:
        mail.add_attachment(b'attached by email', maintype='application',
                            subtype='octet-stream', filename='inbound.bin')
    return message_id, mail.as_bytes()


class _Refusal:
    exception = None


class _InboundFixture:
    """A folder and a document with their senders, and the means to send a mail and look at the state."""

    @contextmanager
    def _refused(self):
        """The call inside is refused with an ``AccessError``, and what it wrote before is kept.

        ``assertRaises`` of the test framework rolls back the savepoint of the call that failed. A mail
        gateway does not: fetchmail catches the error of a mail, marks the mail as processed and
        commits, so the writes that the refused mail made before it was refused stay. What a refusal
        leaves behind is looked at without that rollback.
        """
        refusal = _Refusal()
        try:
            yield refusal
        except AccessError as error:
            refusal.exception = error
        else:
            self.fail('the call was not refused')

    @classmethod
    def _make_user(cls, login, groups, **extra):
        env = cls.env
        return new_test_user(
            env, login='wdoc-in-%s' % login, groups=groups, name='Inbound %s' % login,
            email='Inbound.%s@Example.test' % login, context=_NO_SIGNUP_MAIL, **extra)

    @classmethod
    def _make_fixture(cls):
        env = cls.env
        cls.admin = env.ref('base.user_admin')
        cls.company_a = env.company
        cls.company_b = env['res.company'].sudo().create({'name': 'Inbound company B'})
        cls.reader = cls._make_user('reader', _USER)
        cls.editor = cls._make_user('editor', _USER)
        cls.manager = cls._make_user('manager', _MANAGER)
        cls.stranger = cls._make_user('stranger', _USER)
        cls.plain = cls._make_user('plain', 'base.group_user')
        cls.left = cls._make_user('left', _USER)
        cls.archived = cls._make_user('archived', _USER)
        cls.other_company = cls._make_user(
            'othercompany', _USER, company_id=cls.company_b.id,
            company_ids=[Command.set([cls.company_b.id])])
        cls.contact = env['res.partner'].sudo().create({
            'name': 'Inbound staff contact', 'email': 'inbound-contact@example.test'})
        cls.portal_partner = env['res.partner'].sudo().create({
            'name': 'Inbound portal contact', 'email': 'inbound-portal@example.test'})
        cls.portal = new_test_user(
            env, login='wdoc-in-portal', groups='base.group_portal',
            partner_id=cls.portal_partner.id, email=cls.portal_partner.email,
            context=_NO_SIGNUP_MAIL)
        cls.folder = env['woow.document.folder'].sudo().create({
            'name': 'Inbound folder', 'owner_id': cls.admin.id})
        access = env['woow.document.folder.access'].sudo()
        for user, level in ((cls.reader, 'read'), (cls.editor, 'edit'), (cls.left, 'read'),
                            (cls.archived, 'read'), (cls.other_company, 'read')):
            access.create({'folder_id': cls.folder.id, 'subject_type': 'user',
                           'user_id': user.id, 'level': level})
        access.create({'folder_id': cls.folder.id, 'subject_type': 'partner',
                       'partner_id': cls.portal_partner.id, 'level': 'read'})
        cls.document = env['woow.document'].sudo().create({
            'name': 'inbound.txt', 'folder_id': cls.folder.id,
            'datas': base64.b64encode(b'inbound fixture')})
        env.flush_all()
        cls.records = (cls.document, cls.folder)
        # the users who lose the right after the grant was made
        cls.left.sudo().write({'groups_id': [Command.unlink(env.ref(_USER).id)]})
        cls.archived.sudo().write({'active': False})
        for record in cls.records:
            record.sudo().message_subscribe(partner_ids=[
                cls.reader.partner_id.id, cls.portal_partner.id, cls.contact.id,
                cls.left.partner_id.id, cls.archived.partner_id.id])
        # a question of the administrator on each record, to reply to
        cls.questions = {
            record._name: record.with_user(cls.admin).message_post(
                body='A question', message_type='comment', subtype_xmlid='mail.mt_comment')
            for record in cls.records}
        env.flush_all()

    @property
    def gateway(self):
        """The mail gateway of fetchmail: the superuser."""
        return self.env['mail.thread'].sudo()

    def _state(self):
        """What a mail can write: messages, files, followers, notifications and mails to send."""
        self.env.flush_all()
        env = self.env
        followers = env['mail.followers'].sudo().search([('res_model', 'in', list(_MODELS))])
        return {
            'messages': env['mail.message'].sudo().search_count([('model', 'in', list(_MODELS))]),
            'files': env['ir.attachment'].sudo().with_context(active_test=False).search_count([]),
            'followers': sorted((f.res_model, f.res_id, f.partner_id.id) for f in followers),
            'notifications': env['mail.notification'].sudo().search_count([]),
            'mails': env['mail.mail'].sudo().search_count([]),
        }

    def _records_count(self):
        """The records of both models, trashed ones too, and the messages of their chatter."""
        return tuple(self.env[model].sudo().with_context(active_test=False).search_count([])
                     for model in _MODELS) + (self._state()['messages'],)

    def _author(self, record, address):
        """The partner that core finds for ``address`` on ``record`` (what the route puts in the values)."""
        return self.env['mail.thread'].sudo()._mail_find_partner_from_emails(
            [address], records=record)[0]

    def _send(self, record, sender, route='reply', **kwargs):
        """Process a mail from ``sender`` for ``record`` the way a gateway does.

        'reply' answers a message of the record and names no model (the mail gateway script),
        'record' names the record and answers nothing (the record that the caller names).
        Returns (Message-Id, result of ``message_process``).
        """
        parent = self.questions[record._name].message_id if route == 'reply' else None
        message_id, raw = _raw(sender, parent=parent, **kwargs)
        if route == 'reply':
            return message_id, self.gateway.message_process(False, raw)
        return message_id, self.gateway.message_process(record._name, raw, thread_id=record.id)

    def _posted(self, record, message_id):
        return self.env['mail.message'].sudo().search([('message_id', '=', message_id)])


@tagged('woow_docmgmt_core')
class TestAnInboundReplyNeedsAReader(_InboundFixture, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()

    def _refused_senders(self):
        """The senders that must not write into the chatter: (label, From header)."""
        return [
            ('an internal user with no right on the record', self.stranger.email),
            ('an internal user without the document group', self.plain.email),
            ('a portal user who follows the record', self.portal.email),
            ('an unknown address', 'nobody-inbound@example.test'),
            ('a contact without an account that staff added', self.contact.email),
            ('a user who was archived', self.archived.email),
            ('a user who left the document group', self.left.email),
            ('a user of another company', self.other_company.email),
            ('OdooBot', 'OdooBot <odoobot@example.com>'),
        ]

    def test_an_internal_user_who_can_read_the_record_is_posted(self):
        for record in self.records:
            for route in ('reply', 'record'):
                for who in (self.reader, self.editor, self.manager):
                    with self.subTest(model=record._name, route=route, sender=who.login):
                        before = self._state()
                        message_id, result = self._send(record, who.email, route)
                        self.assertEqual(result, record.id)
                        message = self._posted(record, message_id)
                        self.assertEqual((message.model, message.res_id, len(message)),
                                         (record._name, record.id, 1))
                        self.assertEqual((message.author_id, message.message_type),
                                         (who.partner_id, 'email'))
                        self.assertEqual(self._state()['messages'], before['messages'] + 1)
                        # the sender is subscribed, as when posting in the web client
                        self.assertIn(who.partner_id, record.sudo().message_partner_ids)

    def test_a_sender_who_may_not_reply_is_refused_and_nothing_is_written(self):
        for record in self.records:
            for route in ('reply', 'record'):
                for label, sender in self._refused_senders():
                    with self.subTest(model=record._name, route=route, sender=label):
                        before = self._state()
                        with self._refused():
                            self._send(record, sender, route)
                        self.assertEqual(self._state(), before)

    def test_a_refused_sender_with_a_file_creates_no_attachment(self):
        for record in self.records:
            for route in ('reply', 'record'):
                for label, sender in self._refused_senders():
                    with self.subTest(model=record._name, route=route, sender=label):
                        before = self._state()
                        with self._refused():
                            self._send(record, sender, route, attach=True)
                        self.assertEqual(self._state(), before)
        # the control: the file of a reader is the one file of the post, kept on the record
        for record in self.records:
            with self.subTest(model=record._name, sender='the reader'):
                before = self._state()
                message_id, _result = self._send(record, self.reader.email, attach=True)
                file = self._posted(record, message_id).attachment_ids
                self.assertEqual((file.name, file.res_model, file.res_id),
                                 ('inbound.bin', record._name, record.id))
                self.assertEqual(self._state()['files'], before['files'] + 1)

    def test_a_refusal_is_the_same_for_every_cause_and_for_a_record_that_is_not_there(self):
        """One error: it names neither the cause nor whether the record exists."""
        texts = set()
        for record in self.records:
            for route in ('reply', 'record'):
                for label, sender in self._refused_senders():
                    with self.subTest(model=record._name, route=route, sender=label):
                        with self._refused() as caught:
                            self._send(record, sender, route)
                        texts.add(str(caught.exception))
        # a mail that is for no record at all is refused by message_new, and a record that is not
        # there by message_update, with the same words
        values = {'author_id': self.reader.partner_id.id, 'email_from': self.reader.email}
        for model in _MODELS:
            with self.subTest(model=model, record='none'):
                _message_id, raw = _raw(self.reader.email)
                with self._refused() as caught:
                    self.gateway.message_process(model, raw)
                texts.add(str(caught.exception))
            with self.subTest(model=model, record='missing'):
                with self._refused() as caught:
                    self.env[model].sudo().browse(10 ** 7).message_update(values)
                texts.add(str(caught.exception))
        self.assertEqual(texts, {'Documents and folders do not accept this email.'})

    def test_the_mail_gateway_script_of_a_system_administrator_is_judged_like_fetchmail(self):
        """The script logs in as a system administrator, not as the superuser: the sender counts."""
        gateway = self.env['mail.thread'].with_user(self.admin)
        self.assertTrue(self.admin._is_system() and not gateway.env.su)
        for record in self.records:
            with self.subTest(model=record._name, sender='the reader'):
                before = self._state()
                _message_id, raw = _raw(self.reader.email, parent=self.questions[record._name].message_id)
                self.assertEqual(gateway.message_process(False, raw), record.id)
                self.assertEqual(self._state()['messages'], before['messages'] + 1)
            for label, sender in self._refused_senders():
                with self.subTest(model=record._name, sender=label):
                    before = self._state()
                    _message_id, raw = _raw(sender, parent=self.questions[record._name].message_id)
                    with self._refused():
                        gateway.message_process(False, raw)
                    self.assertEqual(self._state(), before)

    def test_an_archived_user_is_refused_when_the_context_lists_archived_records_too(self):
        """The account must be active whatever the context of the call says about archived records."""
        gateway = self.gateway.with_context(active_test=False)
        for record in self.records:
            for who in (self.archived,):
                with self.subTest(model=record._name, sender=who.login):
                    self.assertEqual(self._author(record, who.email), who.partner_id)
                    self.assertIn(who, who.partner_id.with_context(active_test=False).user_ids)
                    before = self._state()
                    _message_id, raw = _raw(who.email, parent=self.questions[record._name].message_id)
                    with self._refused():
                        gateway.message_process(False, raw)
                    self.assertEqual(self._state(), before)

    def test_a_mail_still_cannot_create_a_record(self):
        for model in _MODELS:
            for sender in (self.reader.email, self.manager.email, self.portal.email,
                           'nobody-inbound@example.test'):
                with self.subTest(model=model, sender=sender):
                    before = self._records_count()
                    _message_id, raw = _raw(sender)
                    with self._refused():
                        self.gateway.message_process(model, raw)
                    with self._refused():
                        self.env[model].sudo().message_new({'subject': 'Direct'}, {})
                    self.assertEqual(self._records_count(), before)

    def test_the_sender_is_judged_by_the_author_partner_that_core_found(self):
        """The forms of an address that core resolves to the reader are accepted, nothing more.

        Case and display names do not matter (core normalises the address), only the first address of
        the header counts, and neither a display name that looks like an address nor a header other than
        From does.
        """
        email = self.reader.email.lower()
        accepted = [
            ('mixed case', self.reader.email.upper()),
            ('a display name', 'Inbound Reader <%s>' % email),
            ('a quoted display name with a comma', '"Reader, Inbound" <%s>' % self.reader.email),
            ('an encoded display name', 'Réader Inbound <%s>' % email),
            ('the address first in the header', '%s, nobody-inbound@example.test' % email),
        ]
        refused = [
            ('the address second in the header', 'nobody-inbound@example.test, %s' % email),
            ('the address only in the display name', '"%s" <nobody-inbound@example.test>' % email),
            ('a plus address', email.replace('@', '+tag@')),
            ('another domain', email.replace('example.test', 'example.invalid')),
        ]
        for record in self.records:
            for label, sender in accepted:
                with self.subTest(model=record._name, accepted=label):
                    message_id, result = self._send(record, sender)
                    self.assertEqual(result, record.id)
                    self.assertEqual(self._posted(record, message_id).author_id,
                                     self.reader.partner_id)
            for label, sender in refused:
                with self.subTest(model=record._name, refused=label):
                    before = self._state()
                    with self._refused():
                        self._send(record, sender)
                    self.assertEqual(self._state(), before)
            with self.subTest(model=record._name, refused='a Sender or Reply-To header'):
                before = self._state()
                with self._refused():
                    self._send(record, 'nobody-inbound@example.test', extra=(
                        ('Sender', self.reader.email), ('Reply-To', self.reader.email)))
                self.assertEqual(self._state(), before)

    def test_an_address_that_belongs_to_several_partners_is_judged_as_core_resolved_it(self):
        """The partner that core picks for the address is the one that counts, whoever else has it."""
        env = self.env
        for record in self.records:
            with closing(env.cr.savepoint()):
                twin = env['res.partner'].sudo().create({
                    'name': 'Inbound twin', 'email': self.reader.email})
                with self.subTest(model=record._name, twin='a plain partner'):
                    # a partner with a user comes before one without: the reader is the author
                    self.assertEqual(self._author(record, self.reader.email),
                                     self.reader.partner_id)
                    message_id, _result = self._send(record, self.reader.email)
                    self.assertEqual(self._posted(record, message_id).author_id,
                                     self.reader.partner_id)
                with self.subTest(model=record._name, twin='a follower of the record'):
                    # core prefers a follower of the record whose company is the record's: the twin
                    # is picked, it has no account, and the mail is refused although the address is
                    # the reader's
                    twin.write({'company_id': record.company_id.id})
                    record.sudo().message_subscribe(partner_ids=[twin.id])
                    self.assertEqual(self._author(record, self.reader.email), twin)
                    before = self._state()
                    with self._refused():
                        self._send(record, self.reader.email)
                    self.assertEqual(self._state(), before)

    def test_a_partner_with_several_users_is_accepted_when_one_of_them_qualifies(self):
        """The email does not say which user wrote it: one account that qualifies is enough."""
        env = self.env
        group = env.ref(_USER)
        for record in self.records:
            for order in ('the reader first', 'the reader last'):
                tag = '%s-%s-%s' % (record._name.replace('.', '-'), record.id, order.split()[-1])
                address = 'inbound-shared-%s@example.test' % tag
                partner = env['res.partner'].sudo().create({'name': 'Inbound shared',
                                                            'email': address})

                def make(kind, groups):
                    return new_test_user(
                        env, login='wdoc-in-shared-%s-%s' % (kind, tag), groups=groups,
                        name='Shared %s' % kind, partner_id=partner.id, email=address,
                        context=_NO_SIGNUP_MAIL)

                # the users of a partner come in the order they were made: a portal user, a
                # user without a grant, and the reader, in either order
                portal = make('portal', 'base.group_portal')
                if order == 'the reader first':
                    reader, stranger = make('reader', _USER), make('stranger', _USER)
                else:
                    stranger, reader = make('stranger', _USER), make('reader', _USER)
                grant = env['woow.document.folder.access'].sudo().create({
                    'folder_id': self.folder.id, 'subject_type': 'user', 'user_id': reader.id,
                    'level': 'read'})
                self.assertEqual(partner.user_ids, portal | reader | stranger)
                self.assertEqual(self._author(record, address), partner)
                with self.subTest(model=record._name, order=order, users='one of three reads'):
                    message_id, _result = self._send(record, address)
                    self.assertEqual(self._posted(record, message_id).author_id, partner)
                for label, change in (
                        ('the reader has no grant', lambda: grant.unlink()),
                        ('the reader is archived', lambda: reader.write({'active': False})),
                        ('the reader left the document group',
                         lambda: reader.write({'groups_id': [Command.unlink(group.id)]}))):
                    with self.subTest(model=record._name, order=order, users=label), \
                            closing(env.cr.savepoint()):
                        change()
                        env.flush_all()
                        self.assertTrue(partner.active)
                        before = self._state()
                        with self._refused():
                            self._send(record, address)
                        self.assertEqual(self._state(), before)
                with self.subTest(model=record._name, order=order, users='the reader and a portal user'), \
                        closing(env.cr.savepoint()):
                    stranger.write({'active': False})
                    message_id, _result = self._send(record, address)
                    self.assertEqual(self._posted(record, message_id).author_id, partner)

    def test_a_partner_with_two_addresses_is_known_by_the_first(self):
        env = self.env
        self.reader.partner_id.sudo().write({
            'email': 'Inbound.reader@example.test, inbound-second@example.test'})
        env.flush_all()
        for record in self.records:
            with self.subTest(model=record._name, address='the first'):
                message_id, _result = self._send(record, 'inbound.reader@example.test')
                self.assertEqual(self._posted(record, message_id).author_id, self.reader.partner_id)
            with self.subTest(model=record._name, address='the second'):
                before = self._state()
                with self._refused():
                    self._send(record, 'inbound-second@example.test')
                self.assertEqual(self._state(), before)

    def test_an_alias_that_points_at_a_record_is_judged_like_a_reply(self):
        """A mail to an alias of a record is a route to an existing record; an alias without one creates."""
        env = self.env
        for record in self.records:
            model_id = env['ir.model']._get_id(record._name)
            alias = env['mail.alias'].sudo().create({
                'alias_name': 'wdoc-inbound-%s-%s' % (record._name.replace('.', '-'), record.id),
                'alias_model_id': model_id, 'alias_force_thread_id': record.id,
                'alias_contact': 'everyone', 'alias_incoming_local': True})
            to = '%s@example.test' % alias.alias_name
            with self.subTest(model=record._name, alias='the reader'):
                before = self._state()
                _message_id, raw = _raw(self.reader.email, to=to)
                self.assertEqual(self.gateway.message_process(False, raw), record.id)
                self.assertEqual(self._state()['messages'], before['messages'] + 1)
            for label, sender in self._refused_senders():
                with self.subTest(model=record._name, alias=label):
                    before = self._state()
                    _message_id, raw = _raw(sender, to=to, attach=True)
                    with self._refused():
                        self.gateway.message_process(False, raw)
                    self.assertEqual(self._state(), before)
            # an alias with no record to update creates one, which message_new refuses (core would
            # mail the sender a notice about the alias, from a cursor of its own: not part of the test)
            creating = env['mail.alias'].sudo().create({
                'alias_name': 'wdoc-inbound-new-%s' % record._name.replace('.', '-'),
                'alias_model_id': model_id, 'alias_contact': 'everyone',
                'alias_incoming_local': True})
            with self.subTest(model=record._name, alias='one that creates, for the reader'), \
                    patch.object(type(env['mail.alias']), '_alias_bounce_incoming_email'):
                before = self._records_count()
                _message_id, raw = _raw(self.reader.email, to='%s@example.test' % creating.alias_name)
                with self._refused():
                    self.gateway.message_process(False, raw)
                self.assertEqual(self._records_count(), before)

    def test_the_caller_of_the_gateway_is_still_checked_first(self):
        """A caller who is neither the superuser nor a system administrator is refused, whoever wrote."""
        for record in self.records:
            for who in (self.reader, self.manager):
                with self.subTest(model=record._name, caller=who.login):
                    before = self._state()
                    _message_id, raw = _raw(
                        self.reader.email, parent=self.questions[record._name].message_id)
                    with self._refused():
                        self.env['mail.thread'].with_user(who).message_process(False, raw)
                    self.assertEqual(self._state(), before)

    def test_core_hands_the_sender_to_message_update_before_it_posts(self):
        """What the module relies on: one call per route, with the author partner of the From address."""
        seen = []
        original = MailThread.message_update

        def spy(this, msg_dict, update_vals=None):
            seen.append((this._name, this.ids, this.env.su, msg_dict.get('author_id'),
                         email_normalize(msg_dict.get('email_from'), strict=False),
                         self.env['mail.message'].sudo().search_count(
                             [('message_id', '=', msg_dict['message_id'])])))
            return original(this, msg_dict, update_vals=update_vals)

        for record in self.records:
            del seen[:]
            with patch.object(MailThread, 'message_update', autospec=True, side_effect=spy):
                message_id, _result = self._send(record, 'Inbound Reader <%s>' % self.reader.email)
            # the call is made as the superuser, with the partner of the address, and the message
            # is not there yet: it is posted after the call
            self.assertEqual(seen, [(record._name, [record.id], True, self.reader.partner_id.id,
                                     self.reader.email.lower(), 0)])
            self.assertEqual(self._posted(record, message_id).author_id, self.reader.partner_id)

    def test_message_update_judges_the_values_it_is_given(self):
        """Called directly, it answers the same way and writes nothing when it refuses."""
        author, address = self.reader.partner_id.id, self.reader.email
        edit = {'description': '<p>written by a mail</p>'}
        wrong = [
            ('no values', {}), ('no author', {'email_from': address}),
            ('no address', {'author_id': author}),
            ('an author that is False', {'author_id': False, 'email_from': address}),
            ('an author that is True', {'author_id': True, 'email_from': address}),
            ('an author that is zero', {'author_id': 0, 'email_from': address}),
            ('an author that is a text', {'author_id': str(author), 'email_from': address}),
            ('an author that is a list', {'author_id': [author], 'email_from': address}),
            ('an author that is a float', {'author_id': float(author), 'email_from': address}),
            ('an author that is a record', {'author_id': self.reader.partner_id,
                                            'email_from': address}),
            ('an address of someone else', {'author_id': author,
                                            'email_from': self.editor.email}),
            ('an address that is not a text', {'author_id': author, 'email_from': [address]}),
            ('a partner that is not there', {'author_id': 10 ** 7, 'email_from': address}),
            ('a stranger', {'author_id': self.stranger.partner_id.id,
                            'email_from': self.stranger.email}),
            ('a portal user', {'author_id': self.portal_partner.id,
                               'email_from': self.portal.email}),
        ]
        for record in self.records:
            before = record.sudo().description
            for label, values in wrong:
                with self.subTest(model=record._name, values=label), self._refused():
                    record.sudo().message_update(values, edit)
            for values in ('not a dict', None, [author]):
                with self.subTest(model=record._name, values=repr(values)), \
                        self._refused():
                    record.sudo().message_update(values, edit)
            self.assertEqual(record.sudo().description, before)
            with self.subTest(model=record._name, values='the reader'):
                record.sudo().message_update({'author_id': author, 'email_from': address}, edit)
                self.assertIn('written by a mail', record.sudo().description)

    def test_message_update_refuses_the_whole_set_when_one_record_is_out_of_reach(self):
        other = self.env['woow.document.folder'].sudo().create({
            'name': 'Inbound other folder', 'owner_id': self.admin.id})
        values = {'author_id': self.reader.partner_id.id, 'email_from': self.reader.email}
        before = (self.folder.sudo().description, other.sudo().description)
        for records in (self.folder | other, other | self.folder):
            with self._refused():
                records.sudo().message_update(values, {'description': '<p>by mail</p>'})
        self.assertEqual((self.folder.sudo().description, other.sudo().description), before)


@tagged('woow_docmgmt_core')
class TestTheSenderPassesTheSameRulesAsAFollower(_InboundFixture, TransactionCase):
    """A mail is accepted exactly when the real record rules let the sender's user read the record."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._make_fixture()
        env = cls.env
        access = env['woow.document.folder.access'].sudo()
        documents = env['woow.document'].sudo()
        folders = env['woow.document.folder'].sudo()
        groups = env['res.groups'].sudo()
        cls.owner = cls._make_user('owner', _USER)
        cls.shared = cls._make_user('shared', _USER)
        user_group = env.ref(_USER)
        cls.readers_group = groups.create({'name': 'Inbound readers'})
        cls.group_reader = cls._make_user(
            'groupreader', _USER, groups_id=[user_group.id, cls.readers_group.id])
        cls.both = cls._make_user(
            'bothcompanies', _USER,
            company_ids=[Command.set([cls.company_a.id, cls.company_b.id])])
        access.create({'folder_id': cls.folder.id, 'subject_type': 'user',
                       'user_id': cls.owner.id, 'level': 'upload'})
        owned = documents.create({'name': 'owned.txt', 'folder_id': cls.folder.id,
                                  'owner_id': cls.owner.id,
                                  'datas': base64.b64encode(b'owned by a member')})
        shared = documents.create({'name': 'shared.txt', 'folder_id': cls.folder.id,
                                   'datas': base64.b64encode(b'shared with a user')})
        shared.write({'shared_user_ids': [Command.set([cls.shared.id])]})
        archived = documents.create({'name': 'archived.txt', 'folder_id': cls.folder.id,
                                     'owner_id': cls.owner.id,
                                     'datas': base64.b64encode(b'archived document')})
        archived.write({'active': False})
        group_folder = folders.create({'name': 'Inbound group folder', 'owner_id': cls.admin.id})
        access.create({'folder_id': group_folder.id, 'subject_type': 'group',
                       'group_id': cls.readers_group.id, 'level': 'read'})
        group_document = documents.create({'name': 'group.txt', 'folder_id': group_folder.id,
                                           'datas': base64.b64encode(b'group document')})
        trashed_folder = folders.create({'name': 'Inbound trashed', 'owner_id': cls.admin.id})
        access.create({'folder_id': trashed_folder.id, 'subject_type': 'user',
                       'user_id': cls.reader.id, 'level': 'read'})
        in_trashed = documents.create({'name': 'in-trash.txt', 'folder_id': trashed_folder.id,
                                       'datas': base64.b64encode(b'document in a folder in the trash')})
        trashed_folder.with_user(cls.admin).action_trash()
        folder_b = folders.create({'name': 'Inbound folder B', 'company_id': cls.company_b.id,
                                   'owner_id': False})
        access.create([{'folder_id': folder_b.id, 'subject_type': 'user',
                        'user_id': user.id, 'level': 'read'}
                       for user in (cls.both, cls.reader, cls.other_company)])
        document_b = documents.create({'name': 'company-b.txt', 'folder_id': folder_b.id,
                                       'datas': base64.b64encode(b'company B document')})
        cls.users = (cls.reader, cls.editor, cls.manager, cls.stranger, cls.plain, cls.left,
                     cls.archived, cls.other_company, cls.owner, cls.shared, cls.group_reader,
                     cls.both)
        cls.parity_records = (cls.folder, cls.document, owned, shared, archived, group_folder,
                              group_document, trashed_folder, in_trashed, folder_b, document_b)
        # a question of the superuser on each record: an administrator of company A cannot post on B
        cls.parity_questions = {
            record: record.sudo().message_post(
                body='Question', message_type='comment', subtype_xmlid='mail.mt_comment')
            for record in cls.parity_records}
        env.flush_all()

    def test_a_mail_is_accepted_exactly_when_the_user_can_read_the_record(self):
        accepted = set()
        for record in self.parity_records:
            parent = self.parity_questions[record].message_id
            for user in self.users:
                reads = bool(user.active and not user.share and record.with_user(user).with_context(
                    active_test=False).has_access('read'))
                before = self._state()
                _message_id, raw = _raw(user.email, parent=parent)
                with self.subTest(record=record.display_name, user=user.login, reads=reads):
                    if reads:
                        self.assertEqual(self.gateway.message_process(False, raw), record.id)
                        self.assertEqual(self._state()['messages'], before['messages'] + 1)
                        accepted.add((record, user))
                    else:
                        with self._refused():
                            self.gateway.message_process(False, raw)
                        self.assertEqual(self._state(), before)
        # the fixtures reach both answers on every kind of grant
        folder, document, owned, shared, archived, group_folder, group_document, trashed_folder, \
            in_trashed, folder_b, document_b = self.parity_records
        expected = (
            (folder, self.reader, True), (folder, self.stranger, False),
            (folder, self.manager, True), (folder, self.plain, False),
            (folder, self.left, False), (folder, self.archived, False),
            (folder, self.other_company, False), (folder, self.owner, True),
            (shared, self.shared, True), (document, self.shared, False),
            (folder, self.shared, False),
            (owned, self.owner, True), (owned, self.reader, True),
            (archived, self.owner, True), (archived, self.reader, False),
            (archived, self.manager, True),
            (group_folder, self.group_reader, True), (group_document, self.group_reader, True),
            (group_folder, self.reader, False),
            (trashed_folder, self.reader, False), (trashed_folder, self.manager, True),
            (in_trashed, self.reader, False),
            (folder_b, self.both, True), (folder_b, self.reader, False),
            (document_b, self.both, True), (document_b, self.manager, False),
        )
        for record, user, accepted_by_rules in expected:
            with self.subTest(expected=record.display_name, user=user.login):
                self.assertEqual((record, user) in accepted, accepted_by_rules)

    def test_the_answer_is_the_one_of_the_rules_for_the_helper_too(self):
        from ..models.mail_thread_guard import _reply_sender_reads
        for record in self.parity_records:
            for user in self.users:
                reads = bool(user.active and not user.share and record.with_user(user).with_context(
                    active_test=False).has_access('read'))
                values = {'author_id': user.partner_id.id, 'email_from': user.email}
                with self.subTest(record=record.display_name, user=user.login):
                    self.assertEqual(_reply_sender_reads(record, values), reads)
