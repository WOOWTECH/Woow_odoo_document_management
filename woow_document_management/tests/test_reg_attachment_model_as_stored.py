# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""The model of an attachment is judged as the column keeps it, and the database is the last line.

Every guard of ``ir.attachment`` starts from ``res_model``: a row of a document or a folder is never
public and never holds a token. The guard compared the value that came in (``value in WOOW_MODELS``)
while the ORM keeps ``str(value)`` (``bytes`` decoded), and an XML-RPC call delivers a base64 value as
``xmlrpc.client.Binary``, for which ``Binary(b'woow.document') == 'woow.document'`` is False and
``str()`` is the model name. So a user with no right on the document could make a public file, name the
record with ``res_id``, and then write the model as a Binary: the guards saw a model that is not one
of the module's, and the row became a public file of the document that a visitor could fetch.

Three things now hold, and the tests below pin each of them on its own:

* the model is judged as the column keeps it (``stored_char`` and ``is_module_model``), at every guard;
* the database refuses a row of a module model that is public or has a token (a CHECK constraint
  built from ``WOOW_MODELS``), whatever stored it: it is the last line, for any path that the guard
  does not know;
* no shape of the five values (model, record, field, flag, token) leaves such a row open, for a plain
  internal user, an editor of the document or the superuser (the last tests walk many of them).

Core hashes the model of a create (``record_tuple_set``), so a ``Binary`` (which has no hash) cannot be
the model of a create, for anyone: a create is tried with bytes, a write with both.
"""
import base64
import random
import re
import xmlrpc.client

from psycopg2 import Error as DatabaseError, errors

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tests.common import get_db_name, new_test_user
from odoo.tools import mute_logger

from ..models.ir_attachment import (
    OPEN_ROW_CONSTRAINT, IrAttachment, char_is_set, is_module_model, stored_char)
from ..services.constants import WOOW_MODELS
from .test_reg_chatter_attachment_tokens import _ChatterFixture

CONSTRAINT = 'ir_attachment_%s' % OPEN_ROW_CONSTRAINT
ACCESS_ERROR = 4        # the fault code that /xmlrpc/2 gives an AccessError


def spellings(model):
    """The values that a Char column keeps as the name ``model``: the text, bytes and a Binary."""
    return [model, model.encode(), xmlrpc.client.Binary(model.encode())]


def row_of(env, attachment):
    """(model, record, public, token, field) of a row as the database holds it, around the ORM.

    The record is 0 when the column holds none.
    """
    env.flush_all()
    env.cr.execute(
        'SELECT res_model, COALESCE(res_id, 0), COALESCE(public, FALSE), access_token, res_field '
        'FROM ir_attachment WHERE id = %s', (attachment.id,))
    return env.cr.fetchone()


def open_rows(env):
    """The rows of module models that are public or hold a token: there must never be one."""
    env.flush_all()
    env.cr.execute(
        "SELECT id, res_model, COALESCE(public, FALSE), access_token FROM ir_attachment "
        "WHERE res_model = ANY(%s) AND (COALESCE(public, FALSE) OR COALESCE(access_token, '') <> '')",
        (list(WOOW_MODELS),))
    return env.cr.fetchall()


class _Fixture(_ChatterFixture):
    """A document and a folder, an editor of both, a plain internal user and the attachments model."""

    def _setup_rows(self):
        self._fixture()
        self.plain = new_test_user(self.env, login='wdoc-stored-plain', groups='base.group_user',
                                   name='Stored plain user', context={'no_reset_password': True})
        self.attachments = self.env['ir.attachment']
        self.targets = {'woow.document': self.document.id, 'woow.document.folder': self.folder.id,
                        'woow.document.folder.access': 0, 'woow.document.tag': 0,
                        'woow.document.log': 0}
        self.assertEqual(set(self.targets), set(WOOW_MODELS))

    def _file(self, user, **extra):
        values = dict({'name': 'stored.txt', 'raw': b'stored bytes', 'type': 'binary',
                       'mimetype': 'text/plain'}, **extra)
        return self.attachments.with_user(user).create(values)


@tagged('woow_docmgmt_core')
class TestTheModelOfAnAttachmentIsJudgedAsTheColumnKeepsIt(_Fixture, TransactionCase):
    """Bytes and a Binary are the name of a model, as they are for the column that stores them."""

    def setUp(self):
        super().setUp()
        self._setup_rows()

    def test_the_normaliser_mirrors_what_the_orm_stores_for_a_char_column(self):
        """``stored_char`` is ``_String.convert_to_cache``: the premise of every guard below."""
        field = self.attachments._fields['res_model']
        record = self.env['res.partner'].sudo().create({'name': 'Stored char partner'})
        for value in (False, None, '', 'woow.document', b'woow.document', b'', 0, 0.0, [], {}, (),
                      7, ['woow.document'], ' woow.document', xmlrpc.client.Binary(b'woow.document'),
                      xmlrpc.client.Binary(b''), record):
            with self.subTest(value=value):
                kept = field.convert_to_cache(value, self.attachments)
                self.assertEqual(stored_char(value) or None, kept or None)
                self.assertEqual(char_is_set(value), bool(kept))
                self.assertEqual(is_module_model(value), kept in WOOW_MODELS)
        # what the column keeps for the values that name a module model, in every spelling
        for model in WOOW_MODELS:
            for value in spellings(model):
                with self.subTest(model=model, value=value):
                    self.assertEqual(stored_char(value), model)
                    self.assertTrue(is_module_model(value))
                    if not isinstance(value, str):
                        self.assertNotEqual(value, model)   # the reason that a comparison fails
        # a name that is not exactly one of the module's is not one of them, in any type
        for value in (' woow.document', 'WOOW.DOCUMENT', 'woow.document\n', ['woow.document'],
                      ('woow.document',), {'woow.document'}, b'woow.documents', 'res.partner'):
            with self.subTest(value=value):
                self.assertFalse(is_module_model(value))

    def test_a_public_row_cannot_be_moved_onto_a_module_record_with_a_model_that_is_not_text(self):
        """The reproduced sequence: a public file with no model, ``res_id``, then the model as bytes.

        A plain internal user makes the file (public), an editor of the record makes a file with a
        token as well: the move is refused for every spelling of the model, the text included, and the
        row stays what it was.
        """
        callers = ((self.plain, {'public': True}), (self.editor, {'public': True}),
                   (self.editor, {'access_token': 'moved-token'}))
        for model, res_id in self.targets.items():
            for value in spellings(model):
                for user, extra in callers:
                    with self.subTest(model=model, value=value, user=user.login, extra=extra):
                        row = self._file(user, **extra)
                        row.write({'res_id': res_id})
                        with self.assertRaises(AccessError):
                            row.write({'res_model': value})
                        stored = row_of(self.env, row)
                        self.assertEqual(stored[:2], (None, res_id))
                        self.assertEqual((stored[2], stored[3]), (
                            extra.get('public', False), extra.get('access_token')))
                        self.assertFalse(open_rows(self.env))

    def test_a_row_that_is_not_open_still_moves_onto_a_record_the_caller_may_write(self):
        """The control of the refusal: the editor of the record attaches a closed file as before."""
        for model in ('woow.document', 'woow.document.folder'):
            for value in spellings(model):
                with self.subTest(model=model, value=value):
                    row = self._file(self.editor)
                    row.write({'res_id': self.targets[model]})
                    row.write({'res_model': value})
                    self.assertEqual(row_of(self.env, row)[:4],
                                     (model, self.targets[model], False, None))

    def test_a_create_for_a_model_that_is_not_text_is_judged_like_the_text(self):
        """The model of a create is read as the column keeps it, for the guard and for the defaults."""
        for model, res_id in self.targets.items():
            for value in spellings(model):
                for extra in ({'public': True}, {'access_token': 'created'}, {'res_field': 'datas'}):
                    with self.subTest(model=model, value=value, extra=extra):
                        with self.assertRaises(AccessError):
                            self._file(self.editor, res_model=value, res_id=res_id, **extra)
        self.assertFalse(open_rows(self.env))
        # a file that is not open is made as before (core hashes the model of a create: no Binary)
        for model in ('woow.document', 'woow.document.folder'):
            for value in spellings(model)[:2]:
                row = self._file(self.editor, res_model=value, res_id=self.targets[model])
                self.assertEqual(row_of(self.env, row)[:4], (model, self.targets[model], False, None))

    def test_the_superuser_closes_a_row_whose_model_is_kept_as_a_module_model(self):
        su = self.attachments.sudo()
        for model, res_id in self.targets.items():
            for value in spellings(model):
                if not isinstance(value, xmlrpc.client.Binary):
                    with self.subTest(model=model, value=value, way='create'):
                        row = su.create({
                            'name': 'su.txt', 'raw': b'su bytes', 'res_model': value,
                            'res_id': res_id, 'public': True, 'access_token': 'su-token'})
                        self.assertEqual(row_of(self.env, row)[:4], (model, res_id, False, None))
                for way, extra in (('a move that names the model and the record', {'res_id': res_id}),
                                   ('a move that names only the model', {})):
                    with self.subTest(model=model, value=value, way=way):
                        row = su.create({
                            'name': 'su.txt', 'raw': b'su bytes', 'res_model': 'res.partner',
                            'res_id': 0, 'public': True, 'access_token': 'su-token'})
                        row.write(dict(extra, res_model=value))
                        self.assertEqual(row_of(self.env, row)[2:4], (False, None))
                with self.subTest(model=model, value=value, way='the flag, for a row that is one'):
                    row = su.create({'name': 'su.txt', 'raw': b'su bytes', 'res_model': model,
                                     'res_id': res_id})
                    row.write({'public': True})
                    self.assertEqual(row_of(self.env, row)[2:4], (False, None))
        self.assertFalse(open_rows(self.env))

    def test_a_row_that_is_not_a_row_of_a_module_model_is_left_as_written(self):
        """Judged the same way: a name that the column keeps as something else is another model's."""
        su = self.attachments.sudo()
        for value in (' woow.document', 'WOOW.DOCUMENT', b'res.partner', 'res.partner'):
            with self.subTest(value=value, way='create'):
                row = su.create({'name': 'other.txt', 'raw': b'other', 'res_model': value,
                                 'res_id': 0, 'public': True, 'access_token': 'other-token'})
                self.assertEqual(row_of(self.env, row)[2:4], (True, 'other-token'))
                row = self._file(self.plain, res_model=value, res_id=0, public=True)
                self.assertEqual(row_of(self.env, row)[2], True)
        for value in (['woow.document'], xmlrpc.client.Binary(b'res.partner'),
                      xmlrpc.client.Binary(b' woow.document'), ('woow.document',)):
            with self.subTest(value=value, way='write'):
                row = su.create({'name': 'other.txt', 'raw': b'other', 'res_model': 'res.partner',
                                 'res_id': 0, 'public': True, 'access_token': 'other-token'})
                row.write({'res_model': value})
                self.assertEqual(row_of(self.env, row)[2:4], (True, 'other-token'))

    def test_the_file_of_a_field_is_judged_by_the_model_as_kept(self):
        """A pending file does not become the file of a field of a record by a model of another type.

        The row that already is the file of a field stays as it is when the write names its own model
        in another type: that is not a move, the column keeps the same text.
        """
        su = self.attachments.sudo()
        for model, res_id in self.targets.items():
            for value in spellings(model)[1:]:
                with self.subTest(model=model, value=value, way='a move'):
                    row = su.create({'name': 'pending.txt', 'raw': b'pending',
                                     'res_model': 'mail.compose.message', 'res_id': 0})
                    with self.assertRaises(AccessError):
                        row.write({'res_model': value, 'res_id': res_id, 'res_field': 'datas'})
                    self.assertEqual(row_of(self.env, row)[:2], ('mail.compose.message', 0))
                with self.subTest(model=model, value=value, way='a row that is its own file'):
                    own = su.create({'name': 'own.txt', 'raw': b'own', 'res_model': model,
                                     'res_id': res_id, 'res_field': 'datas'})
                    own.write({'res_model': value})
                    self.assertEqual((row_of(self.env, own)[:2], row_of(self.env, own)[4]),
                                     ((model, res_id), 'datas'))
                    with self.assertRaises(AccessError):
                        own.write({'res_model': value, 'res_id': res_id + 1})

    def test_a_notification_that_names_the_model_as_bytes_needs_the_right_to_write_the_record(self):
        """``message_notify`` links files as superuser: the model is read as the file will keep it."""
        pending = self._file(self.plain, res_model='mail.compose.message', res_id=0)
        thread = self.env['mail.thread'].with_user(self.plain)
        for model in ('woow.document', 'woow.document.folder'):
            for value in spellings(model):
                with self.subTest(model=model, value=value), self.assertRaises(AccessError):
                    thread._process_attachments_for_post(
                        [], [pending.id], {'message_type': 'user_notification', 'model': value,
                                           'res_id': self.targets[model]})
        # the control: whoever may write the record links the file, and it ends closed, as the text
        for value in spellings('woow.document'):
            with self.subTest(value=value, way='the editor'):
                own = self._file(self.editor, res_model='mail.compose.message', res_id=0,
                                 public=True, access_token='pending-token')
                self.env['mail.thread'].with_user(self.editor)._process_attachments_for_post(
                    [], [own.id], {'message_type': 'user_notification', 'model': value,
                                   'res_id': self.document.id})
                self.assertEqual(row_of(self.env, own)[:4],
                                 ('woow.document', self.document.id, False, None))

    def test_a_route_to_a_module_model_given_as_bytes_is_refused_to_a_user_who_is_not_an_administrator(self):
        thread = self.env['mail.thread'].with_user(self.editor)
        for model in ('woow.document', 'woow.document.folder'):
            for value in spellings(model):
                with self.subTest(model=model, value=value), self.assertRaises(AccessError):
                    thread._message_route_process(
                        self.env['mail.message'], {}, [(value, self.targets[model], None,
                                                        self.editor.id, None)])


@tagged('woow_docmgmt_core')
class TestTheDatabaseRefusesAnOpenRowOfAModuleModel(_Fixture, TransactionCase):
    """The last line: a statement that stores such a row fails with an integrity error."""

    def setUp(self):
        super().setUp()
        self._setup_rows()

    def _refused(self, statement, params=()):
        """``statement`` fails as a violation of the constraint, and the savepoint undoes it."""
        self.env.flush_all()
        with mute_logger('odoo.sql_db'), \
                self.assertRaises(errors.CheckViolation) as caught, self.cr.savepoint():
            self.env.cr.execute(statement, params)
        self.assertEqual(caught.exception.diag.constraint_name, CONSTRAINT)

    def _closed_row(self, model, **extra):
        return self.attachments.sudo().create(dict({
            'name': 'closed.txt', 'raw': b'closed', 'res_model': model, 'res_id': 0}, **extra))

    def test_a_statement_that_stores_an_open_row_of_a_module_model_fails(self):
        for model in WOOW_MODELS:
            row = self._closed_row(model)
            self.env.flush_all()
            for column, value in (('public', True), ('access_token', 'planted'),
                                  ('access_token', ' ')):
                with self.subTest(model=model, way='update', column=column, value=value):
                    self._refused('UPDATE ir_attachment SET %s = %%s WHERE id = %%s' % column,
                                  (value, row.id))
            with self.subTest(model=model, way='update', column='both'):
                self._refused('UPDATE ir_attachment SET public = TRUE, access_token = %s '
                              'WHERE id = %s', ('planted', row.id))
            for column, value in (('public', True), ('access_token', 'planted')):
                with self.subTest(model=model, way='insert', column=column):
                    self._refused(
                        "INSERT INTO ir_attachment (name, type, res_model, res_id, %s) "
                        "VALUES ('planted.txt', 'binary', %%s, 0, %%s)" % column, (model, value))
            # the row of another model is open, and is moved onto the module's model: refused
            open_row = self.attachments.sudo().create({
                'name': 'open.txt', 'raw': b'open', 'res_model': 'res.partner', 'res_id': 0,
                'public': True, 'access_token': 'open-token'})
            with self.subTest(model=model, way='a move of an open row'):
                self._refused('UPDATE ir_attachment SET res_model = %s WHERE id = %s',
                              (model, open_row.id))
            self.assertFalse(open_rows(self.env))

    def test_what_the_constraint_allows(self):
        """Closed rows of module models, and any row of another model, open or not."""
        for model in WOOW_MODELS:
            for public, token in ((False, None), (None, None), (False, ''), (None, '')):
                with self.subTest(model=model, public=public, token=token):
                    self.env.cr.execute(
                        "INSERT INTO ir_attachment (name, type, res_model, res_id, public, "
                        "access_token) VALUES ('closed.txt', 'binary', %s, 0, %s, %s)",
                        (model, public, token))
        for model in (None, '', 'res.partner', ' woow.document', 'WOOW.DOCUMENT', 'woow.documents'):
            with self.subTest(model=model):
                self.env.cr.execute(
                    "INSERT INTO ir_attachment (name, type, res_model, res_id, public, access_token) "
                    "VALUES ('open.txt', 'binary', %s, 0, TRUE, 'token')", (model,))

    def test_the_constraint_lists_the_models_of_the_guard_and_is_validated(self):
        """Built from ``WOOW_MODELS``, so that it cannot drift from the guard; checked on every row."""
        self.env.cr.execute(
            'SELECT convalidated, pg_get_constraintdef(oid) FROM pg_constraint '
            "WHERE conrelid = 'ir_attachment'::regclass AND conname = %s", (CONSTRAINT,))
        validated, definition = self.env.cr.fetchone()
        self.assertTrue(validated)
        listed = re.search(r'<> ALL \(\(ARRAY\[(.*?)\]\)::text\[\]\)', definition)
        self.assertTrue(listed, definition)
        self.assertEqual(set(re.findall(r"'([^']+)'::character varying", listed.group(1))),
                         set(WOOW_MODELS))
        self.assertIn('public', definition)
        self.assertIn('access_token', definition)

    def test_a_path_that_the_guard_does_not_know_fails_with_the_message_of_the_constraint(self):
        """A bug, or code that calls core's ``write`` or ``create``: the statement fails, it is not silent."""
        row = self._closed_row('woow.document')
        self.env.flush_all()
        with mute_logger('odoo.sql_db'), self.assertRaises(errors.CheckViolation), \
                self.cr.savepoint():
            super(IrAttachment, row).write({'public': True})
            self.env.flush_all()
        with mute_logger('odoo.sql_db'), self.assertRaises(errors.CheckViolation), \
                self.cr.savepoint():
            super(IrAttachment, self.attachments).create([{
                'name': 'bypass.txt', 'res_model': 'woow.document.folder', 'res_id': 0,
                'access_token': 'bypass-token'}])
            self.env.flush_all()
        self.env.invalidate_all()
        self.assertFalse(open_rows(self.env))
        constraint = self.env['ir.model.constraint'].search([('name', '=', CONSTRAINT)])
        self.assertEqual(constraint.message, 'Files of documents and folders cannot be public or '
                                             'carry an access token.')


# The shapes of the five values that a call can carry: the ones that store as what they look like and the
# ones that do not (another type with the same text, text that is nearly a model, a flag that is not a bool).
def _shapes(document_id, folder_id, partner):
    models = [False, None, '', 'res.partner', 'mail.compose.message', ' woow.document',
              'WOOW.DOCUMENT', ['woow.document'], 0, 'woow.document.log']
    for model in ('woow.document', 'woow.document.folder', 'woow.document.tag'):
        models += spellings(model) * 2        # the dangerous ones weigh more
    return {
        'res_model': models,
        'res_id': [False, None, 0, document_id, folder_id, str(document_id), float(document_id),
                   [document_id], 10 ** 9],
        'res_field': [False, None, '', 'datas', 0, [], b'datas', xmlrpc.client.Binary(b'datas'),
                      ' ', partner],
        'public': [False, None, 0, True, True, 1, 'yes', [0], 0.5, ''],
        'access_token': [False, None, '', 'token', 0, [], b'token', xmlrpc.client.Binary(b'token'),
                         ' ', 'false', partner],
    }


@tagged('woow_docmgmt_core')
class TestNoShapeOfTheValuesLeavesAnOpenRow(_Fixture, TransactionCase):
    """Property-style: whatever a call carries, no row of a module model is public or tokenised.

    Each walk is a few calls (a create, or a write of a row made before) with values taken at random
    from the shapes above, made for a plain internal user, for an editor of the document and for the
    superuser. After every call the rows of the module's models are read around the ORM. A call may be
    refused, or the values may be normalised, or the database may fail the statement: all of them keep
    the files closed. A row that is stored open fails the test. The walks are made with the
    constraint of the database and without it, so that the guard of the ORM is held on its own too.
    """

    WALKS = 50
    STEPS = 4

    def setUp(self):
        super().setUp()
        self._setup_rows()
        self.partner = self.env['res.partner'].sudo().create({'name': 'Walks partner'})
        self.shapes = _shapes(self.document.id, self.folder.id, self.partner)
        self.callers = {'a plain internal user': self.plain, 'an editor of the document': self.editor,
                        'the superuser': self.env.ref('base.user_root')}

    def _walk(self, caller, rng):
        model = self.attachments.sudo() if caller.id == 1 else self.attachments.with_user(caller)
        rows = model.browse()
        for _step in range(self.STEPS):
            keys = rng.sample(sorted(self.shapes), rng.randint(1, len(self.shapes)))
            values = {key: rng.choice(self.shapes[key]) for key in keys}
            try:
                with mute_logger('odoo.sql_db'), self.cr.savepoint():
                    if not rows or rng.random() < 0.4:
                        created = model.create(dict({'name': 'walk.txt', 'raw': b'walk bytes'}, **values))
                        self.env.flush_all()
                        rows |= created
                    else:
                        rng.choice(rows).write(values)
                        self.env.flush_all()
            except (UserError, DatabaseError, ValueError, TypeError, KeyError):
                # refused: nothing of the call is kept (the savepoint rolled it back)
                self.env.invalidate_all()
                rows = rows.exists()
            self.assertFalse(open_rows(self.env), (caller.login, values))

    def _walks(self):
        for name, caller in self.callers.items():
            rng = random.Random('%s walks' % name)
            for walk in range(self.WALKS):
                with self.subTest(caller=name, walk=walk):
                    self._walk(caller, rng)

    def test_no_shape_of_the_values_leaves_an_open_row_with_the_constraint(self):
        self._walks()

    def test_no_shape_of_the_values_leaves_an_open_row_for_the_guard_alone(self):
        """The same walks with the constraint of the database dropped: the guard keeps the rows closed."""
        self._allow_open_rows()
        self._walks()


@tagged('woow_docmgmt_http')
class TestAnXmlRpcCallThatCarriesTheModelAsBase64(_Fixture, HttpCase):
    """The only transport that delivers a ``Binary``: ``/xmlrpc/2/object``, as a script of a user does."""

    def setUp(self):
        super().setUp()
        self._setup_rows()

    def _call(self, user, method, *args):
        """``ir.attachment.<method>`` over XML-RPC as ``user`` (the password of a test user is the login)."""
        return self.xmlrpc_object.execute_kw(
            get_db_name(), user.id, user.login, 'ir.attachment', method, list(args))

    @staticmethod
    def _datas(content):
        return base64.b64encode(content).decode()

    def test_a_file_that_a_user_with_no_right_on_the_document_made_public_is_not_hung_on_it(self):
        """The reproduced attack: create a public file, name the record, then the model as base64.

        The second call leaves a row that is public and names a record but no model (a file that is
        attached to nothing, which core serves as it serves any public file). The third call is refused
        as an access error, and no row of a module model is open.
        """
        self.assertFalse(self.document.with_user(self.plain).has_access('read'))
        for model, res_id in (('woow.document', self.document.id),
                              ('woow.document.folder', self.folder.id)):
            with self.subTest(model=model):
                created = self._call(self.plain, 'create', {
                    'name': 'hung.txt', 'public': True, 'datas': self._datas(b'attacker bytes')})
                self.assertTrue(self._call(self.plain, 'write', [created], {'res_id': res_id}))
                with self.assertRaises(xmlrpc.client.Fault) as caught:
                    self._call(self.plain, 'write', [created],
                               {'res_model': xmlrpc.client.Binary(model.encode())})
                self.assertEqual(caught.exception.faultCode, ACCESS_ERROR)
                self.assertIn('cannot be reassigned', caught.exception.faultString)
                row = self.env['ir.attachment'].sudo().browse(created)
                self.assertEqual(row_of(self.env, row)[:3], (None, res_id, True))
                self.assertFalse(open_rows(self.env))
                # nothing of it is a file of the record: the record lists no such attachment
                self.assertFalse(self.env['ir.attachment'].sudo().search([
                    ('res_model', '=', model), ('res_id', '=', res_id), ('id', '=', created)]))

    def test_the_file_that_a_user_attaches_with_the_right_to_write_the_record_is_not_served_to_a_visitor(self):
        """The control: an editor attaches a closed file over XML-RPC, and nobody can open it again."""
        for model, res_id in (('woow.document', self.document.id),
                              ('woow.document.folder', self.folder.id)):
            with self.subTest(model=model):
                created = self._call(self.editor, 'create', {
                    'name': 'attached.txt', 'datas': self._datas(b'attached bytes'),
                    'res_model': model, 'res_id': res_id})
                row = self.env['ir.attachment'].sudo().browse(created)
                self.assertEqual(row_of(self.env, row)[:4], (model, res_id, False, None))
                # it is a module row now: neither the flag nor a model of another type changes that
                for values in ({'public': True}, {'access_token': 'late-token'},
                               {'res_model': xmlrpc.client.Binary(b'woow.document.log')}):
                    with self.assertRaises(xmlrpc.client.Fault) as caught:
                        self._call(self.editor, 'write', [created], values)
                    self.assertEqual(caught.exception.faultCode, ACCESS_ERROR)
                self.assertEqual(row_of(self.env, row)[:4], (model, res_id, False, None))
                self.authenticate(None, None)
                self.assertEqual(self.url_open('/web/content/%d?download=1' % created,
                                               allow_redirects=False).status_code, 404)
                # the editor, who may read the record, reads the file
                self.authenticate(self.editor.login, self.editor.login)
                response = self.url_open('/web/content/%d?download=1' % created,
                                         allow_redirects=False)
                self.assertEqual((response.status_code, response.content), (200, b'attached bytes'))
