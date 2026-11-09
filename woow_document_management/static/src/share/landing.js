/* Copyright 2026 WoowTech
 * License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
 */
(() => {
    "use strict";
    const code = window.location.hash.slice(1);
    if (!/^[A-Za-z0-9_-]{43}$/.test(code)) {
        return;
    }
    window.history.replaceState(null, "", "/woow-docs/s");
    const form = document.querySelector('form[action="/woow-docs/s/open"]');
    if (!form) {
        return;
    }
    const field = form.querySelector('input[name="link"]');
    if (!field) {
        return;
    }
    field.value = code;
    form.submit();
})();
