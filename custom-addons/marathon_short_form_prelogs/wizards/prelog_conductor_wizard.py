# -*- coding: utf-8 -*-

from collections import defaultdict
from datetime import datetime, time
from html import escape

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import format_date


class MvPrelogConductorWeekOption(models.TransientModel):
    _name = "mv.prelog.conductor.week.option"
    _description = "Prelog Generator Week Option"
    _order = "week desc"

    name = fields.Char(required=True)
    wizard_id = fields.Many2one(
        "mv.prelog.conductor.wizard", required=True, ondelete="cascade", index=True
    )
    network_id = fields.Many2one("mv.programs", required=True, ondelete="cascade")
    week = fields.Date(required=True, index=True)
    latest_upload_at = fields.Datetime(readonly=True)
    has_data = fields.Boolean(default=True)


class MvPrelogConductorVersionOption(models.TransientModel):
    _name = "mv.prelog.conductor.version.option"
    _description = "Prelog Generator Version Option"
    _order = "version desc"

    name = fields.Char(required=True)
    wizard_id = fields.Many2one(
        "mv.prelog.conductor.wizard", required=True, ondelete="cascade", index=True
    )
    network_id = fields.Many2one("mv.programs", required=True, ondelete="cascade")
    week = fields.Date(required=True, index=True)
    version = fields.Integer(required=True, index=True)
    latest_upload_at = fields.Datetime(readonly=True)


class MvPrelogConductorPreviewRecipient(models.TransientModel):
    _name = "mv.prelog.conductor.preview.recipient"
    _description = "Prelog Generator Recipient Preview"
    _order = "sequence, id"

    wizard_id = fields.Many2one(
        "mv.prelog.conductor.wizard", required=True, ondelete="cascade", index=True
    )
    sequence = fields.Integer(default=10)
    contact_id = fields.Many2one(
        "res.partner", string="Contact", required=True, readonly=True
    )
    email = fields.Char(string="Email", readonly=True)
    prelog_count = fields.Integer(string="Prelog Rows", readonly=True)
    missing_email = fields.Boolean(readonly=True)


class MvPrelogConductorWizard(models.TransientModel):
    _name = "mv.prelog.conductor.wizard"
    _description = "Prelog Generator"
    _mv_related_tab_enabled = False

    network_id = fields.Many2one(
        "mv.programs",
        string="Network",
        domain="[('inactive', '=', False)]",
    )
    week_option_ids = fields.One2many(
        "mv.prelog.conductor.week.option", "wizard_id", string="Available Weeks"
    )
    week_option_id = fields.Many2one(
        "mv.prelog.conductor.week.option",
        string="Week",
        domain="[('wizard_id', '=', id), ('network_id', '=', network_id)]",
    )
    week = fields.Date(
        related="week_option_id.week", string="Selected Week", readonly=True
    )
    version_option_ids = fields.One2many(
        "mv.prelog.conductor.version.option",
        "wizard_id",
        string="Available Versions",
    )
    version_option_id = fields.Many2one(
        "mv.prelog.conductor.version.option",
        string="Version",
        domain="[('wizard_id', '=', id), ('network_id', '=', network_id), ('week', '=', week)]",
    )
    version = fields.Integer(
        related="version_option_id.version", string="Selected Version", readonly=True
    )
    matching_prelog_count = fields.Integer(
        string="Matching Prelog Records", readonly=True
    )
    contact_count = fields.Integer(
        string="Contacts Receiving Workbooks", readonly=True
    )
    missing_email_count = fields.Integer(
        string="Contacts Missing Email", readonly=True
    )
    preview_recipient_ids = fields.One2many(
        "mv.prelog.conductor.preview.recipient",
        "wizard_id",
        string="Recipients",
        readonly=True,
    )

    @api.model
    def action_open_conductor(self):
        wizard = self.create({})
        wizard._load_options()
        return wizard._form_action()

    def _form_action(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Prelog Generator"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "view_id": self.env.ref(
                "marathon_short_form_prelogs.view_mv_prelog_conductor_wizard_form"
            ).id,
            "target": "main",
        }

    @api.onchange("network_id")
    def _onchange_network_id(self):
        for wizard in self:
            wizard.week_option_id = False
            wizard.version_option_id = False
            wizard._rebuild_week_options()
            wizard._refresh_preview()

    @api.onchange("week_option_id")
    def _onchange_week_option_id(self):
        for wizard in self:
            wizard.version_option_id = False
            wizard._rebuild_version_options()
            wizard._refresh_preview()

    @api.onchange("version_option_id")
    def _onchange_version_option_id(self):
        for wizard in self:
            wizard._refresh_preview()

    def _refresh_preview(self):
        """Refresh counts and per-contact rows with one prelog query."""
        for wizard in self:
            grouped_counts = defaultdict(int)
            contacts_by_id = {}
            prelogs = wizard._matching_prelogs()
            for prelog in prelogs:
                contact = prelog.schedule.deal_parent.contact
                grouped_counts[contact.id] += 1
                contacts_by_id[contact.id] = contact

            contacts = sorted(
                contacts_by_id.values(),
                key=lambda contact: (contact.display_name or "").casefold(),
            )
            wizard.matching_prelog_count = len(prelogs)
            wizard.contact_count = len(contacts)
            wizard.missing_email_count = sum(
                not bool((contact.email or "").strip()) for contact in contacts
            )
            wizard.preview_recipient_ids = [Command.clear()] + [
                Command.create(
                    {
                        "sequence": sequence,
                        "contact_id": contact.id,
                        "email": (contact.email or "").strip() or False,
                        "prelog_count": grouped_counts[contact.id],
                        "missing_email": not bool((contact.email or "").strip()),
                    }
                )
                for sequence, contact in enumerate(contacts, start=1)
            ]

    def action_prepare_emails(self):
        self.ensure_one()
        if not self.network_id or not self.week_option_id or not self.version_option_id:
            raise UserError(_("Network, Week, and Version are required."))
        if self.network_id.inactive:
            raise UserError(_("The selected Network is inactive."))

        prelogs = self._matching_prelogs()
        if not prelogs:
            raise UserError(
                _("No eligible prelog records match the selected Network, Week, and Version.")
            )

        grouped_ids = defaultdict(list)
        for prelog in prelogs:
            grouped_ids[prelog.schedule.deal_parent.contact.id].append(prelog.id)

        contacts = self.env["res.partner"].browse(grouped_ids.keys()).exists()
        previous_sends = self.env["mv.prelog.conductor.recipient"].search(
            [
                ("network_id", "=", self.network_id.id),
                ("week", "=", self.week),
                ("version", "=", self.version),
                ("contact_id", "in", contacts.ids),
                (
                    "status",
                    "in",
                    ["queued", "sent"],
                ),
            ],
            order="id desc",
        )
        previous_send_by_contact = {}
        for line in previous_sends:
            previous_send_by_contact.setdefault(line.contact_id.id, line)

        batch = self.env["mv.prelog.conductor.batch"].create(
            {
                "network_id": self.network_id.id,
                "week": self.week,
                "version": self.version,
                "requested_by_id": self.env.user.id,
                "ready_notification_requested": True,
            }
        )

        line_values = []
        for contact in contacts.sorted(lambda partner: partner.display_name or ""):
            account = contact.parent_id
            email = (contact.email or "").strip()
            previous_send = previous_send_by_contact.get(contact.id)
            vals = {
                "batch_id": batch.id,
                "contact_id": contact.id,
                "account_id": account.id if account else False,
                "email": email or False,
                "email_cc": contact._prelog_conductor_email_cc(email),
                "included": bool(email),
                "prelog_ids": [Command.set(grouped_ids[contact.id])],
                "prelog_count": len(grouped_ids[contact.id]),
                "email_subject": self._email_subject(contact, account),
                "email_body": self._email_body(),
                "is_resend": bool(previous_send),
                "duplicate_of_id": previous_send.id if previous_send else False,
            }
            line_values.append(vals)

        self.env["mv.prelog.conductor.recipient"].create(line_values)
        batch_action = {
            "type": "ir.actions.act_window",
            "name": _("Prelog Batch"),
            "res_model": "mv.prelog.conductor.batch",
            "res_id": batch.id,
            "view_mode": "form",
            "views": [
                (
                    self.env.ref(
                        "marathon_short_form_prelogs.view_mv_prelog_conductor_batch_form"
                    ).id,
                    "form",
                )
            ],
            "target": "main",
        }
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Prelog Batch %s Started") % batch.name,
                "message": _(
                    "Your prelogs are being generated. You'll get an email when "
                    "they're ready to send. You can safely close this page."
                ),
                "type": "info",
                "sticky": False,
                "next": batch_action,
            },
        }

    def _rebuild_week_options(self):
        self.week_option_id = False
        self.version_option_id = False
        if not self.network_id:
            return
        options = self.week_option_ids.filtered(
            lambda option: option.network_id == self.network_id and option.has_data
        )
        if options:
            self.week_option_id = options.sorted(
                key=lambda option: (
                    option.latest_upload_at or datetime.min,
                    option.week,
                ),
                reverse=True,
            )[0]
            self._rebuild_version_options()

    def _rebuild_version_options(self):
        self.version_option_id = False
        if not self.network_id or not self.week_option_id:
            return
        options = self.version_option_ids.filtered(
            lambda option: option.network_id == self.network_id
            and option.week == self.week_option_id.week
        )
        if options:
            self.version_option_id = options.sorted(
                key=lambda option: (
                    option.latest_upload_at or datetime.min,
                    option.version,
                ),
                reverse=True,
            )[0]

    def _load_options(self):
        """Snapshot all available Network/Week/Version choices for this page."""
        self.ensure_one()
        domain = [
            ("removed", "=", False),
            "|",
            ("import_job", "=", False),
            ("import_job.state", "=", "completed"),
            "|",
            ("import_program", "!=", False),
            ("schedule.deal_parent.program", "!=", False),
        ]
        latest_by_week = {}
        latest_by_version = {}
        for prelog in self.env["mv.prelog_data"].search(domain, order="id"):
            if prelog.import_program and prelog.import_week_value:
                network = prelog.import_program
                week = prelog.import_week_value
            else:
                network = prelog.schedule.deal_parent.program
                week = prelog.schedule.week
            if not network or network.inactive or not week:
                continue
            upload_at = self._prelog_upload_at(prelog)
            week_key = (network.id, week)
            if upload_at > latest_by_week.get(week_key, datetime.min):
                latest_by_week[week_key] = upload_at
            if prelog.version:
                version_key = (network.id, week, prelog.version)
                if upload_at > latest_by_version.get(version_key, datetime.min):
                    latest_by_version[version_key] = upload_at

        self.env["mv.prelog.conductor.week.option"].create(
            [
                {
                    "name": format_date(self.env, week),
                    "wizard_id": self.id,
                    "network_id": network_id,
                    "week": week,
                    "latest_upload_at": upload_at,
                    "has_data": True,
                }
                for (network_id, week), upload_at in latest_by_week.items()
            ]
        )
        self.env["mv.prelog.conductor.version.option"].create(
            [
                {
                    "name": _("Version %s") % version,
                    "wizard_id": self.id,
                    "network_id": network_id,
                    "week": week,
                    "version": version,
                    "latest_upload_at": upload_at,
                }
                for (network_id, week, version), upload_at in latest_by_version.items()
            ]
        )

    def _matching_prelogs(self):
        self.ensure_one()
        if not self.network_id or not self.week_option_id or not self.version_option_id:
            return self.env["mv.prelog_data"]
        return self._eligible_prelogs(
            week=self.week_option_id.week,
            version=self.version_option_id.version,
        )

    def _eligible_prelogs(self, *, week=None, version=None, limit=None):
        self.ensure_one()
        if not self.network_id:
            return self.env["mv.prelog_data"]
        domain = [
            ("removed", "=", False),
            ("schedule", "!=", False),
            ("schedule.deal_parent.contact", "!=", False),
            ("schedule.deal_parent.contact.active", "=", True),
            ("schedule.deal_parent.contact.inactive", "=", False),
            ("schedule.status", "!=", "canceled"),
            ("schedule.deal_parent.status", "!=", "canceled"),
            ("import_match_status", "in", [False, "matched"]),
            "|",
            ("import_job", "=", False),
            ("import_job.state", "=", "completed"),
        ]
        if week:
            domain += [
                "|",
                "&",
                ("import_program", "=", self.network_id.id),
                ("import_week_value", "=", week),
                "&",
                ("schedule.deal_parent.program", "=", self.network_id.id),
                ("schedule.week", "=", week),
            ]
        else:
            domain += [
                "|",
                ("import_program", "=", self.network_id.id),
                ("schedule.deal_parent.program", "=", self.network_id.id),
            ]
        if version is not None:
            domain.append(("version", "=", version))

        prelogs = self.env["mv.prelog_data"].search(domain, order="id")
        eligible = prelogs.filtered(self._has_active_contact_and_account)
        return eligible[:limit] if limit else eligible

    @staticmethod
    def _has_active_contact_and_account(prelog):
        contact = prelog.schedule.deal_parent.contact
        if not contact or not contact.active or contact.inactive:
            return False
        account = contact.parent_id
        if account and (
            not account.active
            or account.inactive
            or account.agency_status == "inactive"
        ):
            return False
        return True

    def _prelog_week(self, prelog):
        if prelog.import_program == self.network_id and prelog.import_week_value:
            return prelog.import_week_value
        if (
            prelog.schedule.deal_parent.program == self.network_id
            and prelog.schedule.week
        ):
            return prelog.schedule.week
        return prelog.import_week_value or prelog.schedule.week

    @staticmethod
    def _prelog_upload_at(prelog):
        value = (
            prelog.import_job.finished_at
            or prelog.import_job.create_date
            or prelog.create_date
        )
        return fields.Datetime.to_datetime(value) or datetime.combine(
            fields.Date.to_date(prelog.import_week_value or prelog.schedule.week),
            time.min,
        )

    def _email_subject(self, contact, account):
        account_name = (
            account.display_name if account else contact.commercial_company_name
        ) or contact.display_name
        return _("%(account)s %(network)s Prelogs week of %(week)s") % {
            "account": account_name,
            "network": self.network_id.display_name,
            "week": format_date(self.env, self.week),
        }

    def _email_body(self):
        network_name = escape(self.network_id.display_name or "")
        week = escape(format_date(self.env, self.week))
        return (
            f"<p>Attached please find your {network_name} prelogs for week of {week}.</p>"
            "<p>Please email us with any questions.</p>"
            "<p>Thanks!</p>"
        )

    @api.constrains("network_id", "week_option_id", "version_option_id")
    def _check_options_belong_to_network(self):
        for wizard in self:
            if wizard.week_option_id and wizard.week_option_id.network_id != wizard.network_id:
                raise ValidationError(_("The selected Week does not belong to this Network."))
            if wizard.version_option_id and (
                wizard.version_option_id.network_id != wizard.network_id
                or wizard.version_option_id.week != wizard.week_option_id.week
            ):
                raise ValidationError(
                    _("The selected Version does not belong to this Network and Week.")
                )
