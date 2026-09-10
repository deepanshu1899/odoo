# -*- coding: utf-8 -*-

import base64
import logging
from html import escape

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import email_split, format_date

from ..services.xlsx_renderer import render_short_form_workbook

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = "res.partner"

    def _prelog_conductor_email_cc(self, primary_email=None):
        """Return the contact's valid, unique Prelog CC addresses."""
        self.ensure_one()
        primary = (primary_email or self.email or "").strip().casefold()
        seen = {primary} if primary else set()
        addresses = []
        raw_value = (self.prelog_cc or "").replace(";", ",")
        for address in email_split(raw_value):
            key = address.strip().casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            addresses.append(address.strip())
        return ", ".join(addresses)


class MvPrelogConductorBatch(models.Model):
    _name = "mv.prelog.conductor.batch"
    _description = "Prelog Batch"
    _order = "create_date desc, id desc"

    name = fields.Char(default="New", required=True, copy=False, readonly=True)
    network_id = fields.Many2one(
        "mv.programs",
        string="Network",
        required=True,
        ondelete="restrict",
        index=True,
    )
    week = fields.Date(required=True, index=True)
    version = fields.Integer(required=True, index=True)
    requested_by_id = fields.Many2one(
        "res.users",
        string="Prepared By",
        required=True,
        default=lambda self: self.env.user,
        ondelete="restrict",
    )
    recipient_ids = fields.One2many(
        "mv.prelog.conductor.recipient",
        "batch_id",
        string="Recipients",
    )
    state = fields.Selection(
        [
            ("preparing", "Preparing"),
            ("ready", "Ready"),
            ("queued", "Queued"),
            ("sent", "Sent"),
            ("attention", "Needs Attention"),
        ],
        compute="_compute_summary",
        store=True,
        string="Status",
    )
    recipient_count = fields.Integer(compute="_compute_summary", store=True)
    prelog_count = fields.Integer(compute="_compute_summary", store=True)
    prepared_count = fields.Integer(compute="_compute_summary", store=True)
    missing_email_count = fields.Integer(compute="_compute_summary", store=True)
    failed_count = fields.Integer(compute="_compute_summary", store=True)
    queued_count = fields.Integer(compute="_compute_summary", store=True)
    sent_count = fields.Integer(compute="_compute_summary", store=True)
    ready_notification_requested = fields.Boolean(
        default=False,
        readonly=True,
        copy=False,
    )
    ready_notification_mail_id = fields.Many2one(
        "mail.mail",
        string="Ready Email",
        readonly=True,
        copy=False,
        ondelete="set null",
    )
    ready_notification_queued_at = fields.Datetime(
        string="Ready Email Queued",
        readonly=True,
        copy=False,
    )
    ready_notification_error = fields.Text(
        string="Ready Email Error",
        readonly=True,
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "mv.prelog.conductor.batch"
                ) or _("New")
        return super().create(vals_list)

    @api.depends(
        "recipient_ids.status",
        "recipient_ids.included",
        "recipient_ids.prelog_count",
    )
    def _compute_summary(self):
        for batch in self:
            lines = batch.recipient_ids
            statuses = set(lines.mapped("status"))
            batch.recipient_count = len(lines)
            batch.prelog_count = sum(lines.mapped("prelog_count"))
            batch.prepared_count = len(lines.filtered(lambda line: line.status == "prepared"))
            batch.missing_email_count = len(
                lines.filtered(lambda line: line.status == "missing_email")
            )
            batch.failed_count = len(lines.filtered(lambda line: line.status == "failed"))
            batch.queued_count = len(lines.filtered(lambda line: line.status == "queued"))
            batch.sent_count = len(lines.filtered(lambda line: line.status == "sent"))

            if not lines or statuses.intersection({"pending", "processing"}):
                batch.state = "preparing"
            elif "failed" in statuses:
                batch.state = "attention"
            elif "prepared" in statuses:
                batch.state = "ready"
            elif "queued" in statuses:
                batch.state = "queued"
            elif "missing_email" in statuses:
                batch.state = "attention"
            elif "sent" in statuses and statuses.issubset({"sent", "duplicate"}):
                batch.state = "sent"
            else:
                batch.state = "ready"

    def action_send_all(self):
        self.ensure_one()
        lines = self.recipient_ids.filtered(
            lambda line: line.included
            and line.status == "prepared"
            and line.email
            and line.attachment_id
        )
        if not lines:
            raise UserError(_("There are no included, prepared emails to send."))
        resend_lines = lines.filtered("is_resend")
        if resend_lines:
            confirmation = self.env[
                "mv.prelog.conductor.resend.confirm"
            ].create(
                {
                    "recipient_ids": [Command.set(lines.ids)],
                    "resend_recipient_ids": [Command.set(resend_lines.ids)],
                    "use_send_progress": True,
                }
            )
            return {
                "type": "ir.actions.act_window",
                "name": _("Confirm Prelog Resend"),
                "res_model": "mv.prelog.conductor.resend.confirm",
                "res_id": confirmation.id,
                "view_mode": "form",
                "target": "new",
            }
        return self._send_progress_action(lines)

    def _send_progress_action(self, lines):
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "tag": "prelog_send_progress",
            "params": {
                "batch_id": self.id,
                "batch_name": self.name,
                "recipient_ids": lines.ids,
                "total": len(lines),
            },
        }

    def action_open_recipients(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id(
            "marathon_short_form_prelogs.action_mv_prelog_conductor_recipient"
        )
        action["domain"] = [("batch_id", "=", self.id)]
        action["context"] = {"default_batch_id": self.id, "create": False}
        return action

    def _queue_ready_notifications(self):
        """Queue one completion email after workbook generation has finished."""
        for batch in self:
            if (
                not batch.ready_notification_requested
                or batch.ready_notification_queued_at
                or not batch.recipient_ids
                or batch.recipient_ids.filtered(
                    lambda line: line.status in {"pending", "processing", "duplicate"}
                )
            ):
                continue

            recipient_email = (batch.requested_by_id.email or "").strip()
            if not recipient_email:
                error = _(
                    "The ready notification could not be queued because %(user)s "
                    "does not have an email address."
                ) % {"user": batch.requested_by_id.display_name}
                if batch.ready_notification_error != error:
                    batch.ready_notification_error = error
                continue

            week = format_date(batch.env, batch.week)
            network = batch.network_id.display_name
            action_xmlid = (
                "marathon_short_form_prelogs.action_mv_prelog_conductor_batch"
            )
            batch_url = (
                f"{batch.get_base_url().rstrip('/')}"
                f"/odoo/action-{action_xmlid}/{batch.id}"
            )
            subject = _(
                "Prelogs ready to send - %(network)s, %(week)s, %(version)s"
            ) % {
                "network": network,
                "week": week,
                "version": batch.version,
            }
            body_html = _(
                "<p>Your prelogs for %(network)s for the week of %(week)s, "
                "version %(version)s are ready to send.</p>"
                "<p><a href=\"%(url)s\">Open %(batch)s to review and send them</a></p>"
            ) % {
                "network": escape(network or ""),
                "week": escape(week),
                "version": batch.version,
                "url": escape(batch_url, quote=True),
                "batch": escape(batch.name),
            }

            try:
                with self.env.cr.savepoint():
                    mail = self.env["mail.mail"].create(
                        {
                            "subject": subject,
                            "body_html": body_html,
                            "email_to": batch.requested_by_id.email_formatted,
                            "email_from": self.env.company.email_formatted
                            or batch.requested_by_id.email_formatted,
                            "model": batch._name,
                            "res_id": batch.id,
                            "auto_delete": False,
                        }
                    )
                    batch.write(
                        {
                            "ready_notification_mail_id": mail.id,
                            "ready_notification_queued_at": fields.Datetime.now(),
                            "ready_notification_error": False,
                        }
                    )
                    self.env.ref("mail.ir_cron_mail_scheduler_action")._trigger()
            except Exception as exc:
                _logger.exception(
                    "Could not queue ready notification for prelog batch %s",
                    batch.id,
                )
                batch.ready_notification_error = str(exc)


class MvPrelogConductorRecipient(models.Model):
    _name = "mv.prelog.conductor.recipient"
    _description = "Prelog Recipient"
    _order = "batch_id desc, contact_id, id"

    batch_id = fields.Many2one(
        "mv.prelog.conductor.batch",
        required=True,
        ondelete="cascade",
        index=True,
    )
    network_id = fields.Many2one(
        related="batch_id.network_id",
        store=True,
        readonly=True,
        index=True,
    )
    week = fields.Date(related="batch_id.week", store=True, readonly=True, index=True)
    version = fields.Integer(
        related="batch_id.version", store=True, readonly=True, index=True
    )
    contact_id = fields.Many2one(
        "res.partner",
        string="Contact",
        required=True,
        ondelete="restrict",
        index=True,
    )
    account_id = fields.Many2one(
        "res.partner",
        string="Account / Agency",
        ondelete="set null",
        index=True,
    )
    email = fields.Char(string="Email")
    email_cc = fields.Char(string="Prelog CC")
    included = fields.Boolean(default=True, string="Include")
    prelog_ids = fields.Many2many(
        "mv.prelog_data",
        "mv_prelog_conductor_recipient_prelog_rel",
        "recipient_id",
        "prelog_id",
        string="Prelogs",
        readonly=True,
    )
    prelog_count = fields.Integer(string="Prelog Rows", required=True, readonly=True)
    attachment_id = fields.Many2one(
        "ir.attachment",
        string="Attachment",
        readonly=True,
        ondelete="set null",
    )
    attachment_filename = fields.Char(
        related="attachment_id.name", string="Attachment Filename", readonly=True
    )
    email_subject = fields.Char(string="Subject", required=True)
    email_body = fields.Html(string="Body", required=True, sanitize=True)
    status = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Generating"),
            ("prepared", "Prepared"),
            ("missing_email", "Missing Email"),
            ("failed", "Failed"),
            ("duplicate", "Already Exists"),
            ("queued", "Queued"),
            ("sent", "Sent"),
        ],
        default="pending",
        required=True,
        readonly=True,
        index=True,
    )
    error_message = fields.Text(string="Error", readonly=True)
    duplicate_of_id = fields.Many2one(
        "mv.prelog.conductor.recipient",
        string="Previous Send",
        readonly=True,
        ondelete="set null",
    )
    is_resend = fields.Boolean(
        string="Resend",
        default=False,
        readonly=True,
        index=True,
        help="This recipient has already been sent the same Network, Week, and Version.",
    )
    mail_id = fields.Many2one(
        "mail.mail", string="Outgoing Message", readonly=True, ondelete="set null"
    )
    attempt_count = fields.Integer(default=0, readonly=True)
    rendered_at = fields.Datetime(readonly=True)
    queued_at = fields.Datetime(readonly=True)
    sent_at = fields.Datetime(readonly=True)

    def action_download_attachment(self):
        self.ensure_one()
        if not self.attachment_id:
            raise UserError(_("This recipient does not have a generated workbook yet."))
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{self.attachment_id.id}?download=true",
            "target": "self",
        }

    def action_send_selected(self):
        candidates = self.filtered(
            lambda line: line.included
            and line.status == "prepared"
            and line.email
            and line.attachment_id
        )
        if not candidates:
            raise UserError(_("Select at least one included, prepared recipient."))

        resend_candidates = candidates.filtered("is_resend")
        if resend_candidates and not self.env.context.get(
            "skip_prelog_resend_confirmation"
        ):
            confirmation = self.env[
                "mv.prelog.conductor.resend.confirm"
            ].create(
                {
                    "recipient_ids": [Command.set(candidates.ids)],
                    "resend_recipient_ids": [Command.set(resend_candidates.ids)],
                }
            )
            return {
                "type": "ir.actions.act_window",
                "name": _("Confirm Prelog Resend"),
                "res_model": "mv.prelog.conductor.resend.confirm",
                "res_id": confirmation.id,
                "view_mode": "form",
                "target": "new",
            }

        sent = 0
        failures = 0
        for line in candidates:
            result = line._send_one()
            if result["status"] == "sent":
                sent += 1
            else:
                failures += 1

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Prelog Generator"),
                "message": _("Sent %(sent)s email(s); %(failed)s failed.")
                % {"sent": sent, "failed": failures},
                "type": "success" if not failures else "warning",
                "sticky": False,
            },
        }

    def action_send_progress_step(self):
        """Send one prepared recipient and return a compact progress result."""
        self.ensure_one()
        if self.status == "sent":
            return self._send_progress_result()
        if not (
            self.included
            and self.status == "prepared"
            and self.email
            and self.attachment_id
        ):
            return self._send_progress_result(
                status="failed",
                error=_("This recipient is no longer ready to send."),
            )
        return self._send_one()

    def _send_one(self):
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                mail = self.env["mail.mail"].create(self._mail_values())
                mail.send(raise_exception=False)

                if mail.state == "sent":
                    self.write(
                        {
                            "mail_id": mail.id,
                            "status": "sent",
                            "queued_at": False,
                            "sent_at": fields.Datetime.now(),
                            "error_message": False,
                        }
                    )
                else:
                    failure_reason = mail.failure_reason or _(
                        "The email was not accepted for immediate delivery."
                    )
                    # Never leave this message for the scheduled queue. If an
                    # SMTP server deferred it, record the failure and require
                    # an intentional retry from the recipient row.
                    if mail.state == "outgoing":
                        mail.write({"state": "cancel", "scheduled_date": False})
                    self.write(
                        {
                            "mail_id": mail.id,
                            "status": "failed",
                            "queued_at": False,
                            "sent_at": False,
                            "error_message": failure_reason,
                        }
                    )
        except Exception as exc:  # keep processing the remaining recipients
            _logger.exception("Could not send prelog recipient %s", self.id)
            self.write(
                {
                    "status": "failed",
                    "queued_at": False,
                    "sent_at": False,
                    "error_message": str(exc),
                }
            )
        return self._send_progress_result()

    def _send_progress_result(self, *, status=None, error=None):
        self.ensure_one()
        return {
            "recipient_id": self.id,
            "contact": self.contact_id.display_name,
            "status": status or self.status,
            "error": error if error is not None else (self.error_message or False),
        }

    def _mail_values(self):
        self.ensure_one()
        return {
            "subject": self.email_subject,
            "body_html": self.email_body,
            "email_to": self.email,
            "email_cc": self.email_cc or False,
            "email_from": (
                self.env.company.email_formatted
                or self.batch_id.requested_by_id.email_formatted
                or False
            ),
            "attachment_ids": [Command.set(self.attachment_id.ids)],
            "model": self._name,
            "res_id": self.id,
            "auto_delete": False,
        }

    def action_retry_generation(self):
        failed = self.filtered(lambda line: line.status == "failed")
        if not failed:
            raise UserError(_("Select at least one failed recipient."))
        failed._reset_for_generation()
        return True

    def action_regenerate(self):
        if any(line.status in {"processing", "queued"} for line in self):
            raise UserError(
                _("Generating or queued recipients cannot be regenerated yet.")
            )
        self._reset_for_generation()
        return True

    def _reset_for_generation(self):
        for line in self:
            old_attachment = line.attachment_id
            keep_old_attachment = bool(line.mail_id)
            current_email = (line.contact_id.email or "").strip()
            current_email_cc = line.contact_id._prelog_conductor_email_cc(
                current_email
            )
            previous_send = line._find_previous_send()
            is_resend = (
                line.is_resend
                or line.status in {"queued", "sent"}
                or bool(previous_send)
            )
            line.write(
                {
                    "email": current_email or False,
                    "email_cc": current_email_cc or False,
                    "included": bool(current_email),
                    "attachment_id": False,
                    "mail_id": False,
                    "status": "pending",
                    "error_message": False,
                    "is_resend": is_resend,
                    "duplicate_of_id": previous_send.id if previous_send else False,
                    "rendered_at": False,
                    "queued_at": False,
                    "sent_at": False,
                }
            )
            if old_attachment and not keep_old_attachment:
                old_attachment.unlink()

    @api.model
    def _cron_process_prelog_conductor(self):
        self._sync_mail_statuses()
        lines = self.search(
            [("status", "in", ["pending", "duplicate"])],
            order="create_date, id",
            limit=10,
        )
        for line in lines:
            processing_values = {
                "status": "processing",
                "attempt_count": line.attempt_count + 1,
                "error_message": False,
            }
            if line.status == "duplicate":
                previous_send = line._find_previous_send()
                processing_values.update(
                    {
                        "included": bool(line.email),
                        "is_resend": bool(previous_send),
                        "duplicate_of_id": (
                            previous_send.id if previous_send else False
                        ),
                    }
                )
            line.write(processing_values)
            try:
                with self.env.cr.savepoint():
                    line._generate_workbook()
            except Exception as exc:
                _logger.exception(
                    "Workbook generation failed for prelog recipient %s",
                    line.id,
                )
                line.write({"status": "failed", "error_message": str(exc)})
        self.env.flush_all()
        batches = self.env["mv.prelog.conductor.batch"].search(
            [
                ("ready_notification_requested", "=", True),
                ("ready_notification_queued_at", "=", False),
                ("state", "in", ["ready", "attention"]),
            ],
            order="create_date, id",
            limit=50,
        )
        batches._queue_ready_notifications()
        self._sync_mail_statuses()

    def _find_previous_send(self):
        self.ensure_one()
        return self.search(
            [
                ("id", "!=", self.id),
                ("network_id", "=", self.network_id.id),
                ("week", "=", self.week),
                ("version", "=", self.version),
                ("contact_id", "=", self.contact_id.id),
                ("status", "in", ["queued", "sent"]),
            ],
            order="id desc",
            limit=1,
        )

    @api.model
    def _sync_mail_statuses(self):
        lines = self.search([("status", "=", "queued"), ("mail_id", "!=", False)])
        for line in lines:
            if line.mail_id.state == "sent":
                line.write(
                    {
                        "status": "sent",
                        "sent_at": fields.Datetime.now(),
                        "error_message": False,
                    }
                )
            elif line.mail_id.state in {"exception", "cancel"}:
                line.write(
                    {
                        "status": "failed",
                        "error_message": line.mail_id.failure_reason
                        or _("The queued email was cancelled or failed."),
                    }
                )

    def _generate_workbook(self):
        self.ensure_one()
        if not self.prelog_ids:
            raise UserError(_("No prelog rows are attached to this recipient."))

        workbook_bytes, filename = render_short_form_workbook(self)
        attachment = self.env["ir.attachment"].create(
            {
                "name": filename,
                "datas": base64.b64encode(workbook_bytes),
                "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "res_model": self._name,
                "res_id": self.id,
            }
        )
        self.write(
            {
                "attachment_id": attachment.id,
                "status": "prepared" if self.email else "missing_email",
                "included": bool(self.email),
                "rendered_at": fields.Datetime.now(),
                "error_message": False,
            }
        )


class MvPrelogConductorResendConfirm(models.TransientModel):
    _name = "mv.prelog.conductor.resend.confirm"
    _description = "Confirm Prelog Resend"

    recipient_ids = fields.Many2many(
        "mv.prelog.conductor.recipient",
        "mv_prelog_resend_confirm_recipient_rel",
        "wizard_id",
        "recipient_id",
        string="Emails to Send",
        required=True,
        readonly=True,
    )
    resend_recipient_ids = fields.Many2many(
        "mv.prelog.conductor.recipient",
        "mv_prelog_resend_confirm_previous_rel",
        "wizard_id",
        "recipient_id",
        string="Previously Sent Prelogs",
        required=True,
        readonly=True,
    )
    recipient_count = fields.Integer(compute="_compute_counts")
    resend_count = fields.Integer(compute="_compute_counts")
    use_send_progress = fields.Boolean(default=False)

    @api.depends("recipient_ids", "resend_recipient_ids")
    def _compute_counts(self):
        for wizard in self:
            wizard.recipient_count = len(wizard.recipient_ids)
            wizard.resend_count = len(wizard.resend_recipient_ids)

    def action_confirm_resend(self):
        self.ensure_one()
        candidates = self.recipient_ids.exists()
        if not candidates:
            raise UserError(_("There are no prepared emails to send."))
        if self.use_send_progress:
            batches = candidates.mapped("batch_id")
            if len(batches) != 1:
                raise UserError(_("Send All can only process one prelog batch."))
            return batches._send_progress_action(candidates)
        return candidates.with_context(
            skip_prelog_resend_confirmation=True
        ).action_send_selected()
