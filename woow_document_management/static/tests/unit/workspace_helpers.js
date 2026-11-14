// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// The models and the views that the tests of the workspace screen mount.
import { mailModels } from "@mail/../tests/mail_test_helpers";
import { defineModels, fields, models } from "@web/../tests/web_test_helpers";

export class WoowDocumentFolder extends models.Model {
    _name = "woow.document.folder";
    _parent_name = "parent_id";

    name = fields.Char();
    parent_id = fields.Many2one({ relation: "woow.document.folder" });
    document_count = fields.Integer();
    subfolder_count = fields.Integer();
    color = fields.Integer();
    in_trash = fields.Boolean();
    woow_user_rank = fields.Integer();

    _records = [
        { id: 1, name: "Projects", parent_id: false, document_count: 2, subfolder_count: 2, woow_user_rank: 4 },
        { id: 2, name: "Alpha", parent_id: 1, document_count: 1, subfolder_count: 0, woow_user_rank: 4 },
        { id: 3, name: "Beta", parent_id: 1, document_count: 1, subfolder_count: 0, woow_user_rank: 1 },
        { id: 4, name: "Empty drawer", parent_id: false, document_count: 0, subfolder_count: 0, woow_user_rank: 4 },
    ];
}

export class WoowDocument extends models.Model {
    _name = "woow.document";

    name = fields.Char();
    document_type = fields.Selection({
        selection: [
            ["binary", "File"],
            ["url", "Link"],
            ["shortcut", "Shortcut"],
            ["request", "Request"],
        ],
    });
    mimetype = fields.Char();
    url = fields.Char();
    folder_id = fields.Many2one({ relation: "woow.document.folder" });
    is_favorite = fields.Boolean();
    is_locked = fields.Boolean();
    woow_locked_by_other = fields.Boolean();
    woow_user_can_write = fields.Boolean();
    woow_user_can_share = fields.Boolean();
    version_count = fields.Integer();

    _records = [
        { id: 1, name: "photo.png", document_type: "binary", mimetype: "image/png", folder_id: 2, woow_user_can_write: true, woow_user_can_share: true },
        { id: 2, name: "contract.pdf", document_type: "binary", mimetype: "application/pdf", folder_id: 2, woow_user_can_write: true, is_favorite: true, is_locked: true, version_count: 2 },
        { id: 3, name: "Company site", document_type: "url", url: "https://example.com/about", folder_id: 1, woow_user_can_write: true },
        { id: 4, name: "photo.png", document_type: "shortcut", folder_id: 1, woow_user_can_write: true },
        { id: 5, name: "Signed contract", document_type: "request", folder_id: 3 },
        { id: 6, name: "notes.txt", document_type: "binary", mimetype: "text/plain", folder_id: 3, woow_user_can_write: false },
        { id: 7, name: "locked.txt", document_type: "binary", mimetype: "text/plain", folder_id: 3, is_locked: true, woow_locked_by_other: true },
        { id: 8, name: "<b>bold</b><img src=x onerror=alert(1)>", document_type: "binary", mimetype: "application/zip", folder_id: 4, woow_user_can_write: true },
    ];
}

export class WoowMoveWizard extends models.Model {
    _name = "woow.document.move.wizard";

    document_ids = fields.Many2many({ relation: "woow.document" });
    target_folder_id = fields.Many2one({ relation: "woow.document.folder" });
}

export function defineWorkspaceModels() {
    defineModels({ ...mailModels, WoowDocument, WoowDocumentFolder, WoowMoveWizard });
}

const FIELDS = `
    <field name="document_type"/>
    <field name="mimetype"/>
    <field name="url"/>
    <field name="folder_id"/>
    <field name="is_favorite"/>
    <field name="is_locked"/>
    <field name="woow_locked_by_other"/>
    <field name="woow_user_can_write"/>
    <field name="woow_user_can_share"/>
    <field name="version_count"/>`;

// The card of the screen without the widgets of other apps: the picture area and the marks.
export const kanbanArch = `
    <kanban js_class="woow_workspace_kanban">
        ${FIELDS}
        <templates>
            <t t-name="card" class="woow-card flex-column p-0">
                <div class="woow-card-preview">
                    <img t-if="woo.pictureUrl" t-att-src="woo.pictureUrl" loading="lazy" alt=""/>
                    <i t-else="" t-attf-class="fa #{woo.icon} woow-kind-icon"/>
                </div>
                <field name="name"/>
                <i t-if="record.is_locked.raw_value" class="fa fa-lock"/>
                <i t-if="record.version_count.raw_value &gt; 0" class="fa fa-history"/>
                <i t-if="record.is_favorite.raw_value" class="fa fa-star"/>
            </t>
        </templates>
    </kanban>`;

export const listArch = `
    <list js_class="woow_workspace_list">
        <field name="name"/>
        <field name="folder_id"/>
        ${FIELDS.replace(/<field name="folder_id"\/>/, "")}
    </list>`;

export const searchViewArch = `
    <search>
        <field name="name"/>
        <searchpanel>
            <field name="folder_id" string="Folders" enable_counters="1" hierarchize="1" expand="1"/>
        </searchpanel>
    </search>`;
