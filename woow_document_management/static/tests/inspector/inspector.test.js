// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// Unit tests of the details panel: what it shows from the one server read, what its buttons call,
// and that it tells the screen about every change. The chatter of core `mail` is replaced by a stub
// here (the browser tour mounts the real one).
import { describe, expect, test } from "@odoo/hoot";
import { animationFrame } from "@odoo/hoot-mock";
import { Component, useState, xml } from "@odoo/owl";
import {
    contains,
    defineModels,
    fields,
    makeServerError,
    mockService,
    models,
    mountWithCleanup,
    onRpc,
    patchWithCleanup,
} from "@web/../tests/web_test_helpers";
import { defineMailModels } from "@mail/../tests/mail_test_helpers";
import { registry } from "@web/core/registry";

import { WoowDocumentInspector } from "@woow_document_management/backend/inspector/inspector";

describe.current.tags("woow_inspector");

class WoowDocument extends models.Model {
    _name = "woow.document";
    name = fields.Char();
}

class WoowDocumentTag extends models.Model {
    _name = "woow.document.tag";
    name = fields.Char();
    color = fields.Integer();
    _records = [
        { id: 1, name: "Finance", color: 3 },
        { id: 2, name: "Legal", color: 1 },
        { id: 3, name: "Draft", color: 5 },
    ];
}

defineMailModels();
defineModels([WoowDocument, WoowDocumentTag]);

class StubChatter extends Component {
    static template = xml`<div class="o_stub_chatter" t-esc="props.threadModel + ':' + props.threadId"/>`;
    static props = ["*"];
}

class Host extends Component {
    static components = { WoowDocumentInspector };
    static template = xml`<WoowDocumentInspector t-props="state"/>`;
    static props = ["*"];
    setup() {
        this.state = useState({ ...this.props.panel });
    }
}

function documentData(extra = {}) {
    return {
        id: 7,
        name: "Service agreement.pdf",
        document_type: "binary",
        type_label: "File",
        icon: "fa-file-pdf-o",
        mimetype: "application/pdf",
        file_size: 2048,
        url: false,
        active: true,
        in_trash: false,
        write_date: "2026-11-14 10:00:00",
        folder: { id: 4, name: "Company / Contracts", selectable: true },
        owner: { id: 2, name: "Mei Admin" },
        contact: { id: 9, name: "Acme Supplies" },
        company: { id: 1, name: "My Company" },
        tags: [{ id: 1, name: "Finance", color: 3 }],
        can_write: true,
        is_favorite: false,
        is_locked: false,
        locked_by_me: false,
        locked_by: false,
        can_lock: true,
        can_unlock: false,
        version_count: 2,
        has_portal_members: false,
        can_share: true,
        shares: [
            {
                id: 1,
                kind: "user",
                name: "Bob Lin",
                role: "editor",
                role_label: "Editor",
                expiry_date: false,
            },
            {
                id: 2,
                kind: "partner",
                name: "Carol Wu",
                role: "viewer",
                role_label: "Viewer",
                expiry_date: "2026-12-14",
            },
        ],
        source: false,
        shortcut_target: false,
        request: false,
        ...extra,
    };
}

// The server read, as a table of documents; a test may change it before it mounts the panel.
let documents;

function serve(trace = false) {
    documents = { 7: documentData(), 8: documentData({ id: 8, name: "Second.txt" }) };
    mockService("action", { doAction: () => {} });
    onRpc("woow.document", "woow_inspector_data", ({ args }) => {
        if (trace) {
            expect.step(`read ${args[0][0]}`);
        }
        const data = documents[args[0][0]];
        if (!data) {
            throw makeServerError({ description: "You cannot read this document." });
        }
        return data;
    });
}

async function mountPanel(props = {}) {
    patchWithCleanup(WoowDocumentInspector, {
        components: { ...WoowDocumentInspector.components, Chatter: StubChatter },
    });
    return mountWithCleanup(Host, {
        props: {
            panel: {
                resModel: "woow.document",
                resId: 7,
                onChanged: () => expect.step("changed"),
                ...props,
            },
        },
    });
}

test("the panel is in the registry category of the components", () => {
    const entry = registry.category("woow_document_components").get("inspector");
    expect(entry).toBe(WoowDocumentInspector);
});

test("without a record it says that nothing is selected", async () => {
    serve(true);
    await mountPanel({ resId: false });
    expect(".o_woow_inspector_empty").toHaveCount(1);
    expect(".o_woow_inspector_name").toHaveCount(0);
    expect.verifySteps([]);
});

test("an empty resId of any kind says that nothing is selected", async () => {
    serve(true);
    await mountPanel({ resId: null });
    expect(".o_woow_inspector_empty").toHaveCount(1);
    await mountPanel({ resId: undefined });
    expect(".o_woow_inspector_empty").toHaveCount(2);
    expect.verifySteps([]);
});

test("another model is not shown", async () => {
    serve(true);
    await mountPanel({ resModel: "res.partner", resId: 3 });
    expect(".o_woow_inspector_empty").toHaveCount(1);
    expect.verifySteps([]);
});

test("it shows the facts of the document", async () => {
    serve(true);
    await mountPanel();
    expect.verifySteps(["read 7"]);
    expect(".o_woow_inspector_name").toHaveText("Service agreement.pdf");
    expect(".o_woow_inspector_kind").toHaveText("File");
    expect(".o_woow_inspector_size").toHaveText("2.00 Kb");
    expect(".o_woow_inspector_folder").toHaveText("Company / Contracts");
    expect(".o_woow_inspector_owner").toHaveText("Mei Admin");
    expect(".o_woow_inspector_contact").toHaveText("Acme Supplies");
    expect(".o_woow_inspector_company").toHaveText("My Company");
    expect(".o_woow_inspector_tag").toHaveCount(1);
    expect(".o_woow_inspector_versions_count").toHaveText("2");
    expect(".o_woow_inspector_share_row").toHaveCount(2);
    expect(".o_woow_inspector_share_name:eq(0)").toHaveText("Bob Lin");
    expect(".o_woow_inspector_share_role:eq(0)").toHaveText("Editor");
    expect(".o_woow_inspector_share_role:eq(1)").toHaveText("Viewer");
    expect(".o_stub_chatter").toHaveText("woow.document:7");
    expect(".o_woow_inspector_portal_warning").toHaveCount(0);
    expect(".o_woow_inspector_source_open").toHaveCount(0);
    expect(".o_woow_inspector_request").toHaveCount(0);
});

test("it shows the text of a record as text, never as markup", async () => {
    serve();
    documents[7] = documentData({
        name: "<b id='injected'>bold</b>.txt",
        owner: { id: 2, name: "<i id='injected2'>Mei</i>" },
        tags: [{ id: 1, name: "<u id='injected3'>tag</u>", color: 1 }],
    });
    await mountPanel();
    expect(".o_woow_inspector_name").toHaveText("<b id='injected'>bold</b>.txt");
    expect("#injected").toHaveCount(0);
    expect("#injected2").toHaveCount(0);
    expect("#injected3").toHaveCount(0);
});

test("a reader sees no control that writes", async () => {
    serve();
    documents[7] = documentData({
        can_write: false,
        can_lock: false,
        can_share: false,
        version_count: 0,
        shares: [],
    });
    await mountPanel();
    expect(".o_woow_inspector_lock").toHaveCount(0);
    expect(".o_woow_inspector_unlock").toHaveCount(0);
    expect(".o_woow_inspector_share").toHaveCount(0);
    expect(".o_woow_inspector_tag_input").toHaveCount(0);
    expect(".o_woow_inspector_tag_remove").toHaveCount(0);
    expect(".o_woow_inspector_versions").toHaveCount(0);
    expect(".o_woow_inspector_noshares").toHaveCount(1);
    expect(".o_woow_inspector_star").toHaveCount(1);
});

test("the portal warning of the form is shown when the folder has portal members", async () => {
    serve();
    documents[7] = documentData({ has_portal_members: true });
    await mountPanel();
    expect(".o_woow_inspector_portal_warning").toHaveCount(1);
    expect(".o_woow_inspector_portal_warning").toHaveText(/Messages you send are emailed/);
});

test("a requested file shows who is asked, the due date and the upload button", async () => {
    serve();
    documents[7] = documentData({
        document_type: "request",
        type_label: "Requested file",
        file_size: 0,
        request: {
            asked: { id: 5, name: "Alice Chen" },
            deadline: "2026-11-28",
            overdue: true,
            note: "Please upload the signed copy",
            can_upload: true,
        },
    });
    await mountPanel();
    expect(".o_woow_inspector_asked").toHaveText("Alice Chen");
    expect(".o_woow_inspector_deadline").toHaveText(/2026/);
    expect(".o_woow_inspector_upload").toHaveCount(1);
    expect(".o_woow_inspector_size").toHaveCount(0);
});

test("a source that the user may read is named and can be opened", async () => {
    serve(true);
    documents[7] = documentData({ source: { model: "Contact", name: "Acme Supplies" } });
    onRpc("woow.document", "action_open_source", () => ({
        type: "ir.actions.act_window",
        res_model: "res.partner",
        res_id: 3,
        views: [[false, "form"]],
    }));
    mockService("action", {
        doAction(action) {
            expect.step(`doAction ${action.res_model} ${action.res_id}`);
        },
    });
    await mountPanel();
    expect(".o_woow_inspector_source_kind").toHaveText("Contact");
    expect(".o_woow_inspector_source_name").toHaveText("Acme Supplies");
    await contains(".o_woow_inspector_source_open").click();
    expect.verifySteps(["read 7", "doAction res.partner 3"]);
});

test("the star calls the server, reloads the record and tells the screen", async () => {
    serve(true);
    onRpc("woow.document", "action_toggle_favorite", ({ args }) => {
        expect.step(`star ${args[0][0]}`);
        documents[7].is_favorite = !documents[7].is_favorite;
        return true;
    });
    await mountPanel();
    expect(".o_woow_inspector_star .fa-star-o").toHaveCount(1);
    await contains(".o_woow_inspector_star").click();
    expect.verifySteps(["read 7", "star 7", "read 7", "changed"]);
    expect(".o_woow_inspector_star .fa-star").toHaveCount(1);
    expect(".o_woow_inspector_star").toHaveText("Remove star");
});

test("lock and unlock call their own methods", async () => {
    serve(true);
    onRpc("woow.document", "action_lock", () => {
        expect.step("lock");
        Object.assign(documents[7], { is_locked: true, locked_by_me: true, can_unlock: true });
        return true;
    });
    onRpc("woow.document", "action_unlock", () => {
        expect.step("unlock");
        Object.assign(documents[7], { is_locked: false, locked_by_me: false, can_unlock: false });
        return true;
    });
    await mountPanel();
    await contains(".o_woow_inspector_lock").click();
    expect(".o_woow_inspector_lockstate").toHaveText("Locked by you");
    expect(".o_woow_inspector_lock").toHaveCount(0);
    await contains(".o_woow_inspector_unlock").click();
    expect(".o_woow_inspector_lockstate").toHaveCount(0);
    expect.verifySteps(["read 7", "lock", "read 7", "changed", "unlock", "read 7", "changed"]);
});

test("a file locked by someone else shows the name and no lock button", async () => {
    serve();
    documents[7] = documentData({
        is_locked: true,
        locked_by: "Bob Lin",
        can_lock: false,
        can_unlock: false,
    });
    await mountPanel();
    expect(".o_woow_inspector_lockstate").toHaveText("Locked by Bob Lin");
    expect(".o_woow_inspector_lock").toHaveCount(0);
    expect(".o_woow_inspector_unlock").toHaveCount(0);
});

test("tags are added from a search and removed with their button", async () => {
    serve(true);
    onRpc("woow.document", "write", ({ args }) => {
        expect.step(`write ${JSON.stringify(args[1])}`);
        const tags = documents[7].tags;
        for (const [command, id] of args[1].tag_ids) {
            if (command === 4) {
                tags.push({ id, name: "Legal", color: 1 });
            } else if (command === 3) {
                documents[7].tags = tags.filter((tag) => tag.id !== id);
            }
        }
        return true;
    });
    await mountPanel();
    await contains(".o_woow_inspector_tag_input").edit("leg", { confirm: false });
    await animationFrame();
    expect(".o_woow_inspector_tag_choice").toHaveCount(1);
    expect(".o_woow_inspector_tag_choice").toHaveText("Legal");
    await contains(".o_woow_inspector_tag_choice").click();
    expect(".o_woow_inspector_tag").toHaveCount(2);
    await contains(".o_woow_inspector_tag:eq(0) .o_woow_inspector_tag_remove").click();
    expect(".o_woow_inspector_tag").toHaveCount(1);
    expect.verifySteps([
        "read 7",
        'write {"tag_ids":[[4,2]]}',
        "read 7",
        "changed",
        'write {"tag_ids":[[3,1]]}',
        "read 7",
        "changed",
    ]);
});

test("a tag that the document already has is not offered", async () => {
    serve();
    await mountPanel();
    await contains(".o_woow_inspector_tag_input").edit("fin", { confirm: false });
    await animationFrame();
    expect(".o_woow_inspector_tag_choice").toHaveCount(0);
});

test("a refused call shows the message of the server and keeps the panel", async () => {
    serve(true);
    onRpc("woow.document", "action_lock", () => {
        throw makeServerError({ description: "This document is locked by another user." });
    });
    await mountPanel();
    await contains(".o_woow_inspector_lock").click();
    expect(".o_woow_inspector_error").toHaveText("This document is locked by another user.");
    expect(".o_woow_inspector_name").toHaveCount(1);
    expect.verifySteps(["read 7"]);
});

test("a record that cannot be read shows the message of the server", async () => {
    serve();
    await mountPanel({ resId: 99 });
    expect(".o_woow_inspector_error").toHaveText("You cannot read this document.");
    expect(".o_woow_inspector_name").toHaveCount(0);
});

test("a folder click chooses the folder through the screen when it listens", async () => {
    serve(true);
    await mountPanel({ onSelectFolder: (id) => expect.step(`folder ${id}`) });
    await contains("a.o_woow_inspector_folder").click();
    expect.verifySteps(["read 7", "folder 4"]);
});

test("a folder click opens the workspace on that folder when no screen listens", async () => {
    serve(true);
    mockService("action", {
        doAction(action, options) {
            expect.step(
                `${action} ${options.additionalContext.searchpanel_default_folder_id}`
            );
        },
    });
    await mountPanel();
    await contains("a.o_woow_inspector_folder").click();
    expect.verifySteps([
        "read 7",
        "woow_document_management.action_woow_document_workspace 4",
    ]);
});

test("a folder that the user cannot open is not a link", async () => {
    serve();
    documents[7] = documentData({
        folder: { id: 4, name: "Restricted folder", selectable: false },
    });
    await mountPanel();
    expect("a.o_woow_inspector_folder").toHaveCount(0);
    expect("span.o_woow_inspector_folder").toHaveText("Restricted folder");
});

test("the versions button opens the list of the versions of this document", async () => {
    serve(true);
    mockService("action", {
        doAction(action) {
            expect.step(`${action.res_model} ${JSON.stringify(action.domain)}`);
        },
    });
    await mountPanel();
    await contains(".o_woow_inspector_versions").click();
    expect.verifySteps(["read 7", 'woow.document.version [["document_id","=",7]]']);
});

test("the share button opens the dialog and reloads when it closes", async () => {
    serve(true);
    onRpc("woow.document", "action_open_share_dialog", () => ({
        type: "ir.actions.act_window",
        res_model: "woow.document",
        res_id: 7,
        views: [[false, "form"]],
        target: "new",
    }));
    mockService("action", {
        async doAction(action, options) {
            expect.step(`dialog ${action.target}`);
            documents[7].shares = [];
            await options.onClose();
        },
    });
    await mountPanel();
    await contains(".o_woow_inspector_share").click();
    expect(".o_woow_inspector_noshares").toHaveCount(1);
    expect.verifySteps(["read 7", "dialog new", "read 7", "changed"]);
});

test("choosing another record loads it", async () => {
    serve(true);
    const host = await mountPanel();
    expect(".o_woow_inspector_name").toHaveText("Service agreement.pdf");
    host.state.resId = 8;
    await animationFrame();
    expect(".o_woow_inspector_name").toHaveText("Second.txt");
    host.state.resId = false;
    await animationFrame();
    expect(".o_woow_inspector_empty").toHaveCount(1);
    expect.verifySteps(["read 7", "read 8"]);
});

test("the close button calls onClose", async () => {
    serve(true);
    await mountPanel({ onClose: () => expect.step("closed") });
    await contains(".o_woow_inspector_close").click();
    expect.verifySteps(["read 7", "closed"]);
});
