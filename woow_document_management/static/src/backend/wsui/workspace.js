// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// The state and the behaviour that the workspace kanban and list share: selection, opening,
// the Actions menu, the folder chosen in the left panel and the upload by dropping files.
// It is a convenience only. It reads and writes through the ORM service and this module's own
// routes as the logged-in user; every call is judged by the server again.
import { onWillDestroy, useState, useSubEnv } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { useFileViewer } from "@web/core/file_viewer/file_viewer_hook";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { useBus, useService } from "@web/core/utils/hooks";
import {
    DOCUMENT_MODEL,
    DROP_MAX_FILES,
    FOLDER_MODEL,
    availableActions,
    clickSelection,
    downloadOf,
    isFileOrLink,
    downloadUrl,
    safeLinkUrl,
    toItem,
    uploadRefusal,
    viewerFile,
} from "./workspace_utils";

export const COMPONENTS_CATEGORY = "woow_document_components";
const UPLOAD_ROUTE = "/woow_document/upload";
const MOVE_WIZARD = "woow.document.move.wizard";
// What a card carries while it is dragged to a folder.
export const DRAG_TYPE = "text/plain";
export const DRAG_TEXT = "woow-documents";

/** Every record of a root list, in the order shown (grouped or not). */
export function displayedRecords(root) {
    if (root.isGrouped) {
        return root.groups.flatMap((group) => group.list.records || []);
    }
    return root.records || [];
}

/** The file name that the server proposes in the answer, or a plain fallback. */
export function downloadFilename(response) {
    const header = (response.headers && response.headers.get("Content-Disposition")) || "";
    const encoded = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
    if (encoded) {
        try {
            return decodeURIComponent(encoded[1].trim());
        } catch {
            // fall through to the plain name
        }
    }
    const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(header);
    return plain ? plain[1].trim() : "download";
}

/**
 * Downloads a file or a ZIP of this module. The route is asked first (as the logged-in user,
 * through fetch) and the file is saved only when the answer is a success: a refusal or a failure
 * shows a notification and the screen stays, instead of the whole web client navigating to a
 * bare error page.
 * @returns {Promise<boolean>} whether the download was started
 */
export async function startDownload(url, notification) {
    let blob;
    let filename;
    try {
        const response = await browser.fetch(url, { credentials: "same-origin" });
        if (!response.ok) {
            throw new Error("refused");
        }
        filename = downloadFilename(response);
        blob = await response.blob();
    } catch {
        notification.add(_t("The download failed. The file may have been moved or removed, or you may no longer have access to it."), {
            type: "danger",
        });
        return false;
    }
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = filename;
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
    browser.setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
    return true;
}

/**
 * Sends one file to the upload route of this module, into one folder.
 * @returns {Promise<{ok: boolean, message?: string}>}
 */
export async function uploadOne(file, folderId) {
    const body = new FormData();
    body.append("csrf_token", odoo.csrf_token);
    body.append("folder_id", String(folderId));
    body.append("files", file, file.name);
    let response;
    try {
        response = await browser.fetch(UPLOAD_ROUTE, { method: "POST", body });
    } catch {
        return { ok: false, message: uploadRefusal(0, null) };
    }
    let payload = null;
    try {
        payload = await response.json();
    } catch {
        payload = null;
    }
    if (response.ok && payload && payload.ok) {
        return { ok: true };
    }
    return { ok: false, message: uploadRefusal(response.status, payload) };
}

class Workspace {
    constructor({ state, services, adapter, searchModel }) {
        this.state = state;
        adapter = {
            selectedIds: () => state.selected,
            setSelection: (ids) => {
                state.selected = ids;
            },
            ...adapter,
        };
        this.orm = services.orm;
        this.action = services.action;
        this.notification = services.notification;
        this.viewer = services.viewer;
        this.adapter = adapter;
        this.searchModel = searchModel;
        this._dismissTimer = null;
    }

    // -- what is shown ---------------------------------------------------------------------
    get items() {
        return this.adapter.records().map((record) => {
            const item = toItem(record);
            item.canUpload = (this.state.ranks[item.folderId] || 0) >= 2;
            return item;
        });
    }

    /** Reads the access level of the folders of the selection (the entry Duplicate needs it). */
    async ensureRanks() {
        const wanted = [
            ...new Set(this.selectedItems.map((item) => item.folderId).filter(Boolean)),
        ].filter((id) => !(id in this.state.ranks));
        if (!wanted.length) {
            return;
        }
        let rows = [];
        try {
            rows = await this.orm.read(FOLDER_MODEL, wanted, ["woow_user_rank"]);
        } catch {
            // the level stays unknown, and the entries that need it stay hidden
            return;
        }
        for (const row of rows) {
            this.state.ranks[row.id] = row.woow_user_rank;
        }
    }

    get order() {
        return this.items.map((item) => item.id);
    }

    get selectedIds() {
        const shown = new Set(this.order);
        return this.adapter.selectedIds().filter((id) => shown.has(id));
    }

    get selectedItems() {
        const wanted = new Set(this.selectedIds);
        return this.items.filter((item) => wanted.has(item.id));
    }

    get actions() {
        const entries = availableActions(this.selectedItems);
        const extra = this.adapter.extraActions ? this.adapter.extraActions() : [];
        return entries.concat(extra);
    }

    get download() {
        return downloadOf(this.selectedItems);
    }

    get canShare() {
        const items = this.selectedItems;
        return items.length === 1 && items[0].canShare && isFileOrLink(items[0]);
    }

    isSelected(id) {
        return this.adapter.selectedIds().includes(id);
    }

    // -- selection -------------------------------------------------------------------------
    clickItem(id, keys = {}) {
        const next = clickSelection(
            { ids: this.adapter.selectedIds(), anchor: this.state.anchor },
            id,
            this.order,
            keys
        );
        this.state.anchor = next.anchor;
        this.adapter.setSelection(next.ids);
    }

    clearSelection() {
        this.state.anchor = false;
        this.adapter.setSelection([]);
    }

    // -- the details panel -----------------------------------------------------------------
    get inspector() {
        const category = registry.category(COMPONENTS_CATEGORY);
        return category.contains("inspector") ? category.get("inspector") : null;
    }

    get inspectorResId() {
        const ids = this.selectedIds;
        return ids.length === 1 ? ids[0] : false;
    }

    toggleInfo() {
        this.state.showInfo = !this.state.showInfo;
    }

    // -- reload ----------------------------------------------------------------------------
    async reload() {
        await this.adapter.reload();
        await this.refreshCounters();
        this.state.tick++;
    }

    /** The numbers of the folder tree follow what was uploaded, moved or put in the trash. */
    async refreshCounters() {
        const model = this.searchModel;
        // the search model keeps no public call that reads the counters again without a new query
        if (!model || typeof model._reloadSections !== "function") {
            return;
        }
        await model._reloadSections();
        await model.sectionsPromise;
    }

    // -- opening ---------------------------------------------------------------------------
    async open(item) {
        switch (item.documentType) {
            case "binary": {
                const file = viewerFile(item);
                if (file && file.isViewable) {
                    const files = this.items
                        .map(viewerFile)
                        .filter((candidate) => candidate && candidate.isViewable);
                    const current = files.find((candidate) => candidate.id === item.id);
                    this.viewer.open(current, files);
                } else {
                    await startDownload(downloadUrl(item.id), this.notification);
                }
                return;
            }
            case "url": {
                const url = safeLinkUrl(item.url);
                if (url) {
                    browser.open(url, "_blank", "noopener,noreferrer");
                } else {
                    this.notification.add(_t("This link cannot be opened."), { type: "warning" });
                }
                return;
            }
            case "shortcut":
                await this.callButton("action_open_shortcut_target", [item.id]);
                return;
            case "request":
                await this.callButton("action_open_fulfil_wizard", [item.id]);
                return;
        }
    }

    openSelected() {
        const items = this.selectedItems;
        if (items.length === 1) {
            return this.open(items[0]);
        }
    }

    // -- the control panel -----------------------------------------------------------------
    async downloadSelection() {
        const target = this.download;
        if (target) {
            await startDownload(target.url, this.notification);
        }
    }

    /**
     * Runs a method of the server the way a button of type "object" does: through the button
     * route, which completes the action the method returns (the plain call route leaves out the
     * views a wizard needs, and the web client cannot open it). A method that returns nothing
     * closes, and the screen is read again when the wizard is closed or the method is done.
     */
    async callButton(method, ids) {
        await this.action.doActionButton({
            type: "object",
            name: method,
            resModel: DOCUMENT_MODEL,
            resIds: ids,
            context: user.context,
            onClose: () => this.reload(),
        });
    }

    /** Opens an action that is bound to the documents (as the standard menu of a list does). */
    async callAction(xmlId, ids) {
        await this.action.doActionButton({
            type: "action",
            name: xmlId,
            resModel: DOCUMENT_MODEL,
            resIds: ids,
            context: user.context,
            onClose: () => this.reload(),
        });
    }

    async share() {
        const [item] = this.selectedItems;
        if (!item) {
            return;
        }
        await this.callButton("action_open_share_dialog", [item.id]);
    }

    async runAction(entry) {
        const ids = entry.ids || this.selectedIds;
        if (!ids.length) {
            return;
        }
        if (entry.run) {
            return entry.run();
        }
        if (entry.action) {
            return this.callAction(entry.action, ids);
        }
        if (entry.each) {
            for (const id of ids) {
                await this.orm.call(DOCUMENT_MODEL, entry.method, [[id]]);
            }
            await this.reload();
            return;
        }
        if (entry.key === "trash") {
            this.clearSelection();
        }
        await this.callButton(entry.method, ids);
    }

    // -- the folder chosen in the left panel -----------------------------------------------
    get folderSection() {
        const model = this.searchModel;
        if (!model || !model.categories) {
            return null;
        }
        return model.categories.find((category) => category.fieldName === "folder_id") || null;
    }

    get activeFolderId() {
        const section = this.folderSection;
        return (section && section.activeValueId) || false;
    }

    /** The ids of the chosen folder and of the folders above it, the top one first. */
    get pathIds() {
        const section = this.folderSection;
        if (!section || !section.activeValueId) {
            return [];
        }
        const chain = [];
        let valueId = section.activeValueId;
        const seen = new Set();
        while (valueId && section.values.has(valueId) && !seen.has(valueId)) {
            seen.add(valueId);
            chain.unshift(valueId);
            valueId = section.values.get(valueId).parentId;
        }
        return chain;
    }

    chooseFolder(id) {
        const section = this.folderSection;
        if (!section || (id !== false && !section.values.has(id))) {
            return;
        }
        this.clearSelection();
        this.searchModel.toggleCategoryValue(section.id, id);
    }

    /** The ids of the folders that the panel shows at the top level (for the folder cards). */
    get panelRootIds() {
        const section = this.folderSection;
        return section ? section.rootIds.filter((id) => id) : [];
    }

    // -- dragging cards onto a folder ------------------------------------------------------
    /**
     * Starts the drag of a card: the card joins the selection when it is not in it. Only what the
     * user may move is dragged; the move itself is the move wizard, which judges it again.
     * @returns {boolean} whether the drag may go on
     */
    startDrag(id) {
        if (!this.isSelected(id)) {
            this.clickItem(id);
        }
        const items = this.selectedItems;
        if (!items.length || !items.every((item) => item.canWrite && !item.lockedByOther)) {
            this.notification.add(_t("You cannot move this selection."), { type: "warning" });
            return false;
        }
        this.state.dragIds = items.map((item) => item.id);
        return true;
    }

    endDrag() {
        this.state.dragIds = [];
        this.state.dropFolderId = false;
    }

    get isDragging() {
        return this.state.dragIds.length > 0;
    }

    /** Moves the cards that are being dragged into the folder, through the move wizard. */
    async dropOnFolder(folderId) {
        const ids = [...this.state.dragIds];
        this.endDrag();
        if (!ids.length || !folderId) {
            return;
        }
        const [wizardId] = await this.orm.create(MOVE_WIZARD, [
            { document_ids: [[6, 0, ids]], target_folder_id: folderId },
        ]);
        await this.orm.call(MOVE_WIZARD, "action_move", [[wizardId]]);
        this.notification.add(_t("Moved."), { type: "success" });
        this.clearSelection();
        await this.reload();
    }

    // -- dropping files from the computer --------------------------------------------------
    /**
     * Uploads dropped files into a folder: the one under the pointer when it was dropped on a
     * folder (a card or the tree), else the one chosen in the left panel.
     */
    async dropFiles(files, { skipped = 0, folderId = false } = {}) {
        folderId = folderId || this.activeFolderId;
        if (!folderId) {
            this.notification.add(
                _t("Choose a folder in the left panel, then drop the files again."),
                { type: "warning" }
            );
            return;
        }
        if (this.state.upload && !this.state.upload.finished) {
            return;
        }
        if (!files.length && !skipped) {
            return;
        }
        if (files.length > DROP_MAX_FILES) {
            this.notification.add(_t("Drop at most %s files at a time.", DROP_MAX_FILES), {
                type: "warning",
            });
            return;
        }
        const [folder] = await this.orm.read(FOLDER_MODEL, [folderId], ["name", "woow_user_rank"]);
        if (!folder || folder.woow_user_rank < 2) {
            this.notification.add(_t("You cannot add files to this folder."), { type: "warning" });
            return;
        }
        browser.clearTimeout(this._dismissTimer);
        this.state.upload = {
            total: files.length,
            folderName: folder.name,
            done: 0,
            current: "",
            finished: false,
            uploaded: 0,
            errors: [],
        };
        // read it back: the reactive copy is the one whose changes the screen follows
        const upload = this.state.upload;
        if (skipped) {
            upload.errors.push({
                name: _t("Folders"),
                message: _t("Folders cannot be dropped; drop the files they hold."),
            });
        }
        for (const file of files) {
            upload.current = file.name;
            const result = await uploadOne(file, folderId);
            upload.done++;
            if (result.ok) {
                upload.uploaded++;
            } else {
                upload.errors.push({ name: file.name, message: result.message });
            }
        }
        upload.current = "";
        upload.finished = true;
        await this.reload();
        if (!upload.errors.length) {
            this._dismissTimer = browser.setTimeout(() => this.dismissUpload(), 5000);
        }
    }

    dismissUpload() {
        browser.clearTimeout(this._dismissTimer);
        this.state.upload = null;
    }

    destroy() {
        browser.clearTimeout(this._dismissTimer);
    }
}

/**
 * Creates the workspace of a view controller and puts it in the environment of its children
 * under the key "wooWorkspace".
 * @param {Object} params
 * @param {Object} params.adapter {records, selectedIds, setSelection, reload}: how the view
 *     keeps its records and its selection
 * @param {Object} params.searchModel the search model of the view
 */
export function useWorkspace({ adapter, searchModel }) {
    const state = useState({
        selected: [],
        anchor: false,
        showInfo: false,
        dropping: false,
        dragIds: [],
        dropFolderId: false,
        upload: null,
        ranks: {},
        tick: 0,
    });
    const { open, close } = useFileViewer();
    const services = {
        orm: useService("orm"),
        action: useService("action"),
        notification: useService("notification"),
        viewer: { open, close },
    };
    const workspace = new Workspace({ state, services, adapter, searchModel });
    useSubEnv({ wooWorkspace: workspace });
    if (searchModel) {
        // another folder or filter: what was selected is not in front of the user any more
        useBus(searchModel, "update", () => workspace.clearSelection());
    }
    onWillDestroy(() => workspace.destroy());
    return workspace;
}
