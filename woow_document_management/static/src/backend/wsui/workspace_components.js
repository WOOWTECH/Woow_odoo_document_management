// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// Small components of the workspace screen: the selection bar of the control panel, the folder
// strip above the files, the upload status and the host of the details panel.
import { Component, onWillStart, useEffect, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { DropdownItem } from "@web/core/dropdown/dropdown_item";
import { useBus, useService } from "@web/core/utils/hooks";
import { FOLDER_CARD_LIMIT, FOLDER_MODEL } from "./workspace_utils";

/** The number selected, Download, Share and the Actions menu of the selection. */
export class WooSelectionBar extends Component {
    static template = "woow_document_management.SelectionBar";
    static components = { Dropdown, DropdownItem };
    static props = {
        ws: Object,
        selection: { type: Array, optional: true },
        showCount: { type: Boolean, optional: true },
        onClear: { type: Function, optional: true },
    };
    static defaultProps = { showCount: true };

    setup() {
        this.state = useState(this.props.ws.state);
    }

    get ws() {
        return this.props.ws;
    }

    get items() {
        return this.ws.selectedItems;
    }

    get countLabel() {
        return _t("%s selected", this.items.length);
    }

    clear() {
        if (this.props.onClear) {
            this.props.onClear();
        } else {
            this.ws.clearSelection();
        }
    }
}

/** The path of the chosen folder and the folders it holds, above the files. */
export class WooFolderStrip extends Component {
    static template = "woow_document_management.FolderStrip";
    static props = {};

    setup() {
        this.ws = this.env.wooWorkspace;
        this.state = useState({ cards: [], names: {} });
        this.workspaceState = useState(this.ws.state);
        this.orm = useService("orm");
        this.token = 0;
        onWillStart(async () => {
            if (this.env.searchModel) {
                await this.env.searchModel.sectionsPromise;
            }
        });
        if (this.env.searchModel) {
            useBus(this.env.searchModel, "update", async () => {
                await this.env.searchModel.sectionsPromise;
                this.render();
            });
        }
        useEffect(
            () => {
                this.loadCards();
            },
            () => [
                this.ws.activeFolderId,
                this.workspaceState.tick,
                this.ws.panelRootIds.join(),
                this.ws.pathIds.join(),
            ]
        );
    }

    /** The folders under the chosen one (the top ones when none is chosen), read as the user. */
    async loadCards() {
        const ticket = ++this.token;
        const parent = this.ws.activeFolderId;
        if (!this.ws.folderSection) {
            this.state.cards = [];
            return;
        }
        const path = this.ws.pathIds;
        if (path.length) {
            // the plain names, not the display names that already hold the whole path
            const folders = await this.orm.read(FOLDER_MODEL, path, ["name"]);
            const names = {};
            for (const folder of folders) {
                names[folder.id] = folder.name;
            }
            if (ticket === this.token) {
                this.state.names = names;
            }
        }
        const fields = ["name", "document_count", "subfolder_count", "color"];
        const options = { limit: FOLDER_CARD_LIMIT };
        let cards = await this.orm.searchRead(
            FOLDER_MODEL,
            [
                ["parent_id", "=", parent],
                ["in_trash", "=", false],
            ],
            fields,
            options
        );
        if (!parent) {
            // a top-level folder whose parent the user may not open is a root of the panel too
            const known = new Set(cards.map((card) => card.id));
            const extra = this.ws.panelRootIds.filter((id) => !known.has(id));
            if (extra.length) {
                const more = await this.orm.read(FOLDER_MODEL, extra, fields);
                cards = cards.concat(more);
            }
        }
        if (ticket === this.token) {
            this.state.cards = cards;
        }
    }

    /** The path of the chosen folder as clickable parts, the first one being "all folders". */
    get breadcrumb() {
        const parts = [{ id: false, name: _t("All folders") }];
        for (const id of this.ws.pathIds) {
            parts.push({ id, name: this.state.names[id] || "…" });
        }
        return parts;
    }

    // A card of the files may be dropped on a folder card to move it there.
    onFolderDragOver(ev, card) {
        if (!this.ws.isDragging) {
            return;
        }
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        this.workspaceState.dropFolderId = card.id;
    }

    onFolderDragLeave(card) {
        if (this.workspaceState.dropFolderId === card.id) {
            this.workspaceState.dropFolderId = false;
        }
    }

    onFolderDrop(ev, card) {
        if (!this.ws.isDragging) {
            return;
        }
        ev.preventDefault();
        return this.ws.dropOnFolder(card.id);
    }

    countLabel(card) {
        return card.document_count === 1
            ? _t("1 file")
            : _t("%s files", card.document_count);
    }

    choose(card) {
        this.ws.chooseFolder(card.id);
    }
}

/** The progress and the refusals of an upload by dropping files. */
export class WooUploadStatus extends Component {
    static template = "woow_document_management.UploadStatus";
    static props = {};

    setup() {
        this.ws = this.env.wooWorkspace;
        this.state = useState(this.ws.state);
    }

    get title() {
        const upload = this.state.upload;
        if (!upload.finished) {
            return _t("Uploading %(done)s of %(total)s", {
                done: Math.min(upload.done + 1, upload.total),
                total: upload.total,
            });
        }
        if (upload.errors.length) {
            return _t("%(ok)s of %(total)s files uploaded", {
                ok: upload.uploaded,
                total: upload.total,
            });
        }
        return _t("%s files uploaded", upload.uploaded);
    }

    get percent() {
        const upload = this.state.upload;
        return upload.total ? Math.round((upload.done * 100) / upload.total) : 100;
    }

    dismiss() {
        this.ws.dismissUpload();
    }
}

/** The right-hand panel: the component registered by the details-panel lane, when it is there. */
export class WooInspectorPane extends Component {
    static template = "woow_document_management.InspectorPane";
    static props = { ws: Object, selection: { type: Array, optional: true } };

    setup() {
        this.state = useState(this.props.ws.state);
    }

    get ws() {
        return this.props.ws;
    }

    close() {
        this.ws.state.showInfo = false;
    }

    onChanged() {
        return this.ws.reload();
    }
}
