// Copyright 2026 WoowTech
// License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
/** @odoo-module **/

// The details panel on a page of its own (the "Details" button of the document form). Screens that
// have room for a side panel look the component up themselves; this page does the same, so that it
// shows exactly what such a screen shows.
import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";

export class WoowDocumentInspectorPage extends Component {
    static template = "woow_document_management.InspectorPage";
    static props = { ...standardActionServiceProps };

    setup() {
        this.action = useService("action");
    }

    get inspector() {
        return registry.category("woow_document_components").get("inspector", null);
    }

    get resId() {
        const action = this.props.action;
        const fromParams = action.params && action.params.res_id;
        const fromContext = action.context && action.context.active_id;
        const id = Number(fromParams || fromContext);
        return Number.isSafeInteger(id) && id > 0 ? id : false;
    }

    close() {
        return this.action.restore();
    }
}

registry.category("actions").add("woow_document_inspector", WoowDocumentInspectorPage);
