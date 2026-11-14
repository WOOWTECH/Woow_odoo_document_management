// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// The workspace list: the rows keep their standard behaviour (a click opens the form, the check
// box selects), and the strip of folders, the selection bar (with Open for the viewer) and the
// dropped files work as in the kanban.
import { registry } from "@web/core/registry";
import { listView } from "@web/views/list/list_view";
import { ListController } from "@web/views/list/list_controller";
import { ListRenderer } from "@web/views/list/list_renderer";
import { useDropZone } from "./drop_zone";
import { WooSearchPanel } from "./workspace_search_panel";
import { displayedRecords, useWorkspace } from "./workspace";
import {
    WooFolderStrip,
    WooInspectorPane,
    WooSelectionBar,
    WooUploadStatus,
} from "./workspace_components";

export class WorkspaceListRenderer extends ListRenderer {
    static template = "woow_document_management.WorkspaceListRenderer";
    static components = { ...ListRenderer.components, WooFolderStrip, WooUploadStatus };
}

export class WorkspaceListController extends ListController {
    static template = "woow_document_management.WorkspaceListView";
    static components = {
        ...ListController.components,
        WooSelectionBar,
        WooInspectorPane,
    };

    setup() {
        super.setup();
        this.ws = useWorkspace({
            searchModel: this.env.searchModel,
            adapter: {
                records: () => displayedRecords(this.model.root),
                selectedIds: () =>
                    displayedRecords(this.model.root)
                        .filter((record) => record.selected)
                        .map((record) => record.resId),
                setSelection: (ids) => {
                    for (const record of displayedRecords(this.model.root)) {
                        const wanted = ids.includes(record.resId);
                        if (record.selected !== wanted) {
                            record.toggleSelection(wanted);
                        }
                    }
                },
                reload: () => this.model.load(),
            },
        });
        useDropZone(this.ws, () => this.rootRef.el && this.rootRef.el.querySelector(".o_content"));
    }
}

export const workspaceListView = {
    ...listView,
    Controller: WorkspaceListController,
    Renderer: WorkspaceListRenderer,
    SearchPanel: WooSearchPanel,
};

registry.category("views").add("woow_workspace_list", workspaceListView);
