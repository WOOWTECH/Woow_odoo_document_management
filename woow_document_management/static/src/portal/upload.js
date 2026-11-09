/** @odoo-module **/
// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches("[data-woow-doc-upload]")) {
        return;
    }
    event.preventDefault();
    const result = form.querySelector("[data-woow-upload-result]");
    const button = form.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    try {
        const response = await fetch(form.action, {
            method: "POST", credentials: "same-origin", body: new FormData(form),
            headers: { Accept: "application/json" },
        });
        const data = await response.json();
        if (result) result.textContent = data.ok ? "Upload complete." : "Upload failed.";
        if (data.ok) window.location.reload();
    } catch {
        if (result) result.textContent = "Upload failed.";
    } finally {
        if (button) button.disabled = false;
    }
});
