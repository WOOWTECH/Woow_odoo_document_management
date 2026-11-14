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
            content: "the picture of an image card is a small copy from the core image route",
            trigger: ".o_kanban_record:contains('tour-picture.png') img[loading='lazy'][src^='/web/image/woow.document/']",
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
            content: "the Rename entry opens its wizard (an action the web client can show)",
            trigger: ".woow-selection-bar .o_woow_actions",
            run: "click",
        },
        {
            content: "pick Rename",
            trigger: ".o_woow_actions_menu .o_woow_action_rename",
            run: "click",
        },
        {
            content: "the wizard is a dialog with a form, not an error",
            trigger: ".modal .o_form_view",
        },
        {
            content: "close the wizard",
            trigger: ".modal .modal-header .btn-close",
            run: "click",
        },
        {
            content: "the Create shortcut entry opens its wizard too",
            trigger: ".woow-selection-bar .o_woow_actions",
            run: "click",
        },
        {
            content: "pick Create shortcut",
            trigger: ".o_woow_actions_menu .o_woow_action_shortcut",
            run: "click",
        },
        {
            content: "the shortcut wizard is a dialog with a form",
            trigger: ".modal .o_form_view",
        },
        {
            content: "close the shortcut wizard",
            trigger: ".modal .modal-header .btn-close",
            run: "click",
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
            run: "click",
        },
        {
            content: "back to the cards",
            trigger: ".o_cp_switch_buttons .o_switch_view.o_kanban",
            run: "click",
        },
        {
            content: "a double click on a shortcut opens the file it points to",
            trigger: ".o_kanban_record:has(.woow-kind-icon.fa-share-square-o)",
            run: "dblclick",
        },
        {
            content: "the target is shown as a form",
            trigger: ".o_form_view .o_field_widget[name='name'] input:value('tour-target.txt')",
        },
        {
            content: "go back to the screen",
            trigger: ".o_control_panel .breadcrumb-item:first",
            run: "click",
        },
        {
            content: "a double click on a requested file opens the dialog to send it",
            trigger: ".o_kanban_record:has(.woow-kind-icon.fa-inbox)",
            run: "dblclick",
        },
        {
            content: "the upload dialog is shown",
            trigger: ".modal .o_form_view",
        },
        {
            content: "close the dialog",
            trigger: ".modal .modal-header .btn-close",
            run: "click",
        },
    ],
});
