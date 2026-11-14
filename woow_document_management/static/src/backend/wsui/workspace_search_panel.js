// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// The left panel of the workspace: the standard search panel with a "Trash" entry at the bottom.
import { onWillStart, useState } from "@odoo/owl";
import { SearchPanel } from "@web/search/search_panel/search_panel";
import { useBus, useService } from "@web/core/utils/hooks";
import { FOLDER_MODEL } from "./workspace_utils";

export const TRASH_ACTION = "woow_document_management.action_woow_document_trash_documents";

export class WooSearchPanel extends SearchPanel {
    static template = "woow_document_management.SearchPanel";
    static subTemplates = {
        ...SearchPanel.subTemplates,
        category: "woow_document_management.SearchPanel.Category",
    };

    setup() {
        super.setup();
        this.actionService = useService("action");
        this.orm = useService("orm");
        // The panel gets the display name of a folder, which holds the whole path; the tree
        // shows the plain name of each folder, read as the user.
        this.shortNames = useState({});
        onWillStart(() => this.loadShortNames());
        useBus(this.env.searchModel, "update", () => this.loadShortNames());
    }

    async loadShortNames() {
        await this.env.searchModel.sectionsPromise;
        const [category] = this.env.searchModel.getSections(
            (section) => section.type === "category" && section.fieldName === "folder_id"
        );
        if (!category) {
            return;
        }
        const ids = [...category.values.keys()].filter((id) => id && !(id in this.shortNames));
        if (!ids.length) {
            return;
        }
        const rows = await this.orm.read(FOLDER_MODEL, ids, ["name"]);
        for (const row of rows) {
            this.shortNames[row.id] = row.name;
        }
    }

    /** The name a value of the panel shows: the plain name of a folder, else the panel's own. */
    shortName(section, value) {
        if (section.fieldName === "folder_id" && value.id in this.shortNames) {
            return this.shortNames[value.id];
        }
        return value.display_name;
    }

    /** Keeps the chosen folder in sight: the folders above it are opened in the tree. */
    updateActiveValues() {
        super.updateActiveValues();
        const categories = this.env.searchModel.getSections((s) => s.type === "category");
        for (const category of categories) {
            if (!category.activeValueId || !category.values.has(category.activeValueId)) {
                continue;
            }
            const expanded = (this.state.expanded[category.id] ||= {});
            for (const ancestorId of this.getAncestorValueIds(category, category.activeValueId)) {
                expanded[ancestorId] = true;
            }
        }
    }

    // A card of the files may be dropped on a folder of the tree to move it there.
    onFolderDragOver(ev, section, value) {
        const ws = this.env.wooWorkspace;
        if (!ws || !ws.isDragging || section.fieldName !== "folder_id" || !value.id) {
            return;
        }
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        ws.state.dropFolderId = value.id;
    }

    onFolderDragLeave(section, value) {
        const ws = this.env.wooWorkspace;
        if (ws && ws.state.dropFolderId === value.id) {
            ws.state.dropFolderId = false;
        }
    }

    onFolderDrop(ev, section, value) {
        const ws = this.env.wooWorkspace;
        if (!ws || !ws.isDragging || section.fieldName !== "folder_id" || !value.id) {
            return;
        }
        ev.preventDefault();
        return ws.dropOnFolder(value.id);
    }

    isDropTarget(section, value) {
        const ws = this.env.wooWorkspace;
        return Boolean(ws) && section.fieldName === "folder_id" && ws.state.dropFolderId === value.id;
    }

    openTrash() {
        return this.actionService.doAction(TRASH_ACTION);
    }
}
