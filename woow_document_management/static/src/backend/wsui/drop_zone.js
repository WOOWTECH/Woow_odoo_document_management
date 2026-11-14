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
 * The folder under the pointer: a folder card or a folder of the left panel carries its id in
 * `data-woow-folder-id`. Anything else (the files, the empty area) is not a folder.
 * @returns {number|false}
 */
export function folderUnder(target) {
    const holder = target && target.closest ? target.closest("[data-woow-folder-id]") : null;
    const id = holder ? Number.parseInt(holder.dataset.woowFolderId, 10) : NaN;
    return Number.isInteger(id) && id > 0 ? id : false;
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
                // the folder under the pointer receives the files: show which one it is
                workspace.state.dropFolderId = folderUnder(ev.target);
            };
            const onLeave = (ev) => {
                if (!carriesFiles(ev)) {
                    return;
                }
                depth = Math.max(0, depth - 1);
                if (!depth) {
                    workspace.state.dropping = false;
                    workspace.state.dropFolderId = false;
                }
            };
            const onDrop = (ev) => {
                if (!carriesFiles(ev)) {
                    return;
                }
                ev.preventDefault();
                depth = 0;
                workspace.state.dropping = false;
                const folderId = folderUnder(ev.target);
                workspace.state.dropFolderId = false;
                const { files, skipped } = droppedFiles(ev.dataTransfer);
                workspace.dropFiles(files, { skipped, folderId });
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
