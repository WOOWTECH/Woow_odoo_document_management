/** @odoo-module **/
// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import { Component, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";

export class ShareResult extends Component {
    static template = "woow_document_management.ShareResult";
    static props = { ...standardActionServiceProps };

    setup() {
        // Keep the URLs only in this action instance. No browser storage or logging.
        const params = this.props.action.params || {};
        this.links = Array.isArray(params.links) ? params.links.filter(
            (value) => typeof value === "string" && /^https?:\/\//.test(value)
        ) : [];
        delete params.links;
        onWillUnmount(() => { this.links = []; });
    }

    async copyLink(link) {
        await navigator.clipboard.writeText(link);
    }

}

registry.category("actions").add("woow_document_share_result", ShareResult);
