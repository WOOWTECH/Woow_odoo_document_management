// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// T-CA-21: exercise Odoo's backend FileViewer with this addon's preview route.
import { createFileViewer } from "@web/core/file_viewer/file_viewer_hook";
import { registry } from "@web/core/registry";

let viewer;

registry.category("web_tour.tours").add("tour_ca21_fileviewer_custom_url", {
    steps: () => [
        {
            trigger: ".o_main_navbar",
            async run() {
                const id = Number(window.__ca21DocId);
                if (!Number.isSafeInteger(id) || id <= 0) {
                    throw new Error("CA-21 fixture document ID is missing");
                }
                const previewUrl = `/woow_document/doc/${id}/preview`;
                const response = await fetch(previewUrl, { credentials: "same-origin" });
                if (!response.ok || (await response.text()) !== "ca21-preview-body") {
                    throw new Error("CA-21 preview route did not serve the fixture");
                }
                viewer = createFileViewer();
                viewer.open({
                    displayName: "ca21.txt",
                    downloadUrl: `/woow_document/doc/${id}/download`,
                    defaultSource: previewUrl,
                    isText: true,
                    isViewable: true,
                    mimetype: "text/plain",
                });
            },
        },
        {
            trigger: ".o-FileViewer iframe.o-isText",
            run() {
                const id = Number(window.__ca21DocId);
                const frame = document.querySelector(".o-FileViewer iframe.o-isText");
                if (frame.getAttribute("src") !== `/woow_document/doc/${id}/preview`) {
                    throw new Error("CA-21 FileViewer did not use the custom preview URL");
                }
                viewer.close();
            },
        },
    ],
});
