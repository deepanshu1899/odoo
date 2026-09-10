# Prelog Generator

Prelog Generator prepares contact-specific Short Form prelog workbooks and email drafts from existing Marathon Ventures prelog data.

## Installation

1. Ensure `marathon_ventures` and `mail` are installed.
2. Update the Apps list and install **Prelog Generator**.
3. Confirm the scheduled action **MV — Prelog Generator** is active.

The addon uses the packaged `short_form_prelog.xlsx` master template and the `openpyxl` version already included in Odoo's Python requirements.

## Use

1. Open **Marathon Ventures → Operations → Prelog Generator**.
2. Select one Network. Week and Version offer only combinations with eligible prelog records.
3. Review the record, contact, and missing-email counts, plus the contact-by-contact recipient preview and each contact's prelog row count. Missing primary email addresses are highlighted.
4. Click **Prepare Emails**. This creates a batch and recipient lines; it does not send the contact emails. A self-closing message confirms that work continues safely in the background.
5. The scheduled action generates one workbook per contact in the background. When generation finishes, Odoo queues one notification email to the user who prepared the batch, with a direct link to review and send it. The user's email and the system's outgoing mail server must be configured.
6. Open the batch to download workbooks, review the subject and body, exclude recipients directly from the checkbox column, use the checkbox in the **Include** heading to include or exclude all available recipients, retry failures, or regenerate intentionally. Recipient batches of normal size display on one page.
7. Use **Send All** or select recipient lines and use **Send Selected**. **Send All** opens a centered progress dialog that updates after each recipient and reports sent and failed totals. These actions send each message immediately through the configured outgoing mail server and report any delivery attempt that fails.

Contacts without an email still receive a generated workbook on their batch line, but it cannot be sent by email.

Each prepared email is addressed to the deal's primary contact. Valid, unique addresses saved in that contact's **Prelog CC** field are added to the email's CC recipients; the primary address is never duplicated in CC.

## Resending

Creating another batch for the same Network, Week, and Version is allowed. Contacts
who were previously sent that same selection are marked **Resend**, but their new
workbooks are prepared normally. **Send All** and **Send Selected** display a
confirmation listing the previously sent recipients before any resend occurs.

## Selection rules

The batch uses one Network, Week, and Version. It includes only prelogs that:

- match that exact selection;
- are not removed;
- have a linked contact through the existing schedule and deal records;
- are not linked to cancelled schedules or deals; and
- belong to active contacts and accounts.

No version fallback or mixing occurs.
