/** @odoo-module **/

import { registry } from "@web/core/registry";
import { CheckBox } from "@web/core/checkbox/checkbox";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import {
    BooleanToggleField,
    booleanToggleField,
} from "@web/views/fields/boolean_toggle/boolean_toggle_field";
import { FormController } from "@web/views/form/form_controller";
import { formView } from "@web/views/form/form_view";
import { X2ManyField, x2ManyField } from "@web/views/fields/x2many/x2many_field";
import { ListRenderer } from "@web/views/list/list_renderer";
import { listView } from "@web/views/list/list_view";
import {
    Component,
    onMounted,
    onWillUnmount,
    useState,
} from "@odoo/owl";


class PrelogIncludeCheckbox extends BooleanToggleField {
    static template = "web.BooleanField";

    setup() {
        super.setup();
        this.orm = useService("orm");
    }

    async onChange(newValue) {
        this.state.value = newValue;
        await this.orm.write(
            this.props.record.resModel,
            [this.props.record.resId],
            { [this.props.name]: newValue }
        );
        await this.props.record.load();
    }
}

registry.category("fields").add("prelog_include_checkbox", {
    ...booleanToggleField,
    component: PrelogIncludeCheckbox,
});


class PrelogRecipientListRenderer extends ListRenderer {
    static template = "marathon_short_form_prelogs.PrelogRecipientListRenderer";
    static components = { ...ListRenderer.components, CheckBox };

    get massIncludeRecords() {
        return this.props.list.records.filter(
            (record) =>
                record.data.email && !["queued", "sent"].includes(record.data.status)
        );
    }

    get allIncluded() {
        const records = this.massIncludeRecords;
        return records.length > 0 && records.every((record) => record.data.included);
    }

    get someIncluded() {
        const records = this.massIncludeRecords;
        return records.some((record) => record.data.included) && !this.allIncluded;
    }

    async toggleAllIncluded(value) {
        const records = this.massIncludeRecords;
        if (!records.length) {
            return;
        }
        await this.orm.write(
            records[0].resModel,
            records.map((record) => record.resId),
            { included: value }
        );
        await Promise.all(records.map((record) => record.load()));
    }
}

registry.category("views").add("prelog_recipient_list", {
    ...listView,
    Renderer: PrelogRecipientListRenderer,
});


class PrelogRecipientX2ManyField extends X2ManyField {
    static components = {
        ...X2ManyField.components,
        ListRenderer: PrelogRecipientListRenderer,
    };
}

registry.category("fields").add("prelog_recipient_x2many", {
    ...x2ManyField,
    component: PrelogRecipientX2ManyField,
});


class PrelogProgressDialogFrame extends Dialog {
    async dismiss() {
        // Keep the progress dialog open until the current Send All run finishes.
    }
}


class PrelogSendProgressDialog extends Component {
    static template = "marathon_short_form_prelogs.PrelogSendProgressDialog";
    static components = { Dialog: PrelogProgressDialogFrame };
    static props = {
        batchId: Number,
        batchName: String,
        recipientIds: Array,
        total: Number,
        close: Function,
    };

    setup() {
        this.orm = useService("orm");
        this.cancelled = false;
        this.state = useState({
            completed: 0,
            sent: 0,
            failed: 0,
            currentNumber: 0,
            currentContact: "",
            running: true,
            connectionError: "",
        });
        onMounted(() => this.sendAll());
        onWillUnmount(() => {
            this.cancelled = true;
        });
    }

    get percent() {
        if (!this.props.total) {
            return 100;
        }
        return Math.round((this.state.completed / this.props.total) * 100);
    }

    async sendAll() {
        for (const [index, recipientId] of this.props.recipientIds.entries()) {
            if (this.cancelled) {
                return;
            }
            this.state.currentNumber = index + 1;
            this.state.currentContact = "";
            try {
                const result = await this.orm.call(
                    "mv.prelog.conductor.recipient",
                    "action_send_progress_step",
                    [[recipientId]]
                );
                if (this.cancelled) {
                    return;
                }
                this.state.currentContact = result.contact || "";
                if (result.status === "sent") {
                    this.state.sent += 1;
                } else {
                    this.state.failed += 1;
                }
                this.state.completed += 1;
            } catch (error) {
                this.state.connectionError =
                    error?.data?.message ||
                    error?.message ||
                    _t("The connection was interrupted while sending.");
                break;
            }
        }
        if (!this.cancelled) {
            this.state.running = false;
        }
    }

    closeAndRefresh() {
        this.props.close();
    }
}


function openPrelogSendProgress(env, action) {
    const params = action.params || {};
    env.services.dialog.add(
        PrelogSendProgressDialog,
        {
            batchId: params.batch_id,
            batchName: params.batch_name || _t("Prelog Batch"),
            recipientIds: params.recipient_ids || [],
            total: params.total || 0,
        },
        {
            onClose: () =>
                env.services.action.doAction({
                    type: "ir.actions.act_window",
                    name: _t("Prelog Batch"),
                    res_model: "mv.prelog.conductor.batch",
                    res_id: params.batch_id,
                    views: [[false, "form"]],
                    target: "current",
                }),
        }
    );
}

registry.category("actions").add("prelog_send_progress", openPrelogSendProgress);


class PrelogGeneratorFormController extends FormController {
    setup() {
        super.setup();
        this.display.controlPanel = false;
    }
}

registry.category("views").add("prelog_generator_form", {
    ...formView,
    Controller: PrelogGeneratorFormController,
});
