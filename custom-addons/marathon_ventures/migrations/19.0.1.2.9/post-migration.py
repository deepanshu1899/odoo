# -*- coding: utf-8 -*-
"""Backfill stored Prelog Workbench matching for 19.0.1.2.9.

New imports populate these fields before the job completes. Existing rows need
the same stored representation once so the upgraded Workbench never falls back
to live matching. Historical attachments are preserved; historical unmatched
rows receive suggestions and Info but are not auto-attached during upgrade.
Operators can use Refresh to attach newly exact historical matches deliberately.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # Normalize the old three-code import status to the two states a record can
    # actually carry. A failed row has no record, so failed_to_create was never
    # a meaningful record-level status.
    cr.execute(
        """
        UPDATE mv_prelog_data
           SET import_match_status = CASE
               WHEN schedule IS NOT NULL AND COALESCE(removed, FALSE) = FALSE
                   THEN 'matched'
               ELSE 'unmatched'
           END
        """
    )
    _logger.info(
        "mv_prelog_data: normalized stored status on %s row(s)", cr.rowcount,
    )

    # A removed row makes no matching claim. Matched rows only retain their real
    # attachment; suggestion state belongs exclusively to unmatched records.
    cr.execute(
        """
        UPDATE mv_prelog_data
           SET suggested_schedule = NULL,
               possible_schedules = NULL,
               match_flags = NULL,
               info = NULL
         WHERE COALESCE(removed, FALSE) = TRUE
            OR schedule IS NOT NULL
        """
    )

    env = api.Environment(cr, SUPERUSER_ID, {})
    Prelog = env['mv.prelog_data']
    domain = [
        ('removed', '=', False),
        ('schedule', '=', False),
        ('import_program', '!=', False),
        ('import_week_value', '!=', False),
    ]
    ids = Prelog.search(domain, order='id').ids
    batch_size = 1000
    for start in range(0, len(ids), batch_size):
        batch = Prelog.browse(ids[start:start + batch_size])
        # Suggestions/status/info only. An upgrade must never silently attach a
        # historical row a human may already have reviewed.
        Prelog._prelog_store_matching(batch, False, False, attach=False)
        env.flush_all()
        _logger.info(
            "mv_prelog_data: stored matching backfill %s/%s",
            min(start + batch_size, len(ids)),
            len(ids),
        )
