# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Searches, groupings and orderings that must not reach past the record rules."""
import base64
import json
import secrets

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, tagged


def _portal_user(env, login, password=None):
    partner = env['res.partner'].sudo().create({
        'name': login, 'email': '%s@example.test' % login})
    vals = {'name': login, 'login': login, 'partner_id': partner.id,
            'groups_id': [Command.set([env.ref('base.group_portal').id])]}
    if password:
        vals['password'] = password
    return env['res.users'].sudo().create(vals)


def _document_user(env, login):
    return env['res.users'].sudo().create({
        'name': login, 'login': login, 'email': '%s@example.test' % login,
        'groups_id': [Command.set([
            env.ref('woow_document_management.group_woow_document_user').id])]})


def _membership_fixture(env, suffix, portal_password=None):
    """A portal reader A of one folder that also lists another customer B, a member,
    a group and a tag; a document of it is shared with a third customer C."""
    admin = env.ref('base.user_admin')
    reader = _portal_user(env, 'wdoc-groups-a-%s' % suffix, portal_password)
    other = _portal_user(env, 'wdoc-groups-b-%s' % suffix)
    third = _portal_user(env, 'wdoc-groups-c-%s' % suffix)
    member = _document_user(env, 'wdoc-groups-member-%s' % suffix)
    group = env['res.groups'].sudo().create({'name': 'Folder group %s' % suffix})
    tag = env['woow.document.tag'].sudo().create({'name': 'Tag %s' % suffix})
    folder = env['woow.document.folder'].sudo().create({
        'name': 'Membership folder %s' % suffix, 'owner_id': admin.id,
        'tag_ids': [Command.set([tag.id])]})
    access = env['woow.document.folder.access'].sudo()
    access.create([
        {'folder_id': folder.id, 'subject_type': 'partner',
         'partner_id': reader.partner_id.id, 'level': 'read'},
        {'folder_id': folder.id, 'subject_type': 'partner',
         'partner_id': other.partner_id.id, 'level': 'read'},
        {'folder_id': folder.id, 'subject_type': 'user', 'user_id': member.id, 'level': 'read'},
        {'folder_id': folder.id, 'subject_type': 'group', 'group_id': group.id,
         'level': 'read'}])
    document = env['woow.document'].sudo().create({
        'name': 'membership.txt', 'folder_id': folder.id,
        'datas': base64.b64encode(b'membership fixture')})
    document.sudo().write({'shared_partner_ids': [Command.set([third.partner_id.id])],
                           'shared_user_ids': [Command.set([member.id])],
                           'tag_ids': [Command.set([tag.id])]})
    env.flush_all()
    return reader, other, third, member, group, tag, folder, document


@tagged('woow_docmgmt_core')
class TestFieldGroupsInDomains(TransactionCase):
    """A field a user may not read can be neither searched nor grouped on (HD-02)."""

    def test_portal_cannot_probe_member_lists_through_domains_or_groupings(self):
        reader, other, third, member, group, tag, folder, document = _membership_fixture(
            self.env, 'orm')
        folders = self.env['woow.document.folder'].with_user(reader)
        documents = self.env['woow.document'].with_user(reader)
        self.assertEqual(folders.search_count([('id', '=', folder.id)]), 1)
        self.assertEqual(documents.search_count([('id', '=', document.id)]), 1)
        probes = (
            (folders, [('eff_read_partner_ids', 'in', [other.partner_id.id])]),
            (folders, [('eff_upload_partner_ids', 'in', [other.partner_id.id])]),
            (folders, [('eff_read_user_ids', 'in', [member.id])]),
            (folders, [('eff_read_group_ids', 'in', [group.id])]),
            (folders, [('access_ids.partner_id', '=', other.partner_id.id)]),
            (folders, [('tag_ids', 'in', [tag.id])]),
            (documents, [('shared_partner_ids', 'in', [third.partner_id.id])]),
            (documents, [('shared_user_ids', 'in', [member.id])]),
            (documents, [('tag_ids', 'in', [tag.id])]),
            (documents, [('folder_id.eff_read_partner_ids', 'in', [other.partner_id.id])]),
            (documents, [('folder_id', 'any', [('eff_read_user_ids', 'in', [member.id])])]),
        )
        for model, domain in probes:
            with self.subTest(model=model._name, domain=domain):
                with self.assertRaises(AccessError):
                    model.search_count(domain)
        for model, groupby in ((folders, 'eff_read_group_ids'), (folders, 'tag_ids'),
                               (documents, 'tag_ids'), (documents, 'shared_partner_ids')):
            with self.subTest(model=model._name, groupby=groupby), self.assertRaises(AccessError):
                model.read_group([], [groupby], [groupby])

    def test_internal_members_portal_pages_and_rules_keep_working(self):
        reader, other, third, member, group, tag, folder, document = _membership_fixture(
            self.env, 'ok')
        folders = self.env['woow.document.folder'].with_user(member)
        self.assertEqual(folders.search_count([
            ('eff_read_partner_ids', 'in', [other.partner_id.id])]), 1)
        self.assertEqual(folders.read_group([('id', '=', folder.id)], ['tag_ids'],
                                            ['tag_ids'])[0]['tag_ids'][0], tag.id)
        documents = self.env['woow.document'].with_user(member)
        self.assertEqual(documents.search_count([('shared_user_ids', 'in', [member.id])]), 1)
        direct = self.env['woow.document'].with_user(third)._woow_portal_direct_documents()
        self.assertEqual(direct, document)
        self.assertFalse(self.env['woow.document'].with_user(other)._woow_portal_direct_documents())
        self.assertEqual(self.env['woow.document'].with_user(reader).search(
            [('folder_id', '=', folder.id)]), document)


@tagged('woow_docmgmt_core')
class TestFieldGroupsInDomainsRpc(HttpCase):

    def _call_kw(self, model, method, args, kwargs=None):
        payload = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'id': 'groups',
                              'params': {'model': model, 'method': method,
                                         'args': args, 'kwargs': kwargs or {}}})
        response = self.url_open('/web/dataset/call_kw/%s/%s' % (model, method),
                                 data=payload, headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_portal_rpc_cannot_probe_member_lists(self):
        password = secrets.token_urlsafe(16)
        reader, other, third, member, group, tag, folder, document = _membership_fixture(
            self.env, 'rpc', portal_password=password)
        self.authenticate(reader.login, password)
        for model, domain in (
                ('woow.document.folder', [['eff_read_partner_ids', 'in', [other.partner_id.id]]]),
                ('woow.document', [['shared_partner_ids', 'in', [third.partner_id.id]]])):
            result = self._call_kw(model, 'search_count', [domain])
            self.assertIn('error', result)
            self.assertEqual(result['error']['data']['name'], 'odoo.exceptions.AccessError')
        grouped = self._call_kw('woow.document', 'web_read_group', [[], ['tag_ids'], ['tag_ids']])
        self.assertIn('error', grouped)
        self.assertNotIn(tag.name, json.dumps(grouped))
        visible = self._call_kw('woow.document', 'search_count', [[['id', '=', document.id]]])
        self.assertEqual(visible['result'], 1)


@tagged('woow_docmgmt_core')
class TestFolderOrderingIsNotAnOracle(TransactionCase):
    """Sorting or grouping by a folder never compares names a reader cannot see."""

    def test_order_and_groups_by_folder_ignore_folder_names_and_sequences(self):
        admin = self.env.ref('base.user_admin')
        member = _document_user(self.env, 'wdoc-order-member')
        portal = _portal_user(self.env, 'wdoc-order-portal')
        folders = self.env['woow.document.folder'].sudo()
        hidden = folders.create({'name': 'Mmm order hidden', 'owner_id': admin.id})
        child = folders.create({'name': 'Child of hidden', 'parent_id': hidden.id,
                                'inherit_access': False, 'owner_id': admin.id})
        work = folders.create({'name': 'Work order probe', 'owner_id': admin.id})
        sub = folders.create({'name': 'Sub of work', 'parent_id': work.id})
        access = self.env['woow.document.folder.access'].sudo()
        access.create([
            {'folder_id': child.id, 'subject_type': 'user', 'user_id': member.id,
             'level': 'read'},
            {'folder_id': work.id, 'subject_type': 'user', 'user_id': member.id, 'level': 'edit'},
            {'folder_id': work.id, 'subject_type': 'partner', 'partner_id': portal.partner_id.id,
             'level': 'read'}])
        documents = self.env['woow.document'].sudo()
        shared = documents.create({'name': 'shared.txt', 'folder_id': hidden.id,
                                   'datas': base64.b64encode(b'shared')})
        shared.write({'shared_user_ids': [Command.set([member.id])],
                      'shared_partner_ids': [Command.set([portal.partner_id.id])]})
        local = documents.create({'name': 'local.txt', 'folder_id': work.id,
                                  'datas': base64.b64encode(b'local')})
        self.env.flush_all()
        member_folders = self.env['woow.document.folder'].with_user(member)
        self.assertEqual(member_folders.search_count([('id', '=', hidden.id)]), 0)
        pair, docs = [child.id, sub.id], [shared.id, local.id]

        def orders():
            member_docs = self.env['woow.document'].with_user(member)
            return (
                [row['id'] for row in member_folders.search_read(
                    [('id', 'in', pair)], ['id'], order='parent_id')],
                [row['id'] for row in member_folders.web_search_read(
                    [('id', 'in', pair)], {'id': {}}, order='parent_id desc')['records']],
                member_docs.search([('id', 'in', docs)], order='folder_id').ids,
                [group['folder_id'][0] for group in member_docs.read_group(
                    [('id', 'in', docs)], ['folder_id'], ['folder_id'])],
                self.env['woow.document'].with_user(portal).search(
                    [('id', 'in', docs)], order='folder_id desc').ids,
            )

        seen = set()
        own_orders = set()
        for name, sequence in (('Aaa order probe', 10), ('Zzz order probe', 10),
                               ('Work order probe', 5), ('Work order probe', 15)):
            work.with_user(member).write({'name': name, 'sequence': sequence})
            self.env.flush_all()
            seen.add(repr(orders()))
            own_orders.add(tuple(member_folders.search([('id', 'in', [work.id, child.id])]).ids))
        self.assertEqual(len(seen), 1, seen)
        # A folder list itself still follows sequence and name.
        self.assertEqual(len(own_orders), 2)
        # Document administrators read every folder and keep the named order.
        manager_docs = self.env['woow.document'].with_user(admin)
        work.sudo().write({'name': 'Aaa order probe', 'sequence': 10})
        first = manager_docs.search([('id', 'in', docs)], order='folder_id').ids
        work.sudo().write({'name': 'Zzz order probe'})
        self.assertNotEqual(manager_docs.search([('id', 'in', docs)], order='folder_id').ids,
                            first)
