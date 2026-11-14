// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// Plain helpers of the workspace screen: what kind of file a card shows, which URLs it uses,
// which entries of the Actions menu apply to a selection, and how a click changes a selection.
// Nothing here talks to the server or builds markup from data; every URL part that comes from a
// record is passed through encodeURIComponent.
import { _t } from "@web/core/l10n/translation";

export const DOCUMENT_MODEL = "woow.document";
export const FOLDER_MODEL = "woow.document.folder";
// The server answers at most this many documents of one duplicate / lock call (see the models).
export const DUPLICATE_MAX = 20;
export const LOCK_MAX = 200;
// A drop of more files than this is refused before anything is sent.
export const DROP_MAX_FILES = 50;
// Folder cards shown at once above the files.
export const FOLDER_CARD_LIMIT = 60;

// The types the preview route serves inline (the module's own list) and the viewer can show.
const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const PDF_TYPE = "application/pdf";
const TEXT_TYPE = "text/plain";

const KIND_ICONS = {
    image: "fa-file-image-o",
    pdf: "fa-file-pdf-o",
    text: "fa-file-text-o",
    sheet: "fa-file-excel-o",
    word: "fa-file-word-o",
    slides: "fa-file-powerpoint-o",
    archive: "fa-file-archive-o",
    video: "fa-file-video-o",
    audio: "fa-file-audio-o",
    link: "fa-link",
    shortcut: "fa-share-square-o",
    request: "fa-inbox",
    file: "fa-file-o",
};

/** The base type of a mimetype, lower case, without parameters. */
export function baseMimetype(mimetype) {
    return String(mimetype || "").split(";", 1)[0].trim().toLowerCase();
}

/**
 * The kind of a document, from its type and its mimetype.
 * @param {{documentType: string, mimetype?: string}} doc
 * @returns {string} one key of KIND_ICONS
 */
export function documentKind(doc) {
    if (doc.documentType === "url") {
        return "link";
    }
    if (doc.documentType === "shortcut") {
        return "shortcut";
    }
    if (doc.documentType === "request") {
        return "request";
    }
    const mimetype = baseMimetype(doc.mimetype);
    if (mimetype === PDF_TYPE) {
        return "pdf";
    }
    if (mimetype.startsWith("image/")) {
        return "image";
    }
    if (mimetype.startsWith("video/")) {
        return "video";
    }
    if (mimetype.startsWith("audio/")) {
        return "audio";
    }
    if (
        mimetype === "text/csv" ||
        mimetype.includes("spreadsheet") ||
        mimetype === "application/vnd.ms-excel"
    ) {
        return "sheet";
    }
    if (
        mimetype === "application/msword" ||
        mimetype.includes("wordprocessingml") ||
        mimetype === "application/vnd.oasis.opendocument.text"
    ) {
        return "word";
    }
    if (mimetype.includes("presentation") || mimetype === "application/vnd.ms-powerpoint") {
        return "slides";
    }
    if (
        ["application/zip", "application/x-zip", "application/gzip", "application/x-tar"].includes(
            mimetype
        )
    ) {
        return "archive";
    }
    if (mimetype.startsWith("text/")) {
        return "text";
    }
    return "file";
}

export function kindIcon(kind) {
    return KIND_ICONS[kind] || KIND_ICONS.file;
}

/** True when the card of the document shows the picture itself. */
export function hasPicture(doc) {
    return doc.documentType === "binary" && IMAGE_TYPES.includes(baseMimetype(doc.mimetype));
}

// -- URLs of this module's routes -------------------------------------------------------------
export function previewUrl(id) {
    return `/woow_document/doc/${encodeURIComponent(id)}/preview`;
}

export function downloadUrl(id) {
    return `/woow_document/doc/${encodeURIComponent(id)}/download`;
}

export function zipUrl(ids) {
    return `/woow_document/zip?document_ids=${encodeURIComponent(ids.join(","))}`;
}

/**
 * A link target the browser may open: http, https or mailto, no credentials in it. Anything else
 * (javascript:, data:, a relative path, text that is not a URL) gives null. The server checks the
 * same rule when a link is stored; this keeps an old or edited value from being opened blindly.
 * @param {string} value
 * @returns {string|null} the normalized URL
 */
export function safeLinkUrl(value) {
    if (typeof value !== "string" || !value || value.length > 2048) {
        return null;
    }
    if (/[\s\\\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/.test(value)) {
        return null;
    }
    // an explicit scheme is required: a relative address is not a link to open
    if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(value)) {
        return null;
    }
    let parsed;
    try {
        parsed = new URL(value);
    } catch {
        return null;
    }
    if (parsed.protocol === "mailto:") {
        return parsed.pathname.includes("@") && !parsed.host ? parsed.href : null;
    }
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
        return null;
    }
    if (!parsed.hostname || parsed.username || parsed.password) {
        return null;
    }
    return parsed.href;
}

// -- the items the screen works with ----------------------------------------------------------
/**
 * The facts of one record that the screen needs, from the data of a web-client record.
 * @param {{resId: number, data: Object}} record
 */
export function toItem(record) {
    const data = record.data;
    const folder = data.folder_id;
    return {
        id: record.resId,
        name: data.name || "",
        documentType: data.document_type || "binary",
        mimetype: data.mimetype || "",
        url: data.url || "",
        folderId: Array.isArray(folder) ? folder[0] : (folder && folder.id) || false,
        isFavorite: Boolean(data.is_favorite),
        isLocked: Boolean(data.is_locked),
        lockedByOther: Boolean(data.woow_locked_by_other),
        canWrite: Boolean(data.woow_user_can_write),
        canShare: Boolean(data.woow_user_can_share),
        versionCount: data.version_count || 0,
    };
}

const isFile = (item) => item.documentType === "binary";
export const isFileOrLink = (item) => item.documentType === "binary" || item.documentType === "url";
const isPdf = (item) => isFile(item) && baseMimetype(item.mimetype) === PDF_TYPE;

/** The file object that the core file viewer takes, or null when the file cannot be viewed. */
export function viewerFile(item) {
    if (!isFile(item)) {
        return null;
    }
    const mimetype = baseMimetype(item.mimetype);
    const source = previewUrl(item.id);
    const file = {
        id: item.id,
        displayName: item.name,
        downloadUrl: downloadUrl(item.id),
        mimetype,
        defaultSource: source,
        isImage: false,
        isPdf: false,
        isText: false,
        isViewable: false,
    };
    if (IMAGE_TYPES.includes(mimetype)) {
        file.isImage = true;
        file.isViewable = true;
    } else if (mimetype === PDF_TYPE) {
        file.isPdf = true;
        file.isViewable = true;
        file.defaultSource = `/web/static/lib/pdfjs/web/viewer.html?file=${encodeURIComponent(
            source
        )}#pagemode=none`;
    } else if (mimetype === TEXT_TYPE) {
        file.isText = true;
        file.isViewable = true;
    }
    return file;
}

// -- the entries that apply to a selection ----------------------------------------------------
/**
 * The download of a selection: one file goes to the download route, several to the ZIP route.
 * @returns {{url: string, count: number}|null}
 */
export function downloadOf(items) {
    const ids = items.filter(isFile).map((item) => item.id);
    if (!ids.length) {
        return null;
    }
    return { url: ids.length === 1 ? downloadUrl(ids[0]) : zipUrl(ids), count: ids.length };
}

/**
 * The entries of the Actions menu that apply to the items. An entry that does not apply is not
 * returned, so the menu never offers it. The server checks every call again.
 * @param {Array} items values of toItem()
 * @returns {Array<{key: string, label: string, icon: string, method: string, each?: boolean,
 *     ids?: number[]}>}
 */
export function availableActions(items) {
    const entries = [];
    if (!items.length) {
        return entries;
    }
    const single = items.length === 1;
    const [first] = items;
    const writable = items.every((item) => item.canWrite && !item.lockedByOther);
    if (single && first.canWrite && !first.lockedByOther) {
        entries.push({
            key: "rename",
            label: _t("Rename"),
            icon: "fa-pencil",
            method: "action_open_rename_wizard",
        });
    }
    if (writable) {
        entries.push({
            key: "move",
            label: _t("Move"),
            icon: "fa-arrows",
            method: "action_open_move_wizard",
        });
    }
    if (items.length <= DUPLICATE_MAX && items.every(isFileOrLink)) {
        entries.push({
            key: "duplicate",
            label: _t("Duplicate"),
            icon: "fa-clone",
            method: "action_duplicate",
        });
    }
    const unstarred = items.filter((item) => !item.isFavorite);
    const starTargets = unstarred.length ? unstarred : items;
    entries.push({
        key: "star",
        label: unstarred.length ? _t("Star") : _t("Remove star"),
        icon: unstarred.length ? "fa-star" : "fa-star-o",
        method: "action_toggle_favorite",
        each: true,
        ids: starTargets.map((item) => item.id),
    });
    if (single && isFileOrLink(first)) {
        entries.push({
            key: "shortcut",
            label: _t("Create shortcut"),
            icon: "fa-share-square-o",
            method: "action_open_shortcut_wizard",
        });
    }
    if (single && isFile(first) && first.canWrite && !first.lockedByOther) {
        entries.push({
            key: "new_version",
            label: _t("New version"),
            icon: "fa-upload",
            method: "action_new_version",
        });
    }
    if (single && isPdf(first)) {
        entries.push({
            key: "pdf_split",
            label: _t("Split PDF"),
            icon: "fa-scissors",
            method: "action_open_pdf_split_wizard",
        });
    }
    if (items.length > 1 && items.every(isPdf)) {
        entries.push({
            key: "pdf_merge",
            label: _t("Merge PDF files"),
            icon: "fa-object-group",
            method: "action_open_pdf_merge_wizard",
        });
    }
    if (items.length <= LOCK_MAX && items.every(isFileOrLink)) {
        if (items.every((item) => item.canWrite && !item.isLocked)) {
            entries.push({
                key: "lock",
                label: _t("Lock"),
                icon: "fa-lock",
                method: "action_lock",
            });
        }
        if (items.some((item) => item.isLocked)) {
            entries.push({
                key: "unlock",
                label: _t("Unlock"),
                icon: "fa-unlock",
                method: "action_unlock",
                ids: items.filter((item) => item.isLocked).map((item) => item.id),
            });
        }
    }
    if (writable) {
        entries.push({
            key: "trash",
            label: _t("Move to trash"),
            icon: "fa-trash",
            method: "action_trash",
            danger: true,
        });
    }
    return entries;
}

// -- selection by click -----------------------------------------------------------------------
/**
 * The selection after a click on one card or row.
 * A plain click selects only that one; Ctrl (or Cmd) toggles it; Shift selects the range between
 * the last clicked one and this one, in the order shown.
 * @param {{ids: number[], anchor: number|false}} selection
 * @param {number} id
 * @param {number[]} order the ids as they are shown
 * @param {{ctrl?: boolean, shift?: boolean}} keys
 */
export function clickSelection(selection, id, order, keys = {}) {
    const current = selection.ids.filter((value) => order.includes(value));
    if (keys.shift && selection.anchor !== false && order.includes(selection.anchor)) {
        const from = order.indexOf(selection.anchor);
        const to = order.indexOf(id);
        const range = order.slice(Math.min(from, to), Math.max(from, to) + 1);
        const merged = keys.ctrl ? [...new Set([...current, ...range])] : range;
        return { ids: merged, anchor: selection.anchor };
    }
    if (keys.ctrl) {
        const ids = current.includes(id)
            ? current.filter((value) => value !== id)
            : [...current, id];
        return { ids, anchor: id };
    }
    return { ids: [id], anchor: id };
}

/** The message of a refused upload, from the HTTP status and the JSON body (when there is one). */
export function uploadRefusal(status, payload) {
    if (payload && typeof payload.message === "string" && payload.message) {
        return payload.message;
    }
    if (status === 413) {
        return _t("The file is too large.");
    }
    if (status === 429 || status === 503) {
        return _t("The server is busy. Try again in a moment.");
    }
    if (status === 404) {
        return _t("The folder is not available.");
    }
    return _t("The file could not be uploaded.");
}
