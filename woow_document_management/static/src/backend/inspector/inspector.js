// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// The details panel of a document. It is registered in the registry category
// "woow_document_components" under the key "inspector"; a screen looks it up there and passes
// { resModel, resId, onClose, onChanged }. The panel adds no right: everything it shows is read by
// one server method as the user, and every button calls a server method that checks access.
import { Chatter } from "@mail/chatter/web_portal/chatter";
import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
import { deserializeDate, deserializeDateTime, formatDate, formatDateTime } from "@web/core/l10n/dates";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { humanSize } from "@web/core/utils/binary";
import { useService } from "@web/core/utils/hooks";

const DOCUMENT_MODEL = "woow.document";
const WORKSPACE_ACTION = "woow_document_management.action_woow_document_workspace";
const VERSION_LIST_VIEW = "woow_document_management.view_woow_document_version_list";
const TAG_SUGGESTIONS = 8;
const SEARCH_MAX_CHARS = 100;

// The icon of the kinds of document that the server does not tell by their type of file.
const KIND_ICONS = { shortcut: "fa-share", request: "fa-inbox" };

/** The message of a failed call: the server's own text when it sent one. */
export function errorMessage(error) {
    return (
        (error && error.data && error.data.message) ||
        (error && error.message) ||
        _t("Something went wrong.")
    );
}

export class WoowDocumentInspector extends Component {
    static template = "woow_document_management.Inspector";
    static components = { Chatter };
    static props = {
        resModel: { type: String },
        resId: { type: [Number, Boolean, { value: null }], optional: true },
        onClose: { type: Function, optional: true },
        onChanged: { type: Function, optional: true },
        // optional: a screen that wants to choose the folder itself (the panel falls back to
        // opening the workspace with that folder chosen)
        onSelectFolder: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            data: null,
            error: "",
            busy: false,
            tagSearch: "",
            tagChoices: [],
        });
        this.loadToken = 0;
        this.tagToken = 0;
        onWillStart(() => this.load(this.props));
        onWillUpdateProps((nextProps) => {
            if (nextProps.resId !== this.props.resId || nextProps.resModel !== this.props.resModel) {
                return this.load(nextProps);
            }
        });
    }

    get supported() {
        return this.props.resModel === DOCUMENT_MODEL && Boolean(this.props.resId);
    }

    get data() {
        return this.state.data;
    }

    get iconClass() {
        const data = this.data;
        return KIND_ICONS[data.document_type] || data.icon || "fa-file-o";
    }

    get sizeText() {
        const data = this.data;
        return data.document_type === "binary" && data.file_size ? humanSize(data.file_size) : "";
    }

    get changedText() {
        const value = this.data && this.data.write_date;
        return value ? formatDateTime(deserializeDateTime(value)) : "";
    }

    get deadlineText() {
        const value = this.data && this.data.request && this.data.request.deadline;
        return value ? formatDate(deserializeDate(value)) : "";
    }

    expiryText(share) {
        return share.expiry_date ? formatDate(deserializeDate(share.expiry_date)) : "";
    }

    tagClass(tag) {
        return `o_tag_color_${Number.isInteger(tag.color) ? tag.color : 0}`;
    }

    async load(props) {
        const token = ++this.loadToken;
        if (props.resModel !== DOCUMENT_MODEL || !props.resId) {
            this.state.data = null;
            this.state.error = "";
            return;
        }
        try {
            const data = await this.orm.call(DOCUMENT_MODEL, "woow_inspector_data", [[props.resId]]);
            if (token === this.loadToken) {
                this.state.data = data;
                this.state.error = "";
            }
        } catch (error) {
            if (token === this.loadToken) {
                this.state.data = null;
                this.state.error = errorMessage(error);
            }
        }
    }

    /** Run one call of a button, then show the new state and tell the screen. */
    async run(call, { changes = true } = {}) {
        if (this.state.busy) {
            return;
        }
        this.state.busy = true;
        try {
            const result = await call();
            if (changes) {
                await this.load(this.props);
                this.notifyChanged();
            }
            return result;
        } catch (error) {
            this.state.error = errorMessage(error);
        } finally {
            this.state.busy = false;
        }
    }

    notifyChanged() {
        if (this.props.onChanged) {
            this.props.onChanged();
        }
    }

    callOnDocument(method) {
        return this.orm.call(DOCUMENT_MODEL, method, [[this.props.resId]]);
    }

    /**
     * A button method that returns an action. It goes through the route that buttons use, which
     * completes the action (the views) before it is run.
     */
    callButton(method) {
        return rpc(`/web/dataset/call_button/${DOCUMENT_MODEL}/${method}`, {
            args: [[this.props.resId]],
            kwargs: { context: user.context },
            method,
            model: DOCUMENT_MODEL,
        });
    }

    /** Open an action that the server returned; the panel is reloaded when its dialog closes. */
    async openAction(method) {
        const result = await this.run(() => this.callButton(method), { changes: false });
        if (result) {
            await this.action.doAction(result, {
                onClose: async () => {
                    await this.load(this.props);
                    this.notifyChanged();
                },
            });
        }
    }

    toggleStar() {
        return this.run(() => this.callOnDocument("action_toggle_favorite"));
    }

    toggleLock() {
        const method = this.data.is_locked ? "action_unlock" : "action_lock";
        return this.run(() => this.callOnDocument(method));
    }

    openShare() {
        return this.openAction("action_open_share_dialog");
    }

    openUpload() {
        return this.openAction("action_open_fulfil_wizard");
    }

    async openSource() {
        const result = await this.run(() => this.callButton("action_open_source"), {
            changes: false,
        });
        if (result) {
            await this.action.doAction(result);
        }
    }

    openVersions() {
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: _t("Versions"),
            res_model: "woow.document.version",
            views: [[false, "list"]],
            domain: [["document_id", "=", this.props.resId]],
            context: { list_view_ref: VERSION_LIST_VIEW },
        });
    }

    chooseFolder() {
        const folder = this.data.folder;
        if (!folder.selectable) {
            return;
        }
        if (this.props.onSelectFolder) {
            return this.props.onSelectFolder(folder.id);
        }
        return this.action.doAction(WORKSPACE_ACTION, {
            clearBreadcrumbs: true,
            additionalContext: { searchpanel_default_folder_id: folder.id },
        });
    }

    // -- tags ---------------------------------------------------------------------------------
    async onTagInput(ev) {
        const term = ev.target.value.slice(0, SEARCH_MAX_CHARS);
        this.state.tagSearch = term;
        const token = ++this.tagToken;
        if (!term.trim()) {
            this.state.tagChoices = [];
            return;
        }
        const taken = this.data.tags.map((tag) => tag.id);
        try {
            const tags = await this.orm.searchRead(
                "woow.document.tag",
                [["name", "ilike", term.trim()], ["id", "not in", taken]],
                ["name", "color"],
                { limit: TAG_SUGGESTIONS }
            );
            if (token === this.tagToken) {
                this.state.tagChoices = tags;
            }
        } catch (error) {
            this.state.error = errorMessage(error);
        }
    }

    async changeTags(command) {
        await this.run(() =>
            this.orm.write(DOCUMENT_MODEL, [this.props.resId], { tag_ids: [command] })
        );
    }

    async addTag(tag) {
        this.state.tagSearch = "";
        this.state.tagChoices = [];
        await this.changeTags([4, tag.id]);
    }

    removeTag(tag) {
        return this.changeTags([3, tag.id]);
    }
}

registry.category("woow_document_components").add("inspector", WoowDocumentInspector);
