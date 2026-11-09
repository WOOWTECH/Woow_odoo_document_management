# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Disposable Odoo shell probe: exec(open(this_file).read()) with G2_MODE set.

G2_MODE='baseline' forces both old checks to finish before either mail is queued.
G2_MODE='regression' holds the winner's transaction while the loser enters.
Run only in a disposable DB and drop that DB afterward.
"""
import threading
import time
from datetime import timedelta
from unittest.mock import patch

from odoo import api, fields
from odoo.exceptions import UserError

mode = G2_MODE
assert mode in ('baseline', 'regression')
admin = env.ref('base.user_admin')
env['ir.config_parameter'].sudo().set_param(
    'woow_document_management.share_link_base_url', 'https://docs.example.test')
folder = env['woow.document.folder'].sudo().create({
    'name': 'G2 mail race synthetic', 'owner_id': admin.id})
share, token = env['woow.document.share'].with_user(admin)._issue({
    'share_type': 'folder', 'folder_id': folder.id,
    'expire_at': fields.Datetime.now() + timedelta(days=1),
    'recipient_email': 'nobody@example.test'})
env['woow.document.log'].sudo().create([{
    'event': 'share_send', 'result': 'ok', 'channel': 'backend',
    'user_id': admin.id, 'share_id': share.id, 'company_id': env.company.id,
} for _ in range(99)])
env.cr.commit()

entered = threading.Barrier(2, timeout=20)
counted = threading.Barrier(2, timeout=20)
first_done = threading.Event()
results = {}
backend_pid = {}
log_type = type(env['woow.document.log'])
mail_type = type(env['mail.mail'])
original_sum = log_type._sum_count


def sum_with_barrier(self, domain):
    result = original_sum(self, domain)
    if mode == 'baseline' and ('event', '=', 'share_send') in domain:
        counted.wait()
    return result


def forbid_external_send(*args, **kwargs):
    raise AssertionError('external mail send was attempted')


def worker(number):
    try:
        with env.registry.cursor() as cr:
            local = api.Environment(cr, admin.id, {})
            cr.execute('SELECT 1')  # Both cursors acquire a pre-send RR snapshot.
            cr.execute('SELECT pg_backend_pid()')
            backend_pid[number] = cr.fetchone()[0]
            entered.wait()
            if mode == 'regression' and number == 2:
                assert first_done.wait(20)
            local['woow.document.share'].browse(share.id)._woow_send_link(token, '')
            if mode == 'regression' and number == 1:
                first_done.set()
                # Do not commit until PostgreSQL confirms the second cursor is
                # waiting on our uncommitted sender-row update.
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    with env.registry.cursor() as observer:
                        observer.execute(
                            'SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s',
                            (backend_pid[2],))
                        row = observer.fetchone()
                    if row and row[0] == 'Lock':
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError('second cursor never waited for sender row')
            cr.commit()
            results[number] = 'committed'
    except UserError:
        results[number] = 'limit_rejected'
    except Exception as exc:
        results[number] = type(exc).__name__
    finally:
        if number == 1:
            first_done.set()


with patch.object(log_type, '_sum_count', sum_with_barrier), \
        patch.object(mail_type, 'send', forbid_external_send):
    one = threading.Thread(target=worker, args=(1,))
    two = threading.Thread(target=worker, args=(2,))
    one.start()
    two.start()
    one.join(timeout=30)
    two.join(timeout=30)
    assert not one.is_alive() and not two.is_alive(), results

retry = 'not_run'
if mode == 'regression':
    # This is the fresh transaction an HTTP serialization retry would use.
    with env.registry.cursor() as cr:
        local = api.Environment(cr, admin.id, {})
        try:
            local['woow.document.share'].browse(share.id)._woow_send_link(token, '')
        except UserError:
            retry = 'limit_rejected'
        else:
            retry = 'unexpected_send'
        cr.rollback()

with env.registry.cursor() as cr:
    check = api.Environment(cr, admin.id, {})
    mail_count = check['mail.mail'].sudo().search_count([
        ('model', '=', 'woow.document.share'), ('res_id', '=', share.id)])
    send_count = check['woow.document.log'].sudo().search_count([
        ('event', '=', 'share_send'), ('result', '=', 'ok'),
         ('user_id', '=', admin.id), ('share_id', '=', share.id)])
print('G2_RESULT|mode=%s|results=%s|retry=%s|mail_mail=%s|share_send=%s' %
      (mode, results, retry, mail_count, send_count))
if mode == 'baseline':
    assert sorted(results.values()) == ['committed', 'committed'], results
    assert (mail_count, send_count) == (2, 101)
else:
    assert list(results.values()).count('committed') == 1, results
    assert retry == 'limit_rejected', retry
    assert (mail_count, send_count) == (1, 100)
