// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// The main flow of the workspace screen: choose a folder from its card, select a card, act on it,
// open it in the viewer, and look at the same files as rows.
import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("tour_woow_workspace_screen", {
    steps: () => [
        {
            content: "the top folders are cards above the files",
            trigger: ".woow-folder-card:contains('Tour projects')",
            run: "click",
        },
        {
            content: "the path shows the folder that was chosen",
            trigger: ".woow-breadcrumb .woow-crumb:contains('Tour projects')",
        },
        {
            content: "its sub-folder is a card too",
            trigger: ".woow-folder-card:contains('Tour sub folder')",
        },
        {
            content: "the picture of an image card comes from the preview route",
            trigger: ".o_kanban_record:contains('tour-picture.png') img[loading='lazy'][src^='/woow_document/doc/']",
        },
        {
            content: "one click selects a card",
            trigger: ".o_kanban_record:contains('tour-notes.txt')",
            run: "click",
        },
        {
            content: "the control panel shows the number selected",
            trigger: ".woow-selection-bar .o_woow_selected_count:contains('1 selected')",
        },
        {
            content: "and the download of the file",
            trigger: ".woow-selection-bar .o_woow_download",
        },
        {
            content: "open the Actions menu",
            trigger: ".woow-selection-bar .o_woow_actions",
            run: "click",
        },
        {
            content: "the menu offers the actions that apply",
            trigger: ".o_woow_actions_menu .o_woow_action_rename",
        },
        {
            content: "star the file from the menu",
            trigger: ".o_woow_actions_menu .o_woow_action_star",
            run: "click",
        },
        {
            content: "the star is on",
            trigger: ".o_kanban_record:contains('tour-notes.txt') .fa-star.text-warning",
        },
        {
            content: "a double click opens the file in the viewer",
            trigger: ".o_kanban_record:contains('tour-picture.png')",
            run: "dblclick",
        },
        {
            content: "the viewer shows the image",
            trigger: ".o-FileViewer .o-FileViewer-viewImage",
        },
        {
            content: "close the viewer",
            trigger: ".o-FileViewer",
            run: "press Escape",
        },
        {
            content: "the left panel ends with the Trash entry",
            trigger: ".o_search_panel .woow-trash-entry",
        },
        {
            content: "switch to rows",
            trigger: ".o_cp_switch_buttons .o_switch_view.o_list",
            run: "click",
        },
        {
            content: "the rows show the folder strip as well",
            trigger: ".o_list_view .woow-strip .woow-crumb:contains('Tour projects')",
        },
        {
            content: "the check box of a row selects it",
            trigger: ".o_list_view .o_data_row:contains('tour-notes.txt') .o_list_record_selector input",
            run: "click",
        },
        {
            content: "the same selection bar is there",
            trigger: ".o_list_view .woow-selection-bar .o_woow_actions",
        },
        {
            content: "go back to the folders",
            trigger: ".woow-breadcrumb .woow-crumb:contains('All folders')",
            run: "click",
        },
        {
            content: "the folder card is shown again",
            trigger: ".woow-folder-card:contains('Tour projects')",
        },
    ],
});
