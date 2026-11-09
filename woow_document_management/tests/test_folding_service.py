# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

from odoo.tests import TransactionCase, tagged

from ..services import folding


@tagged('woow_docmgmt_core')
class TestFoldingService(TransactionCase):

    def test_hidden_ancestors_fold_without_names(self):
        names = {1: 'Visible root', 4: 'Visible child'}
        self.assertEqual(folding.fold((1, 2, 3, 4), names),
                         [(1, 'Visible root'), (None, '…'), (4, 'Visible child')])
        self.assertEqual(folding.breadcrumb((1, 2, 3, 4), names),
                         folding.fold((1, 2, 3, 4), names))
        self.assertEqual(folding.display_name((1, 2, 3, 4), names, 'Restricted'),
                         'Visible root / … / Visible child')
        self.assertEqual(folding.display_name((1, 2, 3), names, 'Restricted'),
                         'Restricted')

    def test_readable_roots_and_nearest_children(self):
        paths = {1: (), 4: (1, 2, 3), 5: (1, 2, 3, 4), 8: (6, 7)}
        self.assertEqual(folding.roots(paths), [1, 8])
        self.assertEqual(folding.children_of(1, paths), [4])
        self.assertEqual(folding.children_of(4, paths), [5])

    def test_parent_path_including_self_is_not_treated_as_ancestor(self):
        paths = {1: folding.parse_parent_path('1/'),
                 4: folding.parse_parent_path('1/2/3/4/'),
                 5: folding.parse_parent_path('1/2/3/4/5/')}
        self.assertEqual(folding.roots(paths), [1])
        self.assertEqual(folding.children_of(1, paths), [4])
        self.assertEqual(folding.children_of(4, paths), [5])

    def test_zip_path_skips_hidden_levels_and_stays_under_root(self):
        names = {1: 'Other', 3: 'Root', 5: 'File folder'}
        self.assertEqual(folding.zip_segments(3, (1, 2, 3, 4, 5), names),
                         ('Root', 'File folder'))
        self.assertEqual(folding.zip_segments(9, (1, 2, 3, 4, 5), names), ())
