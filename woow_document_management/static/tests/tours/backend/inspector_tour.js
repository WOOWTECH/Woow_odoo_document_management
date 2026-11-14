// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// The details panel on a real record: its facts, the star, the lock, a tag, a note in the chatter of
// core `mail`, and the click on the folder that opens the workspace on it.
import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("tour_woow_inspector", {
    steps: () => [
        {
            content: "the panel shows the document",
            trigger: ".o_woow_inspector .o_woow_inspector_name:contains('inspector-tour.txt')",
        },
        {
            content: "the folder is shown",
            trigger: ".o_woow_inspector a.o_woow_inspector_folder:contains('Inspector tour folder')",
        },
        {
            content: "the chatter of the record is there",
            trigger: ".o_woow_inspector .o-mail-Chatter",
        },
        {
            content: "star the document",
            trigger: ".o_woow_inspector_star:contains('Star')",
            run: "click",
        },
        {
            content: "the star is on",
            trigger: ".o_woow_inspector_star:contains('Remove star')",
        },
        {
            content: "lock the document",
            trigger: ".o_woow_inspector_lock",
            run: "click",
        },
        {
            content: "the lock is shown",
            trigger: ".o_woow_inspector_lockstate:contains('Locked by you')",
        },
        {
            content: "unlock it again",
            trigger: ".o_woow_inspector_unlock",
            run: "click",
        },
        {
            trigger: ".o_woow_inspector_lock",
        },
        {
            content: "search a tag",
            trigger: ".o_woow_inspector_tag_input",
            run: "edit Tour",
        },
        {
            content: "choose it",
            trigger: ".o_woow_inspector_tag_choice:contains('Tour tag')",
            run: "click",
        },
        {
            content: "the tag is on the document",
            trigger: ".o_woow_inspector_tag:contains('Tour tag')",
        },
        {
            content: "write a note in the chatter",
            trigger: ".o_woow_inspector .o-mail-Chatter-logNote",
            run: "click",
        },
        {
            trigger: ".o_woow_inspector .o-mail-Composer-input",
            run: "edit a note written from the panel",
        },
        {
            trigger: ".o_woow_inspector .o-mail-Composer-send:not(:disabled)",
            run: "click",
        },
        {
            content: "the note is in the chatter",
            trigger: ".o_woow_inspector .o-mail-Message:contains('a note written from the panel')",
        },
        {
            content: "choose the folder",
            trigger: ".o_woow_inspector a.o_woow_inspector_folder",
            run: "click",
        },
        {
            content: "the workspace opens with the folder chosen",
            trigger: ".o_search_panel .o_search_panel_category_value .active:contains('Inspector tour folder')",
        },
        {
            content: "and shows the document",
            trigger: ".o_kanban_record:contains('inspector-tour.txt')",
        },
    ],
});
