// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// The workspace kanban: cards that are selected with one click and opened with two, a strip of
// folders above them and files dropped in to upload.
import { useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { kanbanView } from "@web/views/kanban/kanban_view";
import { KanbanController } from "@web/views/kanban/kanban_controller";
import { KanbanRenderer } from "@web/views/kanban/kanban_renderer";
import { KanbanRecord } from "@web/views/kanban/kanban_record";
import { useDropZone } from "./drop_zone";
import { WooSearchPanel } from "./workspace_search_panel";
import { DRAG_TEXT, DRAG_TYPE, displayedRecords, useWorkspace } from "./workspace";
import {
    WooFolderStrip,
    WooInspectorPane,
    WooSelectionBar,
    WooUploadStatus,
} from "./workspace_components";
import { documentKind, hasPicture, kindIcon, thumbnailUrl, toItem } from "./workspace_utils";

// What a click on these does not select the card: they have their own behaviour.
const OWN_CLICK = "a, button, input, textarea, select, [data-bs-toggle], .o_dropdown_kanban";

export class WorkspaceKanbanRecord extends KanbanRecord {
    static template = "woow_document_management.WorkspaceKanbanRecord";

    setup() {
        super.setup();
        this.ws = this.env.wooWorkspace;
        this.wooState = useState(this.ws.state);
    }

    get isSelected() {
        return this.wooState.selected.includes(this.props.record.resId);
    }

    /** The card template reads the kind of file, its icon and its picture from `woo`. */
    get renderingContext() {
        const context = super.renderingContext;
        const item = toItem(this.props.record);
        const kind = documentKind(item);
        context.woo = {
            kind,
            icon: kindIcon(kind),
            pictureUrl: hasPicture(item) ? thumbnailUrl(item.id, item.writeStamp) : false,
        };
        return context;
    }

    getRecordClasses() {
        const classes = super.getRecordClasses();
        return this.isSelected ? `${classes} woow-selected` : classes;
    }

    onGlobalClick(ev) {
        if (ev.target.closest(OWN_CLICK)) {
            return;
        }
        this.ws.clickItem(this.props.record.resId, {
            ctrl: ev.ctrlKey || ev.metaKey,
            shift: ev.shiftKey,
        });
    }

    onDoubleClick(ev) {
        if (ev.target.closest(OWN_CLICK)) {
            return;
        }
        return this.ws.open(toItem(this.props.record));
    }

    onDragStart(ev) {
        if (ev.target !== ev.currentTarget || !this.ws.startDrag(this.props.record.resId)) {
            ev.preventDefault();
            return;
        }
        ev.dataTransfer.setData(DRAG_TYPE, DRAG_TEXT);
        ev.dataTransfer.effectAllowed = "move";
    }

    onDragEnd() {
        this.ws.endDrag();
    }

    onKeydown(ev) {
        if (ev.target !== ev.currentTarget) {
            return;
        }
        if (ev.key === "Enter") {
            ev.preventDefault();
            return this.ws.open(toItem(this.props.record));
        }
        if (ev.key === " ") {
            ev.preventDefault();
            this.ws.clickItem(this.props.record.resId, {
                ctrl: ev.ctrlKey || ev.metaKey,
                shift: ev.shiftKey,
            });
        }
    }
}

export class WorkspaceKanbanRenderer extends KanbanRenderer {
    static template = "woow_document_management.WorkspaceKanbanRenderer";
    static components = {
        ...KanbanRenderer.components,
        KanbanRecord: WorkspaceKanbanRecord,
        WooFolderStrip,
        WooUploadStatus,
    };

    onBackgroundClick(ev) {
        if (!ev.target.closest(".o_kanban_record, .woow-strip, .woow-upload-status, button, a")) {
            this.env.wooWorkspace.clearSelection();
        }
    }
}

export class WorkspaceKanbanController extends KanbanController {
    static template = "woow_document_management.WorkspaceKanbanView";
    static components = {
        ...KanbanController.components,
        WooSelectionBar,
        WooInspectorPane,
    };

    setup() {
        super.setup();
        this.ws = useWorkspace({
            searchModel: this.env.searchModel,
            adapter: {
                records: () => displayedRecords(this.model.root),
                reload: () => this.model.load(),
            },
        });
        useDropZone(this.ws, () => this.rootRef.el && this.rootRef.el.querySelector(".o_content"));
    }

    /** As in the list, the search bar gives its place to the selection bar while there is one. */
    get wooDisplay() {
        const { controlPanel } = this.props.display;
        if (!controlPanel) {
            return this.props.display;
        }
        return {
            ...this.props.display,
            controlPanel: { ...controlPanel, layoutActions: !this.ws.selectedIds.length },
        };
    }
}

export const workspaceKanbanView = {
    ...kanbanView,
    Controller: WorkspaceKanbanController,
    Renderer: WorkspaceKanbanRenderer,
    SearchPanel: WooSearchPanel,
};

registry.category("views").add("woow_workspace_kanban", workspaceKanbanView);
