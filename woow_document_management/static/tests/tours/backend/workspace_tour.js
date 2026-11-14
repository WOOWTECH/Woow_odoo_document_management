// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// The workspace screen: cards, rows, buttons without a selection, the star and the search panel.
import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("tour_woow_workspace", {
    steps: () => [
        {
            content: "the card of the document is shown",
            trigger: ".o_kanban_view .o_kanban_record:contains('tour-workspace.txt')",
        },
        {
            content: "the folder tree of the search panel is shown",
            trigger: ".o_search_panel .o_search_panel_category_value:contains('Tour folder')",
        },
        {
            content: "the buttons are there without any selection",
            trigger: ".o_control_panel button:contains('New link')",
        },
        {
            trigger: ".o_control_panel button:contains('New folder')",
        },
        {
            trigger: ".o_control_panel button:contains('Trash')",
        },
        {
            content: "star the document",
            trigger: ".o_kanban_record:contains('tour-workspace.txt') button[name='action_toggle_favorite']",
            run: "click",
        },
        {
            content: "the star is on",
            trigger: ".o_kanban_record:contains('tour-workspace.txt') .fa-star.text-warning",
        },
        {
            content: "open the upload wizard",
            trigger: ".o_control_panel button:contains('Upload')",
            run: "click",
        },
        {
            trigger: ".modal button[name='action_upload']",
        },
        {
            trigger: ".modal button:contains('Cancel')",
            run: "click",
        },
        {
            content: "only the starred ones",
            trigger: ".o_search_panel .o_search_panel_category_value:contains('Starred')",
            run: "click",
        },
        {
            content: "the starred document stays",
            trigger: ".o_kanban_record:contains('tour-workspace.txt')",
        },
        {
            content: "switch to rows",
            trigger: ".o_cp_switch_buttons .o_switch_view.o_list",
            run: "click",
        },
        {
            content: "the rows have the star too",
            trigger: ".o_list_view .o_data_row:contains('tour-workspace.txt') button[name='action_toggle_favorite']",
        },
        {
            content: "the buttons are there in the list too",
            trigger: ".o_control_panel button:contains('Upload')",
        },
        {
            content: "open the form",
            trigger: ".o_list_view .o_data_row:contains('tour-workspace.txt') td[name='name']",
            run: "click",
        },
        {
            content: "the form has the star button",
            trigger: ".o_form_view button[name='action_toggle_favorite']",
        },
    ],
});
