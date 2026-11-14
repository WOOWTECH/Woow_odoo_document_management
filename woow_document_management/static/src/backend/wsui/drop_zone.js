// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// Dropping files from the computer onto the content area of the workspace.
import { useEffect } from "@odoo/owl";

function carriesFiles(ev) {
    const types = ev.dataTransfer && ev.dataTransfer.types;
    return Boolean(types) && Array.from(types).includes("Files");
}

/**
 * What a drop holds: the files, and how many of the dropped entries were folders (those cannot be
 * sent as a file and are reported instead).
 */
export function droppedFiles(dataTransfer) {
    const files = [];
    let skipped = 0;
    const items = Array.from(dataTransfer.items || []);
    if (items.length) {
        for (const item of items) {
            if (item.kind !== "file") {
                continue;
            }
            const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
            if (entry && entry.isDirectory) {
                skipped++;
                continue;
            }
            const file = item.getAsFile();
            if (file) {
                files.push(file);
            }
        }
        return { files, skipped };
    }
    return { files: Array.from(dataTransfer.files || []), skipped };
}

/**
 * Listens for dropped files on the element that `getElement()` returns.
 * @param {Object} workspace the workspace of the view (dropFiles, state.dropping)
 * @param {function(): HTMLElement|null} getElement
 */
export function useDropZone(workspace, getElement) {
    useEffect(
        (element) => {
            if (!element) {
                return;
            }
            let depth = 0;
            const onEnter = (ev) => {
                if (!carriesFiles(ev)) {
                    return;
                }
                ev.preventDefault();
                depth++;
                workspace.state.dropping = true;
            };
            const onOver = (ev) => {
                if (!carriesFiles(ev)) {
                    return;
                }
                ev.preventDefault();
                ev.dataTransfer.dropEffect = "copy";
            };
            const onLeave = (ev) => {
                if (!carriesFiles(ev)) {
                    return;
                }
                depth = Math.max(0, depth - 1);
                if (!depth) {
                    workspace.state.dropping = false;
                }
            };
            const onDrop = (ev) => {
                if (!carriesFiles(ev)) {
                    return;
                }
                ev.preventDefault();
                depth = 0;
                workspace.state.dropping = false;
                const { files, skipped } = droppedFiles(ev.dataTransfer);
                workspace.dropFiles(files, { skipped });
            };
            element.addEventListener("dragenter", onEnter);
            element.addEventListener("dragover", onOver);
            element.addEventListener("dragleave", onLeave);
            element.addEventListener("drop", onDrop);
            return () => {
                element.removeEventListener("dragenter", onEnter);
                element.removeEventListener("dragover", onOver);
                element.removeEventListener("dragleave", onLeave);
                element.removeEventListener("drop", onDrop);
            };
        },
        () => [getElement()]
    );
}
