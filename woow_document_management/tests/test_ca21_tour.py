# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Browser tour for the Odoo FileViewer custom source assumption."""

import base64

from odoo.tests import HttpCase, tagged


@tagged('-at_install', 'post_install', 'woow_docmgmt_core_assumptions')
class TestCoreAssumptionsFileViewerTour(HttpCase):

    def test_ca21_fileviewer_custom_url_tour(self):
        """T-CA-21: backend FileViewer renders the document preview route."""
        folder = self.env['woow.document.folder'].sudo().create({'name': 'CA21 tour folder'})
        document = self.env['woow.document'].sudo().create({
            'name': 'ca21.txt', 'folder_id': folder.id,
            'datas': base64.b64encode(b'ca21-preview-body'),
        })
        self.start_tour(
            '/odoo', 'tour_ca21_fileviewer_custom_url', login='admin',
            code=(
                'window.__ca21DocId = %d; '
                "odoo.startTour('tour_ca21_fileviewer_custom_url', {startUrl: '/odoo'});"
            ) % document.id,
        )
