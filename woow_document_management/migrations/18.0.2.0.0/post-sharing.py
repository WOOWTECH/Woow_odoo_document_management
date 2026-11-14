# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Upgrade from 18.0.1: a viewer row, without an end date, for every person already shared with.

Release 18.0.1 kept the people a document is shared with in two lists only (``shared_user_ids``
and ``shared_partner_ids``). From 18.0.2 the rows of ``woow.document.share`` are the source of those
lists, so each entry gets its row. The lists are not changed: every entry stays where it was, and
the editor list starts empty. Safe to run again: a person who has a row is skipped.
"""


def migrate(cr, version):
    if not version:
        return
    cr.execute(  # sql-ok: Q63
        """
        INSERT INTO woow_document_share
            (document_id, folder_id, company_id, subject_type, user_id, role,
             create_uid, write_uid, create_date, write_date)
        SELECT rel.document_id, doc.folder_id, doc.company_id, 'user', rel.user_id, 'viewer',
               1, 1, now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC'
          FROM woow_document_shared_user_rel rel
          JOIN woow_document doc ON doc.id = rel.document_id
         WHERE NOT EXISTS (SELECT 1 FROM woow_document_share share
                            WHERE share.document_id = rel.document_id
                              AND share.user_id = rel.user_id)
    """)
    cr.execute(  # sql-ok: Q63
        """
        INSERT INTO woow_document_share
            (document_id, folder_id, company_id, subject_type, partner_id, role,
             create_uid, write_uid, create_date, write_date)
        SELECT rel.document_id, doc.folder_id, doc.company_id, 'partner', rel.partner_id, 'viewer',
               1, 1, now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC'
          FROM woow_document_shared_partner_rel rel
          JOIN woow_document doc ON doc.id = rel.document_id
         WHERE NOT EXISTS (SELECT 1 FROM woow_document_share share
                            WHERE share.document_id = rel.document_id
                              AND share.partner_id = rel.partner_id)
    """)
