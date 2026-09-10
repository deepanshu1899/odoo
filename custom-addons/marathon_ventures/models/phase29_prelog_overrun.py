# -*- coding: utf-8 -*-
"""Phase 29 - Prelog / Schedule Overrun detection.

An overrun occurs when the number of prelogs attached to a schedule
exceeds the schedule's `units_available` capacity.

Design:
  * `mv.schedules.overrun_amount` (Integer, stored) - max(0, attached - cap).
  * `mv.prelog_data.is_overrun` (Boolean, stored) - True for the LAST N
    attached prelogs (by ascending id) where N == schedule.overrun_amount.
    "Last attached" == last-imported == the ones that "caused" the overrun.
  * Fields are plain (not @api.depends) because the population depends on
    a schedule-wide count that Odoo can't express cheaply as a dependency
    graph. Instead every mutation path calls _recompute_prelog_overruns
    with the affected schedule ids.

Mutation surface hooked from:
  - mv_prelog_import_job._process_rows            (after CSV import batch)
  - fuzzy_match_apply                             (attach / replace)
  - fuzzy_match_detach                            (detach)
  - _fuzzy_set_removed_records                    (remove / unremove)
  - fuzzy_workbench_bulk_action                   (permanent delete)
  - mv.prelog_data.unlink                         (fallback for any other
                                                    delete path)
"""
import logging

from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class MvSchedulesOverrun(models.Model):
    _name = 'mv.schedules'
    _inherit = 'mv.schedules'

    overrun_amount = fields.Integer(
        string='Overrun Amount',
        default=0,
        help=(
            'Number of attached prelogs that exceed the schedule\'s '
            'units_available capacity. Zero when within capacity.'
        ),
    )
    prelog_attached_count = fields.Integer(
        string='Attached Prelogs',
        default=0,
        help='Count of non-removed prelogs currently attached to this schedule.',
    )


class MvPrelogDataOverrun(models.Model):
    _name = 'mv.prelog_data'
    _inherit = 'mv.prelog_data'

    # ------------------------------------------------------------------
    # Indexes for the Prelog Workbench.
    #
    # Every workbench query filters on some combination of
    # version / import_program / import_week_value / removed / schedule,
    # and the overrun map groups by `schedule`. Only import_program,
    # import_week_value, network_deal_number and import_job carried an
    # index; the rest forced sequential scans, which is what made large
    # views slow. A composite index for the common three-way filter is
    # added in migrations/19.0.1.2.5/post-migration.py.
    # ------------------------------------------------------------------
    version = fields.Integer(string='Version', index=True)
    schedule = fields.Many2one(
        string='Schedule',
        comodel_name='mv.schedules',
        ondelete='cascade',
        index=True,
    )

    is_overrun = fields.Boolean(
        string='Is Overrun',
        default=False,
        help=(
            'True when this prelog is attached to a schedule whose '
            'attached count exceeds its units_available. Set by '
            '_recompute_prelog_overruns based on ascending id order '
            '(later imports overrun first).'
        ),
    )

    # ------------------------------------------------------------------
    # unlink hook: any delete path (workbench bulk delete, import
    # replace_existing sweep, manual admin action) funnels through here.
    # Collect the schedules that lose an attachment BEFORE the delete,
    # then recompute after.
    # ------------------------------------------------------------------
    def unlink(self):
        sched_ids = set(self.mapped('schedule').ids)
        res = super().unlink()
        if sched_ids:
            self._recompute_prelog_overruns(sched_ids)
        return res

    # ------------------------------------------------------------------
    # Core helper. Idempotent - safe to call multiple times.
    # Callers pass a set/list of schedule ids that MAY have gained or
    # lost an attached prelog. We recompute each one from scratch.
    # ------------------------------------------------------------------
    @api.model
    def _mv_latest_prelog_version(self, schedule):
        """Return the most-recent prelog version for a schedule's
        program + week, or None when no prelog exists.

        Prelog files are uploaded daily and each new version RE-INCLUDES
        every prior day's spots (v5 contains Mon-Fri, v4 contains
        Mon-Thu, etc.). Overrun must therefore be judged against a
        SINGLE version - the latest upload - not the union of all
        versions. This resolves that latest version from the prelog
        population for the schedule's program + week.
        """
        program = schedule.deal_parent.program if schedule.deal_parent else False
        week = schedule.week
        domain = [('removed', '=', False)]
        if program:
            domain.append(('import_program', '=', program.id))
        if week:
            domain.append(('import_week_value', '=', week))
        if not program and not week:
            # Fall back to versions actually attached to this schedule.
            domain = [('schedule', '=', schedule.id), ('removed', '=', False)]
        latest = self.search(domain, order='version desc', limit=1)
        return latest.version or None

    @api.model
    def _recompute_prelog_overruns(self, schedule_ids):
        if not schedule_ids:
            return
        Schedule = self.env['mv.schedules']
        schedules = Schedule.browse(list(schedule_ids)).exists()
        for schedule in schedules:
            latest_version = self._mv_latest_prelog_version(schedule)

            # Attached prelogs scoped to the LATEST version only. Spots
            # carried over from earlier versions do not count toward the
            # overrun (they are superseded by the newest upload).
            attach_domain = [
                ('schedule', '=', schedule.id),
                ('removed', '=', False),
            ]
            if latest_version:
                attach_domain.append(('version', '=', latest_version))
            attached = self.search(attach_domain, order='id asc')

            # Any prelog attached to this schedule from an OLDER version
            # must have its is_overrun flag cleared - it is no longer in
            # the active comparison set.
            stale_overrun = self.search([
                ('schedule', '=', schedule.id),
                ('removed', '=', False),
                ('is_overrun', '=', True),
                ('id', 'not in', attached.ids),
            ])
            if stale_overrun:
                stale_overrun.write({'is_overrun': False, 'info': False})

            count = len(attached)
            cap = int(schedule.units_available or 0)
            overrun = max(0, count - cap)

            # Update schedule aggregates in one shot.
            sched_vals = {}
            if schedule.overrun_amount != overrun:
                sched_vals['overrun_amount'] = overrun
            if schedule.prelog_attached_count != count:
                sched_vals['prelog_attached_count'] = count
            if sched_vals:
                schedule.sudo().write(sched_vals)

            # Split attached prelogs into within-cap and overrun cohorts.
            # First `cap` (by id asc) stay matched, remainder are overrun.
            overrun_recs = attached[-overrun:] if overrun else self.browse()
            in_cap_recs = attached - overrun_recs

            if overrun_recs:
                overrun_recs.write({
                    'is_overrun': True,
                    'info': (
                        'Schedule %s has %s unit(s) but %s prelog(s) are '
                        'attached - over by %s.'
                    ) % (
                        schedule.display_name or '', cap, count, overrun,
                    ),
                })
            if in_cap_recs:
                in_cap_recs.write({'is_overrun': False, 'info': False})
        return True
