// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

import { describe, expect, test } from "@odoo/hoot";
import { makeMockEnv } from "@web/../tests/web_test_helpers";
import {
    availableActions,
    baseMimetype,
    clickSelection,
    documentKind,
    downloadOf,
    hasPicture,
    kindIcon,
    previewUrl,
    safeLinkUrl,
    thumbnailUrl,
    toItem,
    uploadRefusal,
    viewerFile,
    zipUrl,
} from "@woow_document_management/backend/wsui/workspace_utils";

describe.current.tags("desktop");

const doc = (values = {}) => ({
    id: 1,
    name: "a.txt",
    documentType: "binary",
    mimetype: "text/plain",
    url: "",
    folderId: 5,
    isFavorite: false,
    isLocked: false,
    lockedByOther: false,
    canWrite: true,
    canShare: false,
    canUpload: true,
    versionCount: 0,
    ...values,
});

test("the kind of a document follows its type and its mimetype", () => {
    expect(documentKind(doc({ documentType: "url" }))).toBe("link");
    expect(documentKind(doc({ documentType: "shortcut", mimetype: "" }))).toBe("shortcut");
    expect(documentKind(doc({ documentType: "request", mimetype: "" }))).toBe("request");
    expect(documentKind(doc({ mimetype: "application/pdf" }))).toBe("pdf");
    expect(documentKind(doc({ mimetype: "application/pdf; charset=binary" }))).toBe("pdf");
    expect(documentKind(doc({ mimetype: "image/png" }))).toBe("image");
    expect(documentKind(doc({ mimetype: "text/csv" }))).toBe("sheet");
    expect(documentKind(doc({ mimetype: "application/zip" }))).toBe("archive");
    expect(documentKind(doc({ mimetype: "text/plain" }))).toBe("text");
    expect(documentKind(doc({ mimetype: "application/octet-stream" }))).toBe("file");
    expect(documentKind(doc({ mimetype: "" }))).toBe("file");
    expect(kindIcon("pdf")).toBe("fa-file-pdf-o");
    expect(kindIcon("link")).toBe("fa-link");
    expect(kindIcon("shortcut")).toBe("fa-share-square-o");
    expect(kindIcon("request")).toBe("fa-inbox");
    expect(kindIcon("nothing")).toBe("fa-file-o");
    expect(baseMimetype(" Text/Plain ; charset=utf-8")).toBe("text/plain");
});

test("only the images that the preview route serves get a picture", () => {
    expect(hasPicture(doc({ mimetype: "image/png" }))).toBe(true);
    expect(hasPicture(doc({ mimetype: "image/jpeg" }))).toBe(true);
    expect(hasPicture(doc({ mimetype: "image/svg+xml" }))).toBe(false);
    expect(hasPicture(doc({ mimetype: "application/pdf" }))).toBe(false);
    expect(hasPicture(doc({ mimetype: "image/png", documentType: "shortcut" }))).toBe(false);
});

test("the URLs are those of this module and encode the ids", () => {
    expect(previewUrl(12)).toBe("/woow_document/doc/12/preview");
    expect(previewUrl("1/../2")).toBe("/woow_document/doc/1%2F..%2F2/preview");
    expect(zipUrl([1, 2, 3])).toBe("/woow_document/zip?document_ids=1%2C2%2C3");
});

test("the picture of a card is a small copy through the core image route", () => {
    expect(thumbnailUrl(12, "1700000000000")).toBe(
        "/web/image/woow.document/12/datas/256x256?unique=1700000000000"
    );
    expect(thumbnailUrl(12)).toBe("/web/image/woow.document/12/datas/256x256");
    expect(thumbnailUrl("1/../2", "a&b")).toBe(
        "/web/image/woow.document/1%2F..%2F2/datas/256x256?unique=a%26b"
    );
    // never the audited viewer route, which writes a "Previewed" row for each request
    expect(thumbnailUrl(12, "1")).not.toInclude("/woow_document/");
});

test("a link opens only when it is a plain web or mail address", () => {
    expect(safeLinkUrl("https://example.com/a?b=c")).toBe("https://example.com/a?b=c");
    expect(safeLinkUrl("http://example.com")).toBe("http://example.com/");
    expect(safeLinkUrl("mailto:someone@example.com")).toBe("mailto:someone@example.com");
    for (const bad of [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<b>x</b>",
        "vbscript:x",
        "file:///etc/passwd",
        "//example.com",
        "/relative/path",
        "example.com",
        "https://user:pw@example.com/",
        "https://exa mple.com/",
        "https://example.com/\u0000",
        "https://example.com/\u202e",
        "mailto:",
        "",
        null,
        undefined,
        42,
    ]) {
        expect(safeLinkUrl(bad)).toBe(null, { message: `refused: ${String(bad)}` });
    }
    expect(safeLinkUrl("https://example.com/" + "a".repeat(2100))).toBe(null);
});

test("the viewer takes images, PDF files and plain text, and nothing else", () => {
    const image = viewerFile(doc({ id: 7, mimetype: "image/webp", name: "x.webp" }));
    expect(image.isViewable).toBe(true);
    expect(image.isImage).toBe(true);
    expect(image.defaultSource).toBe("/woow_document/doc/7/preview");
    expect(image.downloadUrl).toBe("/woow_document/doc/7/download");
    const pdf = viewerFile(doc({ id: 8, mimetype: "application/pdf" }));
    expect(pdf.isPdf).toBe(true);
    expect(pdf.defaultSource).toBe(
        "/web/static/lib/pdfjs/web/viewer.html?file=" +
            encodeURIComponent("/woow_document/doc/8/preview") +
            "#pagemode=none"
    );
    const text = viewerFile(doc({ id: 9, mimetype: "text/plain" }));
    expect(text.isText).toBe(true);
    expect(viewerFile(doc({ mimetype: "application/zip" })).isViewable).toBe(false);
    expect(viewerFile(doc({ documentType: "url" }))).toBe(null);
});

test("a record becomes an item with plain values", () => {
    const item = toItem({
        resId: 3,
        data: {
            name: "n",
            document_type: "url",
            mimetype: false,
            url: "https://example.com",
            folder_id: [5, "F"],
            is_favorite: true,
            woow_user_can_write: true,
            version_count: 2,
        },
    });
    expect(item).toEqual({
        id: 3,
        name: "n",
        documentType: "url",
        mimetype: "",
        url: "https://example.com",
        folderId: 5,
        isFavorite: true,
        isLocked: false,
        lockedByOther: false,
        canWrite: true,
        canShare: false,
        canUpload: false,
        versionCount: 2,
        writeStamp: "",
    });
    // the write date of the record is the cache key of the picture
    const stamped = toItem({ resId: 4, data: { write_date: { toMillis: () => 1700000000000 } } });
    expect(stamped.writeStamp).toBe("1700000000000");
});

test("the download is the download route for one file and the ZIP route for several", () => {
    expect(downloadOf([])).toBe(null);
    expect(downloadOf([doc({ documentType: "url" })])).toBe(null);
    expect(downloadOf([doc({ id: 4 })])).toEqual({
        url: "/woow_document/doc/4/download",
        count: 1,
    });
    expect(downloadOf([doc({ id: 4 }), doc({ id: 5 }), doc({ id: 6, documentType: "url" })])).toEqual({
        url: "/woow_document/zip?document_ids=4%2C5",
        count: 2,
    });
});

const keysOf = (items) => availableActions(items).map((entry) => entry.key);

test("no selection offers no action", () => {
    expect(keysOf([])).toEqual([]);
});

test("one writable file offers the actions of a file", () => {
    expect(keysOf([doc()])).toEqual([
        "rename",
        "move",
        "duplicate",
        "star",
        "shortcut",
        "request",
        "new_version",
        "lock",
        "trash",
    ]);
});

test("a PDF file adds the split action, several of them the merge action", () => {
    const pdf = (id) => doc({ id, mimetype: "application/pdf" });
    expect(keysOf([pdf(1)])).toInclude("pdf_split");
    expect(keysOf([pdf(1)])).not.toInclude("pdf_merge");
    expect(keysOf([pdf(1), pdf(2)])).toInclude("pdf_merge");
    expect(keysOf([pdf(1), pdf(2)])).not.toInclude("pdf_split");
    expect(keysOf([pdf(1), doc({ id: 2 })])).not.toInclude("pdf_merge");
});

test("what the user may not do is not offered", () => {
    const readOnly = keysOf([doc({ canWrite: false })]);
    for (const key of ["rename", "move", "new_version", "lock", "trash"]) {
        expect(readOnly).not.toInclude(key);
    }
    expect(readOnly).toInclude("duplicate");
    // a copy is made in the same folder: the user needs the upload level there
    expect(keysOf([doc({ canUpload: false })])).not.toInclude("duplicate");
    expect(keysOf([doc({ id: 1 }), doc({ id: 2, canUpload: false })])).not.toInclude("duplicate");
    // a shortcut is made in a folder that is chosen in the dialog: reading the file is enough
    expect(keysOf([doc({ canUpload: false })])).toInclude("shortcut");
    const lockedByOther = keysOf([doc({ isLocked: true, lockedByOther: true })]);
    for (const key of ["rename", "move", "new_version", "lock", "trash"]) {
        expect(lockedByOther).not.toInclude(key);
    }
    expect(lockedByOther).toInclude("unlock");
});

test("lock and unlock follow the state of the lock", () => {
    expect(keysOf([doc()])).toInclude("lock");
    expect(keysOf([doc()])).not.toInclude("unlock");
    const mine = keysOf([doc({ isLocked: true })]);
    expect(mine).toInclude("unlock");
    expect(mine).not.toInclude("lock");
    const mixed = availableActions([doc({ id: 1 }), doc({ id: 2, isLocked: true })]);
    expect(mixed.map((entry) => entry.key)).not.toInclude("lock");
    expect(mixed.find((entry) => entry.key === "unlock").ids).toEqual([2]);
});

test("links, shortcuts and requests offer what fits them", () => {
    expect(keysOf([doc({ documentType: "url" })])).toInclude("shortcut");
    expect(keysOf([doc({ documentType: "url" })])).not.toInclude("new_version");
    const shortcut = keysOf([doc({ documentType: "shortcut" })]);
    for (const key of ["duplicate", "shortcut", "new_version", "lock"]) {
        expect(shortcut).not.toInclude(key);
    }
    expect(shortcut).toInclude("rename");
    expect(keysOf([doc({ documentType: "request" })])).not.toInclude("duplicate");
});

test("the star entry stars what is not starred and removes what is", async () => {
    await makeMockEnv();
    const first = availableActions([doc({ id: 1 }), doc({ id: 2, isFavorite: true })]);
    const star = first.find((entry) => entry.key === "star");
    expect(String(star.label)).toBe("Star");
    expect(star.ids).toEqual([1]);
    expect(star.each).toBe(true);
    const second = availableActions([doc({ id: 1, isFavorite: true })]);
    expect(String(second.find((entry) => entry.key === "star").label)).toBe("Remove star");
    expect(second.find((entry) => entry.key === "star").ids).toEqual([1]);
});

test("duplicate is offered for at most twenty documents", () => {
    const many = Array.from({ length: 21 }, (_, index) => doc({ id: index + 1 }));
    expect(keysOf(many)).not.toInclude("duplicate");
    expect(keysOf(many.slice(0, 20))).toInclude("duplicate");
});

test("a click, Ctrl and Shift change the selection as in a file manager", () => {
    const order = [1, 2, 3, 4, 5];
    let selection = { ids: [], anchor: false };
    selection = clickSelection(selection, 2, order);
    expect(selection).toEqual({ ids: [2], anchor: 2 });
    selection = clickSelection(selection, 4, order, { ctrl: true });
    expect(selection).toEqual({ ids: [2, 4], anchor: 4 });
    selection = clickSelection(selection, 2, order, { ctrl: true });
    expect(selection).toEqual({ ids: [4], anchor: 2 });
    selection = clickSelection({ ids: [2], anchor: 2 }, 5, order, { shift: true });
    expect(selection.ids).toEqual([2, 3, 4, 5]);
    expect(selection.anchor).toBe(2);
    selection = clickSelection({ ids: [4], anchor: 4 }, 2, order, { shift: true });
    expect(selection.ids).toEqual([2, 3, 4]);
    selection = clickSelection({ ids: [1], anchor: 1 }, 3, order, { shift: true, ctrl: true });
    expect(selection.ids).toEqual([1, 2, 3]);
    // an anchor that is not shown any more is not used
    selection = clickSelection({ ids: [9], anchor: 9 }, 3, order, { shift: true });
    expect(selection).toEqual({ ids: [3], anchor: 3 });
    // ids that are not shown are dropped
    selection = clickSelection({ ids: [9, 1], anchor: 1 }, 2, order, { ctrl: true });
    expect(selection.ids).toEqual([1, 2]);
});

test("the refusal of an upload is the server's message, or a short one by status", async () => {
    await makeMockEnv();
    expect(uploadRefusal(400, { message: "Not allowed." })).toBe("Not allowed.");
    expect(String(uploadRefusal(413, null))).toBe("The file is too large.");
    expect(String(uploadRefusal(503, null))).toBe("The server is busy. Try again in a moment.");
    expect(String(uploadRefusal(404, {}))).toBe("The folder is not available.");
    expect(String(uploadRefusal(500, null))).toBe("The file could not be uploaded.");
});
