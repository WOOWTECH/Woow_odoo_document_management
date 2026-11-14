// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

import { describe, expect, test } from "@odoo/hoot";
import { click, dblclick, keyDown, keyUp, press, queryAll, queryAllTexts, queryOne } from "@odoo/hoot-dom";
import { animationFrame } from "@odoo/hoot-mock";
import { Component, xml } from "@odoo/owl";
import {
    contains,
    mockService,
    mountView,
    onRpc,
    patchWithCleanup,
} from "@web/../tests/web_test_helpers";
import { browser } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";
import {
    defineWorkspaceModels,
    kanbanArch,
    listArch,
    searchViewArch,
} from "./workspace_helpers";

describe.current.tags("desktop");
defineWorkspaceModels();

// The folder strip reads the folders after the first render: give it its turn.
async function mountKanban(params = {}) {
    const view = await mountView({
        resModel: "woow.document",
        type: "kanban",
        arch: kanbanArch,
        searchViewArch,
        ...params,
    });
    await animationFrame();
    return view;
}
async function mountList(params = {}) {
    const view = await mountView({
        resModel: "woow.document",
        type: "list",
        arch: listArch,
        searchViewArch,
        ...params,
    });
    await animationFrame();
    return view;
}

// The strip reads the folders again after a folder is chosen: let the requests come back.
async function settle() {
    for (let turn = 0; turn < 4; turn++) {
        await animationFrame();
    }
}

// An entry that returns an action is run the way a button of type "object" runs: through the
// action service, which asks the button route and so receives the action completed with its views
// (the plain call route gives a wizard no views and the web client cannot open it).
function mockButtons(steps = (params) => `${params.name} ${JSON.stringify(params.resIds)}`) {
    mockService("action", {
        doActionButton(params) {
            expect.step(steps(params));
            expect(params.type).toBe("object");
            expect(params.resModel).toBe("woow.document");
            expect(typeof params.onClose).toBe("function");
        },
        doAction() {
            throw new Error("an entry must not hand a raw server action to doAction");
        },
    });
}

const cardOf = (name) =>
    queryAll(".o_kanban_record:not(.o_kanban_ghost)").find((el) => el.textContent.includes(name));
const selectedNames = () =>
    queryAll(".o_kanban_record.woow-selected").map((el) => el.textContent.trim());

// -- the cards ----------------------------------------------------------------------------
test("a card shows the picture of an image and the icon of the other kinds", async () => {
    await mountKanban();
    expect(".o_kanban_record:not(.o_kanban_ghost)").toHaveCount(8);
    const images = queryAll(".woow-card-preview img");
    expect(images).toHaveCount(1);
    // a small copy through the core image route, not the audited viewer route of the full file
    expect(images[0].getAttribute("src")).toMatch(
        /^\/web\/image\/woow\.document\/1\/datas\/256x256(\?unique=\d+)?$/
    );
    expect(images[0].getAttribute("src")).not.toInclude("/preview");
    expect(images[0].getAttribute("loading")).toBe("lazy");
    expect(cardOf("contract.pdf").querySelector(".woow-kind-icon")).toHaveClass("fa-file-pdf-o");
    expect(cardOf("Company site").querySelector(".woow-kind-icon")).toHaveClass("fa-link");
    expect(cardOf("Signed contract").querySelector(".woow-kind-icon")).toHaveClass("fa-inbox");
    expect(cardOf("notes.txt").querySelector(".woow-kind-icon")).toHaveClass("fa-file-text-o");
    const shortcut = queryAll(".o_kanban_record").find((el) => el.querySelector(".fa-share-square-o"));
    expect(Boolean(shortcut)).toBe(true);
});

test("a card carries the marks of a starred, a locked and a versioned file", async () => {
    await mountKanban();
    const pdf = cardOf("contract.pdf");
    expect(pdf.querySelector(".fa-star")).not.toBe(null);
    expect(pdf.querySelector(".fa-lock")).not.toBe(null);
    expect(pdf.querySelector(".fa-history")).not.toBe(null);
    const image = cardOf("photo.png");
    expect(image.querySelector(".fa-star")).toBe(null);
    expect(image.querySelector(".fa-lock")).toBe(null);
});

// -- the folders ----------------------------------------------------------------------------
test("the folder cards show the top folders, and a click chooses one", async () => {
    await mountKanban();
    expect(queryAllTexts(".woow-folder-card .woow-folder-name")).toEqual(["Projects", "Empty drawer"]);
    expect(queryAllTexts(".woow-folder-card .woow-folder-count")).toEqual(["2 files here", "0 files here"]);
    expect(queryAllTexts(".woow-crumb")).toEqual(["All folders"]);
    await click(".woow-folder-card:first");
    await settle();
    expect(queryAllTexts(".woow-folder-card .woow-folder-name")).toEqual(["Alpha", "Beta"]);
    expect(queryAllTexts(".woow-folder-card .woow-folder-count")).toEqual(["1 file here", "1 file here"]);
    expect(queryAllTexts(".woow-crumb")).toEqual(["All folders", "Projects"]);
    expect(".o_search_panel_category_value header.active").toHaveText(/Projects/);
});

test("the strip of folders starts at its first card when another folder is chosen", async () => {
    await mountKanban();
    // a phone shows the cards as one scrolling row: make it so here, and scroll to the end
    const strip = queryOne(".woow-folder-cards");
    strip.style.cssText = "flex-wrap: nowrap !important; overflow-x: auto; width: 80px;";
    strip.scrollLeft = 500;
    expect(strip.scrollLeft).toBeGreaterThan(0);
    await click(".woow-folder-card:first");
    await settle();
    expect(queryAllTexts(".woow-folder-card .woow-folder-name")).toEqual(["Alpha", "Beta"]);
    expect(queryOne(".woow-folder-cards").scrollLeft).toBe(0);
});

test("each part of the path is a way back", async () => {
    await mountKanban();
    await click(".woow-folder-card:first");
    await settle();
    await click(".woow-folder-card:first");
    await settle();
    expect(queryAllTexts(".woow-crumb")).toEqual(["All folders", "Projects", "Alpha"]);
    expect(".woow-folder-card").toHaveCount(0);
    expect(".o_kanban_record:not(.o_kanban_ghost)").toHaveCount(2);
    await click(".woow-crumb:eq(1)");
    await settle();
    expect(queryAllTexts(".woow-crumb")).toEqual(["All folders", "Projects"]);
    await click(".woow-crumb:first");
    await settle();
    expect(queryAllTexts(".woow-crumb")).toEqual(["All folders"]);
    expect(".o_kanban_record:not(.o_kanban_ghost)").toHaveCount(8);
});

test("the left panel ends with a Trash entry that opens the trash", async () => {
    mockService("action", {
        doAction(action) {
            expect.step(String(action));
        },
    });
    await mountKanban();
    expect(".o_search_panel .woow-trash-entry").toHaveCount(1);
    await click(".woow-trash-entry");
    expect.verifySteps(["woow_document_management.action_woow_document_trash_documents"]);
});

// -- the selection --------------------------------------------------------------------------
test("a click selects one card, Ctrl adds one, Shift takes a range", async () => {
    await mountKanban();
    expect(".woow-selection-bar").toHaveCount(0);
    await click(cardOf("photo.png"));
    await animationFrame();
    expect(selectedNames()).toEqual(["photo.png"]);
    expect(".o_woow_selected_count").toHaveText("1 selected");
    await keyDown("ctrl");
    await click(cardOf("Company site"));
    await keyUp("ctrl");
    await animationFrame();
    expect(selectedNames()).toEqual(["photo.png", "Company site"]);
    await click(cardOf("Signed contract"));
    await animationFrame();
    expect(selectedNames()).toEqual(["Signed contract"]);
    await keyDown("shift");
    await click(cardOf("locked.txt"));
    await keyUp("shift");
    await animationFrame();
    expect(selectedNames().length).toBe(3);
    expect(".o_woow_selected_count").toHaveText("3 selected");
    await click(".woow-selection-clear");
    await animationFrame();
    expect(".woow-selected").toHaveCount(0);
    expect(".woow-selection-bar").toHaveCount(0);
});

test("a click on the empty area, or on the star, does not change the selection the same way", async () => {
    await mountKanban();
    await click(cardOf("photo.png"));
    await animationFrame();
    await click(".o_kanban_renderer");
    await animationFrame();
    expect(".woow-selected").toHaveCount(0);
});

test("the control panel shows Download and Share for one file that may be shared", async () => {
    await mountKanban();
    await click(cardOf("photo.png"));
    await animationFrame();
    expect(".woow-selection-bar .o_woow_download").toHaveCount(1);
    expect(".woow-selection-bar .o_woow_share").toHaveCount(1);
    expect(".woow-selection-bar .o_woow_actions").toHaveCount(1);
    await click(cardOf("Company site"));
    await animationFrame();
    // a link has no file to download and this user may not share it
    expect(".woow-selection-bar .o_woow_download").toHaveCount(0);
    expect(".woow-selection-bar .o_woow_share").toHaveCount(0);
});

test("the Actions menu offers what applies to the selection", async () => {
    await mountKanban();
    await click(cardOf("photo.png"));
    await settle();
    await click(".o_woow_actions");
    await animationFrame();
    expect(queryAllTexts(".o_woow_actions_menu .dropdown-item")).toEqual([
        "Rename",
        "Move",
        "Duplicate",
        "Star",
        "Create shortcut",
        "Ask for a file",
        "New version",
        "Lock",
        "Move to trash",
    ]);
});

test("the Actions menu does not offer what the user may not do", async () => {
    await mountKanban();
    await click(cardOf("notes.txt")); // read only
    await settle();
    await click(".o_woow_actions");
    await animationFrame();
    const texts = queryAllTexts(".o_woow_actions_menu .dropdown-item");
    // the folder of this file (Beta) is read only for the user: no copy can be made in it either
    for (const hidden of ["Rename", "Move", "Move to trash", "Lock", "New version", "Duplicate"]) {
        expect(texts).not.toInclude(hidden);
    }
    expect(texts).toInclude("Star");
});

test("an entry calls the server action of the selected documents", async () => {
    mockButtons();
    await mountKanban();
    await click(cardOf("photo.png"));
    await keyDown("ctrl");
    await click(cardOf("Company site"));
    await keyUp("ctrl");
    await settle();
    await click(".o_woow_actions");
    await animationFrame();
    await click(".o_woow_action_duplicate");
    await animationFrame();
    await click(".o_woow_actions");
    await animationFrame();
    await click(".o_woow_action_trash");
    await animationFrame();
    expect.verifySteps(["action_duplicate [1,3]", "action_trash [1,3]"]);
});

test("an entry that opens a wizard runs as a button of type object, so the wizard has its views", async () => {
    mockButtons();
    await mountKanban();
    await click(cardOf("photo.png"));
    await animationFrame();
    await click(".o_woow_actions");
    await animationFrame();
    await click(".o_woow_action_rename");
    await animationFrame();
    expect.verifySteps(["action_open_rename_wizard [1]"]);
});

test("the other wizard entries of the menu run as buttons of type object too", async () => {
    mockButtons();
    await mountKanban();
    await click(cardOf("contract.pdf"));
    await settle();
    for (const key of ["move", "shortcut", "new_version", "pdf_split"]) {
        await click(".o_woow_actions");
        await animationFrame();
        await click(".o_woow_action_" + key);
        await animationFrame();
    }
    expect.verifySteps([
        "action_open_move_wizard [2]",
        "action_open_shortcut_wizard [2]",
        "action_new_version [2]",
        "action_open_pdf_split_wizard [2]",
    ]);
});

test("Ask for a file opens the bound action of the documents", async () => {
    mockService("action", {
        doActionButton(params) {
            expect.step(`${params.type} ${params.name} ${JSON.stringify(params.resIds)}`);
        },
    });
    await mountKanban();
    await click(cardOf("photo.png"));
    await settle();
    await click(".o_woow_actions");
    await animationFrame();
    await click(".o_woow_action_request");
    await animationFrame();
    expect.verifySteps(["action woow_document_management.action_woow_document_request_wizard [1]"]);
});

test("the star entry stars one document after the other", async () => {
    onRpc("woow.document", "action_toggle_favorite", ({ args }) => {
        expect.step("toggle " + JSON.stringify(args[0]));
        return true;
    });
    await mountKanban();
    await click(cardOf("photo.png"));
    await keyDown("ctrl");
    await click(cardOf("Company site"));
    await keyUp("ctrl");
    await animationFrame();
    await click(".o_woow_actions");
    await animationFrame();
    await click(".o_woow_action_star");
    await animationFrame();
    expect.verifySteps(["toggle [1]", "toggle [3]"]);
});

test("Download goes to the download route for one file and to the ZIP route for several", async () => {
    const urls = [];
    const onClick = (ev) => {
        const link = ev.target.closest && ev.target.closest("a[href]");
        if (link) {
            ev.preventDefault();
            urls.push(link.getAttribute("href"));
        }
    };
    document.addEventListener("click", onClick, true);
    try {
        await mountKanban();
        await click(cardOf("photo.png"));
        await animationFrame();
        await click(".o_woow_download");
        await keyDown("ctrl");
        await click(cardOf("contract.pdf"));
        await keyUp("ctrl");
        await animationFrame();
        await click(".o_woow_download");
    } finally {
        document.removeEventListener("click", onClick, true);
    }
    expect(urls).toEqual([
        "/woow_document/doc/1/download",
        "/woow_document/zip?document_ids=1%2C2",
    ]);
});

test("Share calls the share dialog of the server for one document", async () => {
    mockButtons();
    await mountKanban();
    await click(cardOf("photo.png"));
    await animationFrame();
    await click(".o_woow_share");
    await animationFrame();
    expect.verifySteps(["action_open_share_dialog [1]"]);
});

// -- opening ---------------------------------------------------------------------------------
test("a double click on an image opens the file viewer with the other files", async () => {
    await mountKanban();
    await dblclick(cardOf("photo.png"));
    await animationFrame();
    expect(".o-FileViewer").toHaveCount(1);
    // photo.png, contract.pdf, notes.txt and locked.txt can be viewed: previous / next work
    expect(".o-FileViewer-navigation").toHaveCount(2);
    expect(".o-FileViewer-viewImage").toHaveCount(1);
    await press("Escape");
    await animationFrame();
    expect(".o-FileViewer").toHaveCount(0);
});

test("Enter opens the focused card", async () => {
    await mountKanban();
    cardOf("photo.png").focus();
    await press("Enter");
    await animationFrame();
    expect(".o-FileViewer").toHaveCount(1);
});

test("a link opens in a new tab without an opener, and an unsafe one does not open", async () => {
    patchWithCleanup(browser, {
        open: (...args) => expect.step("open " + args.join(" ")),
    });
    await mountKanban();
    await dblclick(cardOf("Company site"));
    await animationFrame();
    expect.verifySteps(["open https://example.com/about _blank noopener,noreferrer"]);
});

test("a shortcut opens its target through the method of the server", async () => {
    mockButtons();
    await mountKanban();
    const shortcut = queryAll(".o_kanban_record").find((el) => el.querySelector(".fa-share-square-o"));
    await dblclick(shortcut);
    await animationFrame();
    expect.verifySteps(["action_open_shortcut_target [4]"]);
});

test("a requested file opens its upload wizard", async () => {
    mockButtons();
    await mountKanban();
    await dblclick(cardOf("Signed contract"));
    await animationFrame();
    expect.verifySteps(["action_open_fulfil_wizard [5]"]);
});

// -- dropping files --------------------------------------------------------------------------
function dropFiles(files) {
    const transfer = new DataTransfer();
    for (const file of files) {
        transfer.items.add(file);
    }
    queryOne(".o_content").dispatchEvent(
        new DragEvent("drop", { dataTransfer: transfer, bubbles: true, cancelable: true })
    );
}

function patchUpload(answers) {
    const calls = [];
    const original = browser.fetch;
    patchWithCleanup(browser, {
        fetch: async (url, options) => {
            if (url !== "/woow_document/upload") {
                return original(url, options);
            }
            const file = options.body.get("files");
            calls.push({ url, folder: options.body.get("folder_id"), name: file.name });
            const answer = answers[file.name] || { status: 200, body: { ok: true } };
            return new Response(JSON.stringify(answer.body), {
                status: answer.status,
                headers: { "Content-Type": "application/json" },
            });
        },
    });
    return calls;
}

test("dropping files without a chosen folder is refused with a message", async () => {
    mockService("notification", {
        add(message) {
            expect.step(String(message));
        },
    });
    const calls = patchUpload({});
    await mountKanban();
    dropFiles([new File(["x"], "a.txt", { type: "text/plain" })]);
    await animationFrame();
    expect.verifySteps(["Choose a folder in the left panel, then drop the files again."]);
    expect(calls).toHaveLength(0);
});

test("dropping files in a folder without upload level is refused with a message", async () => {
    mockService("notification", {
        add(message) {
            expect.step(String(message));
        },
    });
    const calls = patchUpload({});
    await mountKanban();
    await click(".woow-folder-card:first");
    await settle();
    await click(".woow-folder-card:eq(1)"); // Beta, where the user may only read
    await settle();
    dropFiles([new File(["x"], "a.txt", { type: "text/plain" })]);
    await animationFrame();
    await animationFrame();
    expect.verifySteps(["You cannot add files to this folder."]);
    expect(calls).toHaveLength(0);
});

test("dropped files go to the chosen folder one by one and the refusals are shown per file", async () => {
    const calls = patchUpload({
        "bad.exe": { status: 400, body: { ok: false, message: "Files like this are refused." } },
    });
    await mountKanban();
    await click(".woow-folder-card:first");
    await settle();
    await click(".woow-folder-card:first"); // Alpha, where the user may upload
    await settle();
    dropFiles([
        new File(["one"], "one.txt", { type: "text/plain" }),
        new File(["MZ"], "bad.exe", { type: "application/octet-stream" }),
        new File(["two"], "two.txt", { type: "text/plain" }),
    ]);
    await animationFrame();
    await animationFrame();
    await animationFrame();
    expect(calls.map((call) => call.name)).toEqual(["one.txt", "bad.exe", "two.txt"]);
    expect(calls.every((call) => call.url === "/woow_document/upload" && call.folder === "2")).toBe(true);
    expect(".woow-upload-status strong:first").toHaveText("2 of 3 files uploaded");
    expect(queryAllTexts(".woow-upload-errors li")).toEqual(["bad.exe: Files like this are refused."]);
    await click(".woow-upload-status .btn-close");
    await animationFrame();
    expect(".woow-upload-status").toHaveCount(0);
});

function dropOn(element, files) {
    const transfer = new DataTransfer();
    for (const file of files) {
        transfer.items.add(file);
    }
    element.dispatchEvent(
        new DragEvent("drop", { dataTransfer: transfer, bubbles: true, cancelable: true })
    );
}

test("files dropped on a folder card go into that folder, not into the chosen one", async () => {
    const calls = patchUpload({});
    await mountKanban();
    await click(".woow-folder-card:first"); // Projects is chosen; Alpha and Beta are its cards
    await settle();
    const alpha = queryAll(".woow-folder-card").find((el) => el.textContent.includes("Alpha"));
    dropOn(alpha, [new File(["x"], "on-alpha.txt", { type: "text/plain" })]);
    await settle();
    await settle();
    expect(calls.map((call) => `${call.name} ${call.folder}`)).toEqual(["on-alpha.txt 2"]);
    // the banner names the folder that received the files
    expect(".woow-upload-status .woow-upload-folder").toHaveText("Alpha");
});

test("files dropped on a folder of the tree go into that folder", async () => {
    const calls = patchUpload({});
    await mountKanban();
    await click(".woow-folder-card:first");
    await settle();
    // the chosen folder opens its children in the tree on a second click
    await click(".o_search_panel_category_value header.active");
    await settle();
    const alpha = queryAll(".o_search_panel_category_value header").find((el) =>
        el.textContent.includes("Alpha")
    );
    dropOn(alpha, [new File(["x"], "on-tree.txt", { type: "text/plain" })]);
    await settle();
    await settle();
    expect(calls.map((call) => `${call.name} ${call.folder}`)).toEqual(["on-tree.txt 2"]);
});

test("files dropped on a folder without upload level are refused, whatever folder is chosen", async () => {
    mockService("notification", {
        add(message) {
            expect.step(String(message));
        },
    });
    const calls = patchUpload({});
    await mountKanban();
    await click(".woow-folder-card:first");
    await settle();
    const beta = queryAll(".woow-folder-card").find((el) => el.textContent.includes("Beta"));
    dropOn(beta, [new File(["x"], "on-beta.txt", { type: "text/plain" })]);
    await settle();
    expect.verifySteps(["You cannot add files to this folder."]);
    expect(calls).toHaveLength(0);
});

test("a folder with more sub-folders than cards says so", async () => {
    onRpc("woow.document.folder", "search_read", () =>
        Array.from({ length: 61 }, (_, index) => ({
            id: 100 + index,
            name: "Folder " + index,
            document_count: 0,
            subfolder_count: 0,
            color: 0,
        }))
    );
    await mountKanban();
    expect(".woow-folder-card").toHaveCount(60);
    expect(".woow-folder-more").toHaveCount(1);
});

test("the numbers of the folder tree are read again after files were dropped", async () => {
    patchUpload({});
    const counted = [];
    onRpc("woow.document", "search_panel_select_range", () => {
        counted.push(1);
    });
    await mountKanban();
    await click(".woow-folder-card:first");
    await settle();
    await click(".woow-folder-card:first"); // Alpha
    await settle();
    const before = counted.length;
    dropFiles([new File(["x"], "count.txt", { type: "text/plain" })]);
    await settle();
    await settle();
    expect(counted.length).toBeGreaterThan(before);
});

test("a drop that carries no file is ignored", async () => {
    const calls = patchUpload({});
    await mountKanban();
    const transfer = new DataTransfer();
    transfer.setData("text/plain", "just text");
    queryOne(".o_content").dispatchEvent(
        new DragEvent("drop", { dataTransfer: transfer, bubbles: true, cancelable: true })
    );
    await animationFrame();
    expect(calls).toHaveLength(0);
});

// -- dragging a card onto a folder ----------------------------------------------------------
function dragEvent(type, transfer) {
    return new DragEvent(type, { dataTransfer: transfer, bubbles: true, cancelable: true });
}

test("a card dragged onto a folder card is moved through the move wizard", async () => {
    onRpc("woow.document.move.wizard", "create", ({ args }) => {
        expect.step("wizard " + JSON.stringify(args[0][0]));
    });
    onRpc("woow.document.move.wizard", "action_move", () => {
        expect.step("move");
        return { type: "ir.actions.act_window_close" };
    });
    await mountKanban();
    await click(".woow-folder-card:first");
    await settle();
    const card = cardOf("Company site");
    const target = queryAll(".woow-folder-card")[0]; // Alpha
    const transfer = new DataTransfer();
    card.dispatchEvent(dragEvent("dragstart", transfer));
    await animationFrame();
    target.dispatchEvent(dragEvent("dragover", transfer));
    await animationFrame();
    expect(target).toHaveClass("woow-drop-target");
    target.dispatchEvent(dragEvent("drop", transfer));
    await animationFrame();
    await animationFrame();
    expect.verifySteps([
        'wizard {"document_ids":[[6,0,[3]]],"target_folder_id":2}',
        "move",
    ]);
});

test("a card that the user may not move is not dragged", async () => {
    mockService("notification", {
        add(message) {
            expect.step(String(message));
        },
    });
    await mountKanban();
    const transfer = new DataTransfer();
    const event = dragEvent("dragstart", transfer);
    cardOf("notes.txt").dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect.verifySteps(["You cannot move this selection."]);
});

// -- the details panel ----------------------------------------------------------------------
class StubInspector extends Component {
    static props = ["*"];
    static template = xml`
        <div class="stub-inspector">
            <span class="stub-res-id" t-out="props.resModel + ':' + props.resId"/>
            <button class="stub-close" t-on-click="props.onClose">x</button>
            <button class="stub-changed" t-on-click="props.onChanged">y</button>
        </div>`;
}

// The lane of the details panel registers the real component in the same category; these tests
// stand for the cases with and without it, so they start from an empty category (the registries
// of a test are its own, nothing leaks into the next test).
function useDetailsPanel(component) {
    const category = registry.category("woow_document_components");
    if (category.contains("inspector")) {
        category.remove("inspector");
    }
    if (component) {
        category.add("inspector", component);
    }
}

test("without a details panel there is no info button", async () => {
    useDetailsPanel(null);
    await mountKanban();
    expect(".o_woow_info_toggle").toHaveCount(0);
});

test("the info button shows the details panel with the selected record", async () => {
    useDetailsPanel(StubInspector);
    await mountKanban();
    expect(".stub-inspector").toHaveCount(0);
    await click(".o_woow_info_toggle");
    await animationFrame();
    expect(".stub-res-id").toHaveText("woow.document:false");
    await click(cardOf("photo.png"));
    await animationFrame();
    expect(".stub-res-id").toHaveText("woow.document:1");
    await keyDown("ctrl");
    await click(cardOf("Company site"));
    await keyUp("ctrl");
    await animationFrame();
    expect(".stub-res-id").toHaveText("woow.document:false");
    await click(".stub-close");
    await animationFrame();
    expect(".stub-inspector").toHaveCount(0);
});

test("a change made in the details panel reloads the records", async () => {
    useDetailsPanel(StubInspector);
    onRpc("woow.document", "web_search_read", () => {
        expect.step("load");
    });
    await mountKanban();
    expect.verifySteps(["load"]);
    await click(".o_woow_info_toggle");
    await animationFrame();
    await click(".stub-changed");
    await animationFrame();
    expect.verifySteps(["load"]);
    expect(".stub-inspector").toHaveCount(1);
});

// -- the list --------------------------------------------------------------------------------
test("the list shows the strip of folders and selects a row with its check box", async () => {
    await mountList();
    expect(".woow-folder-card").toHaveCount(2);
    expect(".woow-crumb").toHaveCount(1);
    await click(".o_data_row:first .o_list_record_selector input");
    await animationFrame();
    expect(".o_data_row.o_data_row_selected").toHaveCount(1);
    expect(".woow-selection-bar").toHaveCount(1);
    expect(".woow-selection-bar .o_woow_actions").toHaveCount(1);
});

test("a click on a row of the list does not select it: the row keeps its standard behaviour", async () => {
    await mountList();
    await click(".o_data_row:first td[name=name]");
    await animationFrame();
    expect(".woow-selection-bar").toHaveCount(0);
    expect(".o_data_row.o_data_row_selected").toHaveCount(0);
});

test("the Actions menu of the list keeps what the standard menu of the selection had", async () => {
    mockService("action", {
        doActionButton(params) {
            expect.step(`${params.type} ${params.name}`);
        },
    });
    await mountList();
    await click(".o_data_row:first .o_list_record_selector input");
    await settle();
    await click(".o_woow_actions");
    await animationFrame();
    expect(queryAllTexts(".o_woow_actions_menu .dropdown-item")).toInclude("Ask for a file");
    await click(".o_woow_action_request");
    await animationFrame();
    expect.verifySteps(["action woow_document_management.action_woow_document_request_wizard"]);
});

test("the Open button of a selected row shows the file in the viewer", async () => {
    await mountList();
    await click(".o_data_row:first .o_list_record_selector input");
    await animationFrame();
    await click(".woow-selection-bar .o_woow_open");
    await animationFrame();
    expect(".o-FileViewer").toHaveCount(1);
});

test("the list has the Actions menu and the Download of the selection", async () => {
    await mountList();
    await click(".o_data_row:first .o_list_record_selector input");
    await click(".o_data_row:eq(1) .o_list_record_selector input");
    await animationFrame();
    expect(".woow-selection-bar .o_woow_download").toHaveCount(1);
    await click(".o_woow_actions");
    await animationFrame();
    expect(queryAllTexts(".o_woow_actions_menu .dropdown-item")).toInclude("Duplicate");
});

test("the list takes dropped files into the chosen folder", async () => {
    const calls = patchUpload({});
    await mountList();
    await click(".woow-folder-card:first");
    await settle();
    await click(".woow-folder-card:first");
    await settle();
    dropFiles([new File(["one"], "one.txt", { type: "text/plain" })]);
    await animationFrame();
    await animationFrame();
    expect(calls.map((call) => call.folder)).toEqual(["2"]);
});

test("a name with markup is shown as text and builds no element", async () => {
    await mountKanban();
    const card = cardOf("<b>bold</b>");
    expect(Boolean(card)).toBe(true);
    expect(card.querySelector("b")).toBe(null);
    expect(card.querySelector("img[src='x']")).toBe(null);
    expect(card.textContent).toInclude("<img src=x onerror=alert(1)>");
});
