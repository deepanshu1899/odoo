# -*- coding: utf-8 -*-
"""Odoo-native Prelog Fuzzy Matching workflow."""

import csv
import io
import re
from datetime import datetime

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare

from ..services.prelog_import.config_loader import load_program_config
from ..services.prelog_import.transforms import normalize_match_text


class MvPrelogDataFuzzyMatching(models.Model):
    """Backend service for the Odoo-native Prelog Fuzzy Matching page.

    Salesforce reviewed mirror rows and created another record after a user
    selected a schedule. Odoo imports ``mv.prelog_data`` directly, so this
    service attaches the chosen schedule to that existing unmatched record.
    """

    _inherit = 'mv.prelog_data'

    _FUZZY_PAGE_SIZE = 200
    _FUZZY_TIME_BUFFER_MINUTES = 120
    _FUZZY_MAX_ALTERNATIVES = 4
    _FUZZY_MAX_ALTERNATIVE_DIFFERENCES = 2
    _FUZZY_DAY_ORDER = {
        'mon': 0,
        'tue': 1,
        'wed': 2,
        'thu': 3,
        'fri': 4,
        'sat': 5,
        'sun': 6,
    }

    # ------------------------------------------------------------------
    # Public RPC methods
    # ------------------------------------------------------------------

    @api.model
    def fuzzy_match_get_options(self, program_id=False, week_start=False):
        """Return filters and the current user's latest completed upload."""
        self._fuzzy_check_access()
        programs = self.env['mv.programs'].search([], order='name, id')
        program, selected_week, _unused_version = (
            self._fuzzy_validate_optional_filters(program_id, week_start, False)
        )
        version_domain = []
        if program:
            version_domain.append(('import_program', '=', program.id))
        if selected_week:
            version_domain.append(('import_week_value', '=', selected_week))
        version_groups = self._read_group(
            version_domain,
            groupby=['version'],
            aggregates=[],
        )
        versions = sorted(
            grouped_version
            for grouped_version, in version_groups
            if grouped_version
        )

        return {
            'programs': [
                {
                    'id': program.id,
                    'name': program.display_name,
                    'inactive': bool(program.inactive),
                }
                for program in programs
            ],
            'versions': versions,
            'page_size': self._FUZZY_PAGE_SIZE,
            'time_buffer_minutes': self._FUZZY_TIME_BUFFER_MINUTES,
            'latest_upload': self._fuzzy_latest_user_upload(),
        }

    @api.model
    def fuzzy_match_search(
        self,
        program_id,
        week_start,
        version,
        offset=0,
        limit=None,
        status='all',
        search_term='',
        air_date=False,
        issue_filter='',
        sort_by='air_date',
        import_job_id=False,
        sort_direction='asc',
    ):
        """Return one database-backed page of stored Prelog results.

        Matching is intentionally absent from this request. The background
        import (or explicit Refresh) has already populated status, suggestions,
        flags and Info, so Postgres can filter, count, sort and LIMIT directly.
        """
        self._fuzzy_check_access()
        program, selected_week, selected_version = self._fuzzy_validate_optional_filters(
            program_id,
            week_start,
            version,
        )
        offset = max(self._fuzzy_int(offset, default=0), 0)
        limit = self._fuzzy_int(limit, default=self._FUZZY_PAGE_SIZE)
        limit = min(max(limit, 1), self._FUZZY_PAGE_SIZE)
        base_domain = self._fuzzy_prelog_domain(
            program.id if program else False,
            selected_week,
            selected_version,
            unmatched_only=False,
            include_removed=status == 'removed',
            import_job_id=import_job_id,
        )
        count_domain = self._fuzzy_prelog_domain(
            program.id if program else False,
            selected_week,
            selected_version,
            unmatched_only=False,
            include_removed=False,
            import_job_id=import_job_id,
        )
        counts = self._prelog_stored_counts(count_domain)
        dollars = self._prelog_stored_dollar_totals(count_domain)
        domain = self._prelog_stored_domain(
            base_domain, status, issue_filter, air_date, search_term,
        )
        total = self.search_count(domain)
        filtered_dollars = self._prelog_stored_rate_sum(domain)
        if total:
            offset = min(offset, ((total - 1) // limit) * limit)
        else:
            offset = 0
        prelogs = self.search(
            domain,
            order=self._prelog_stored_order(sort_by, sort_direction),
            limit=limit,
            offset=offset,
        )
        return {
            'rows': [self._prelog_stored_row(prelog) for prelog in prelogs],
            'total': total,
            'offset': offset,
            'limit': limit,
            'page': (offset // limit) + 1 if total else 0,
            'pages': ((total + limit - 1) // limit) if total else 0,
            'counts': counts,
            'dollars': dollars,
            # Dollar value of the CURRENT view (tab + search + air date
            # + issue filter), across every matching row, not just the
            # visible page.
            'filtered_dollars': filtered_dollars,
        }

    @api.model
    def _fuzzy_dollar_totals(self, all_rows):
        """Sum of `rate` per tab, mirroring the `counts` buckets.

        Kept in lockstep with the counts dict above so a tab's dollar
        figure always describes exactly the rows its badge counts.
        """
        def _sum(predicate):
            return round(sum(
                float(row.get('rate') or 0.0)
                for row in all_rows
                if predicate(row)
            ), 2)

        return {
            'all': _sum(lambda r: r['status'] != 'removed'),
            'matched': _sum(lambda r: r['status'] == 'matched'),
            'unmatched': _sum(
                lambda r: r['status'] in ('suggestion', 'no_suggestion')
            ),
            'suggestions': _sum(lambda r: r['status'] == 'suggestion'),
            'no_suggestion': _sum(lambda r: r['status'] == 'no_suggestion'),
            'removed': _sum(lambda r: r['status'] == 'removed'),
            'overruns': _sum(lambda r: r['status'] == 'overrun'),
        }

    @api.model
    def _fuzzy_enrich_visible_rows(self, rows, program, selected_week):
        """Fill in analysis-derived fields for a page of attached rows.

        _fuzzy_build_rows(analyze_attached=False) leaves `explanation`
        and the *_mismatch flags at their defaults for rows that are
        already attached. That is fine for counting / filtering /
        sorting, but the drawer and the row chips want the real values,
        so we compute them here - for at most `limit` rows instead of
        the whole result set.
        """
        if not rows:
            return
        target_ids = [
            row['id'] for row in rows
            if row.get('attached') and row['attached'].get('id')
        ]
        if not target_ids:
            return
        prelogs = self.browse(target_ids).exists()
        rebuilt = self._fuzzy_build_rows(
            prelogs,
            program,
            selected_week,
            use_attached=True,
            analyze_attached=True,
        )
        by_id = {r['id']: r for r in rebuilt}
        carry = (
            'explanation', 'match_quality', 'match_quality_label',
            'time_mismatch', 'length_mismatch', 'rate_mismatch',
            'deal_mismatch', 'network_mismatch', 'day_mismatch',
            'ambiguous_count', 'suggestion_attachable',
        )
        for row in rows:
            full = by_id.get(row['id'])
            if not full:
                continue
            for key in carry:
                if key in full:
                    row[key] = full[key]

    @api.model
    def fuzzy_overrun_diagnostics(
        self,
        program_id=False,
        week_start=False,
        version=False,
        import_job_id=False,
        limit=500,
    ):
        """Per-schedule overrun breakdown for the current filters.

        Answers "why is everything flagged Overrun?" by showing, for
        every schedule in the current view, how many prelogs attached
        to it, its units_available, and the resulting overrun - plus
        the schedule's own matching attributes (rate / days / rotation)
        so an over-loose match is visible at a glance.

        Returns {'rows': [...], 'totals': {...}} sorted worst-first.
        """
        self._fuzzy_check_access()
        program, selected_week, selected_version = (
            self._fuzzy_validate_optional_filters(
                program_id, week_start, version,
            )
        )
        domain = self._fuzzy_prelog_domain(
            program.id if program else False,
            selected_week,
            selected_version,
            unmatched_only=False,
            include_removed=False,
            import_job_id=import_job_id,
        ) + [('schedule', '!=', False)]

        # Grouped count per schedule in one query.
        self.env.cr.execute(
            """
            SELECT p.schedule, COUNT(*)
              FROM mv_prelog_data p
             WHERE p.id IN %s
             GROUP BY p.schedule
             ORDER BY COUNT(*) DESC
            """,
            (tuple(self.search(domain).ids) or (0,),),
        )
        grouped = self.env.cr.fetchall()
        if not grouped:
            return {'rows': [], 'totals': {}}

        sched_ids = [row[0] for row in grouped][: max(int(limit or 500), 1)]
        counts = dict(grouped)
        schedules = self.env['mv.schedules'].browse(sched_ids).exists()

        rows = []
        total_over = 0
        for sched in schedules:
            attached = int(counts.get(sched.id, 0))
            cap = int(sched.units_available or 0)
            overrun = max(0, attached - cap)
            total_over += overrun
            days = ', '.join(
                sorted(
                    [d.name for d in sched.days_allowed if d.name],
                    key=lambda v: self._FUZZY_DAY_ORDER.get(
                        v[:3].strip().lower(), 99,
                    ),
                )
            )
            rows.append({
                'schedule_id': sched.id,
                'schedule': sched.display_name or '',
                'deal_number': (
                    sched.deal_parent.network_deal_number
                    if sched.deal_parent else ''
                ) or '',
                'week': (
                    fields.Date.to_string(sched.week) if sched.week else ''
                ),
                'status': sched.status or '',
                'rate': float(sched.rate or 0.0),
                'length': self._fuzzy_schedule_length(sched) or '',
                'rotation': '%s-%s' % (
                    self._fuzzy_selection_label(sched, 'start_time') or '',
                    self._fuzzy_selection_label(sched, 'end_time') or '',
                ),
                'days_allowed': days,
                'units_available': cap,
                'prelogs_attached': attached,
                'overrun': overrun,
            })
        rows.sort(key=lambda r: (-r['overrun'], -r['prelogs_attached']))
        return {
            'rows': rows,
            'totals': {
                'schedules': len(rows),
                'schedules_over': sum(1 for r in rows if r['overrun'] > 0),
                'prelogs_attached': sum(r['prelogs_attached'] for r in rows),
                'units_available': sum(r['units_available'] for r in rows),
                'overrun': total_over,
                'version': selected_version or '',
            },
        }

    @api.model
    def fuzzy_overrun_diagnostics_csv(
        self,
        program_id=False,
        week_start=False,
        version=False,
        import_job_id=False,
    ):
        """CSV of fuzzy_overrun_diagnostics for offline review."""
        data = self.fuzzy_overrun_diagnostics(
            program_id, week_start, version, import_job_id, limit=100000,
        )
        headers = [
            'Schedule', 'Deal #', 'Week', 'Sched Status', 'Rate', 'Length',
            'Rotation', 'Days Allowed', 'Units Available',
            'Prelogs Attached', 'Overrun',
        ]
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        for row in data['rows']:
            writer.writerow(self._fuzzy_csv_row([
                row['schedule'], row['deal_number'], row['week'],
                row['status'], row['rate'], row['length'], row['rotation'],
                row['days_allowed'], row['units_available'],
                row['prelogs_attached'], row['overrun'],
            ]))
        totals = data.get('totals') or {}
        writer.writerow([])
        writer.writerow(['TOTALS'])
        for key in (
            'schedules', 'schedules_over', 'prelogs_attached',
            'units_available', 'overrun', 'version',
        ):
            writer.writerow([key, totals.get(key, '')])
        return {
            'filename': 'PrelogOverrunDiagnostics-v%s.csv' % (
                totals.get('version') or 'all',
            ),
            'content': buf.getvalue(),
        }

    @api.model
    def fuzzy_workbench_overrun_details(
        self,
        schedule_id,
        limit=5,
        program_id=False,
        week_start=False,
        version=False,
        import_job_id=False,
    ):
        """Return the overrun payload for the drawer.

        Args:
          schedule_id: mv.schedules.id whose overrun context to fetch.
          limit: how many prelog rows to include in the preview list
                 (the drawer shows the first N and links to "view all").

        Returns:
          {
            'schedule_name': 'A-5170',
            'capped_units': 3,           # schedule.units_available
            'attached_count': 4,         # actually-attached prelogs
            'suggested_count': 0,        # pending suggestions
            'total_prelogs': 4,          # attached + suggested
            'overrun_amount': 1,         # max(0, total - cap)
            'prelogs': [
                {'id': 42, 'name': 'Pre-0013567',
                 'air_date': '2026-08-17', 'air_time': '11:23'},
                ...
            ],
            'has_more': True/False,
          }
        """
        self._fuzzy_check_access()
        try:
            schedule_id = int(schedule_id)
        except (TypeError, ValueError):
            return {}
        schedule = self.env['mv.schedules'].browse(schedule_id).exists()
        if not schedule:
            return {}

        # Version scoping: overrun is judged against a single upload
        # version (the latest re-includes all prior days' spots). Use
        # the passed version when present, else the latest for this
        # schedule's program + week.
        resolved_version = (
            version or self._mv_latest_prelog_version(schedule)
        )
        attached_domain = [
            ('schedule', '=', schedule.id),
            ('removed', '=', False),
        ]
        if resolved_version:
            attached_domain.append(('version', '=', resolved_version))
        attached = self.search(attached_domain, order='id asc')

        # Pending suggestions: prelogs that could realistically target
        # this schedule based on the deal-number join key. Restricted
        # to the same program/week/version so we don't drag in
        # historical prelogs.
        deal_number = (
            schedule.deal_parent.network_deal_number if schedule.deal_parent
            else ''
        )
        pending = self.env['mv.prelog_data']
        if deal_number:
            pending_domain = [
                ('schedule', '=', False),
                ('removed', '=', False),
                ('network_deal_number', '=', deal_number),
            ]
            if program_id:
                pending_domain.append(('import_program', '=', self._fuzzy_int(program_id)))
            if week_start:
                pending_domain.append(('import_week_value', '=', week_start))
            if resolved_version:
                pending_domain.append(('version', '=', resolved_version))
            if import_job_id:
                pending_domain.append(('import_job', '=', self._fuzzy_int(import_job_id)))
            pending = self.search(pending_domain, order='airdate asc, id asc')

        combined = attached + pending
        capped = int(schedule.units_available or 0)
        attached_count = len(attached)
        pending_count = len(pending)
        total = attached_count + pending_count
        overrun = max(0, total - capped)

        preview = combined[: max(limit, 1)]
        prelog_payload = [
            {
                'id': prelog.id,
                'name': prelog.display_name or '',
                'air_date': (
                    fields.Date.to_string(prelog.airdate)
                    if prelog.airdate else ''
                ),
                'air_time': prelog.scheduletime or '',
                'attached': bool(prelog.schedule),
            }
            for prelog in preview
        ]
        return {
            'schedule_id': schedule.id,
            'schedule_name': schedule.display_name or '',
            'deal_number': deal_number or '',
            # Every prelog id (attached + pending) so the "view all"
            # action can use a bulletproof [('id','in',[...])] domain
            # instead of reconstructing filter logic on the client.
            'all_prelog_ids': combined.ids,
            'capped_units': capped,
            'attached_count': attached_count,
            'pending_count': pending_count,
            'total_prelogs': total,
            'overrun_amount': overrun,
            'prelogs': prelog_payload,
            'has_more': total > len(preview),
        }

    @api.model
    def fuzzy_match_apply(
        self,
        selections,
        program_id=False,
        week_start=False,
        version=False,
    ):
        """Attach selected schedules directly to unmatched Prelog Data.

        Every selection is validated before the first write, which keeps the
        operation all-or-none. Suggested schedules must still be sold and in
        the same Program/week. Manual overrides are deliberately more
        permissive, but must be explicitly confirmed by the caller.
        """
        self._fuzzy_check_access()
        selections = selections or []
        if not isinstance(selections, list) or not selections:
            raise UserError(_('Select at least one Prelog Data row to attach.'))
        filter_values = (program_id, week_start, version)
        expected_filter = False
        if any(value not in (False, None, '') for value in filter_values):
            expected_filter = self._fuzzy_validate_optional_filters(
                program_id,
                week_start,
                version,
            )

        normalized = []
        seen_prelog_ids = set()
        for item in selections:
            if not isinstance(item, dict):
                raise UserError(_('A schedule selection has an invalid format.'))
            prelog_id = self._fuzzy_int(item.get('prelog_id'))
            if not prelog_id or prelog_id in seen_prelog_ids:
                raise UserError(_('Each selected Prelog Data row must be unique.'))
            seen_prelog_ids.add(prelog_id)
            normalized.append({
                'prelog_id': prelog_id,
                'schedule_id': self._fuzzy_int(item.get('schedule_id')),
                'schedule_ref': (item.get('schedule_ref') or '').strip(),
                'source': item.get('source') or 'suggested',
                'confirmed_override': bool(item.get('confirmed_override')),
                'replace_existing': bool(item.get('replace_existing')),
            })

        prelogs = self.search([('id', 'in', list(seen_prelog_ids))])
        prelogs_by_id = {prelog.id: prelog for prelog in prelogs}
        prepared = []
        errors = []

        for item in normalized:
            prelog = prelogs_by_id.get(item['prelog_id'])
            if not prelog:
                errors.append(
                    _('Prelog Data ID %(id)s was not found or is not accessible.')
                    % {'id': item['prelog_id']}
                )
                continue
            if prelog.schedule and not (
                item['source'] == 'manual'
                and item['replace_existing']
                and item['confirmed_override']
            ):
                errors.append(
                    _('%(prelog)s is already matched. Use Replace Schedule to change it.')
                    % {'prelog': prelog.display_name}
                )
                continue
            if expected_filter:
                expected_program, expected_week, expected_version = (
                    expected_filter
                )
                if any((
                    expected_program and prelog.import_program != expected_program,
                    expected_week and prelog.import_week_value != expected_week,
                    expected_version and prelog.version != expected_version,
                )):
                    errors.append(
                        _(
                            '%(prelog)s no longer belongs to the active '
                            'Program, week, and version filters. Refresh the '
                            'page and try again.'
                        )
                        % {'prelog': prelog.display_name}
                    )
                    continue

            schedule, resolution_error = self._fuzzy_resolve_schedule(item)
            if resolution_error:
                errors.append(
                    _('%(prelog)s: %(error)s')
                    % {'prelog': prelog.display_name, 'error': resolution_error}
                )
                continue

            if item['source'] == 'suggested':
                if schedule.status != 'sold':
                    errors.append(
                        _('%(prelog)s: only a sold schedule can be attached as a suggestion.')
                        % {'prelog': prelog.display_name}
                    )
                    continue
                analysis = self._fuzzy_analyze_schedule(
                    prelog,
                    prelog.import_program,
                    schedule,
                )
                if not all(
                    analysis[key]
                    for key in (
                        'network_match',
                        'deal_match',
                        'day_match',
                    )
                ):
                    errors.append(
                        _(
                            '%(prelog)s: the suggested schedule no longer '
                            'meets the network, deal, and day criteria.'
                        )
                        % {'prelog': prelog.display_name}
                    )
                    continue
                if (
                    (
                        not analysis['time_match']
                        or not analysis['rate_match']
                        or not analysis['length_match']
                    )
                    and not item['confirmed_override']
                ):
                    errors.append(
                        _('%(prelog)s: confirm the time, rate, or length mismatch before attaching.')
                        % {'prelog': prelog.display_name}
                    )
                    continue
                if (
                    not prelog.import_program
                    or schedule.deal_parent.program != prelog.import_program
                    or schedule.week != prelog.import_week_value
                ):
                    errors.append(
                        _('%(prelog)s: the suggested schedule no longer belongs to the same Program and week.')
                        % {'prelog': prelog.display_name}
                    )
                    continue
            elif item['source'] == 'manual':
                if not item['confirmed_override']:
                    errors.append(
                        _('%(prelog)s: confirm the manual schedule override before attaching.')
                        % {'prelog': prelog.display_name}
                    )
                    continue
            else:
                errors.append(
                    _('%(prelog)s: unknown selection source.')
                    % {'prelog': prelog.display_name}
                )
                continue

            prepared.append((prelog, schedule, item['source'], prelog.schedule))

        if errors:
            raise UserError('\n'.join(errors))

        now = fields.Datetime.now()
        user_name = self.env.user.display_name
        touched_schedule_ids = set()
        for prelog, schedule, source, previous_schedule in prepared:
            audit_line = _(
                'Schedule %(schedule)s attached from Prelog Fuzzy Matching / Operations Workbench '
                '(%(source)s) by %(user)s on %(date)s.'
            ) % {
                'schedule': schedule.display_name,
                'source': _('manual override') if source == 'manual' else _('suggestion'),
                'user': user_name,
                'date': fields.Datetime.to_string(now),
            }
            if previous_schedule:
                audit_line = _(
                    'Schedule %(previous)s replaced with %(schedule)s from '
                    'Prelog Operations Workbench by %(user)s on %(date)s.'
                ) % {
                    'previous': previous_schedule.display_name,
                    'schedule': schedule.display_name,
                    'user': user_name,
                    'date': fields.Datetime.to_string(now),
                }
                touched_schedule_ids.add(previous_schedule.id)
            detail = '\n'.join(
                part
                for part in (prelog.import_match_detail, audit_line)
                if part
            )
            prelog.write({
                'schedule': schedule.id,
                'import_match_status': 'matched',
                'suggested_schedule': False,
                'possible_schedules': False,
                'match_flags': '',
                'info': False,
                'import_match_detail': detail,
            })
            touched_schedule_ids.add(schedule.id)

        # Overrun recalc for every schedule that gained OR lost a prelog.
        if touched_schedule_ids:
            self._recompute_prelog_overruns(touched_schedule_ids)

        return {
            'attached': len(prepared),
            'message': _('%(count)s schedule(s) successfully attached.')
            % {'count': len(prepared)},
        }

    @api.model
    def fuzzy_match_set_removed(
        self,
        prelog_ids,
        removed,
        program_id=False,
        week_start=False,
        version=False,
        import_job_id=False,
    ):
        """Soft-remove rows; removing always clears the attached Schedule ID."""
        self._fuzzy_check_access()
        prelogs = self._fuzzy_validate_selected_prelogs(
            prelog_ids,
            program_id,
            week_start,
            version,
            import_job_id,
        )
        return self._fuzzy_set_removed_records(prelogs, bool(removed))

    @api.model
    def _fuzzy_set_removed_records_recalc_hook(self, touched_ids):
        # Split out so subclasses/tests can extend without duplicating.
        if touched_ids:
            self._recompute_prelog_overruns(touched_ids)

    @api.model
    def _fuzzy_set_removed_records(self, prelogs, removed):
        """Implement remove/unremove for both explicit and all-page actions."""
        now = fields.Datetime.now()
        touched_schedule_ids = set()
        restored = self.browse()
        for prelog in prelogs:
            previous_schedule_id = prelog.schedule.id if prelog.schedule else False
            previous_schedule = prelog.schedule.display_name if prelog.schedule else ''
            if removed:
                line = _(
                    'Removed from Prelog Operations Workbench by %(user)s on %(date)s.'
                ) % {
                    'user': self.env.user.display_name,
                    'date': fields.Datetime.to_string(now),
                }
                if previous_schedule:
                    line += ' ' + _(
                        'Cleared Schedule %(schedule)s.'
                    ) % {'schedule': previous_schedule}
            else:
                line = _(
                    'Unremoved from Prelog Operations Workbench by %(user)s on %(date)s; '
                    'schedule suggestions will be recalculated.'
                ) % {
                    'user': self.env.user.display_name,
                    'date': fields.Datetime.to_string(now),
                }
            detail = '\n'.join(
                part for part in (prelog.import_match_detail, line) if part
            )
            prelog.write({
                'removed': removed,
                'schedule': False,
                'import_match_status': 'unmatched',
                'suggested_schedule': False,
                'possible_schedules': False,
                'match_flags': False,
                'info': False,
                'import_match_detail': detail,
            })
            if previous_schedule_id:
                touched_schedule_ids.add(previous_schedule_id)
            if not removed:
                restored |= prelog

        if restored:
            self._prelog_store_matching(restored, False, False, attach=True)

        # Any schedule that lost an attachment needs its overrun recomputed.
        self._fuzzy_set_removed_records_recalc_hook(touched_schedule_ids)

        return {
            'updated': len(prelogs),
            'message': _('%(count)s Prelog row(s) %(action)s.') % {
                'count': len(prelogs),
                'action': _('removed') if removed else _('unremoved'),
            },
        }

    @api.model
    def fuzzy_workbench_bulk_action(
        self,
        action_name,
        selection,
        program_id=False,
        week_start=False,
        version=False,
        status='all',
        search_term='',
        air_date=False,
        issue_filter='',
        sort_by='air_date',
        import_job_id=False,
        confirmed_fuzzy=False,
        sort_direction='asc',
    ):
        """Apply a Workbench action to explicit rows or every filtered row."""
        self._fuzzy_check_access()
        action_name = (action_name or '').strip().lower()
        if action_name not in {'attach', 'remove', 'unremove', 'delete'}:
            raise UserError(_('Unknown Prelog Workbench bulk action.'))

        prelogs, rows = self._fuzzy_resolve_workbench_selection(
            selection,
            program_id,
            week_start,
            version,
            status,
            search_term,
            air_date,
            issue_filter,
            sort_by,
            import_job_id,
            sort_direction,
        )
        if action_name == 'attach':
            row_by_id = {row['id']: row for row in rows}
            attachable = [
                row_by_id[prelog.id]
                for prelog in prelogs
                if (
                    row_by_id[prelog.id]['status'] == 'suggestion'
                    and row_by_id[prelog.id].get('suggested')
                    and row_by_id[prelog.id].get('suggestion_attachable')
                )
            ]
            fuzzy_count = sum(
                row.get('match_quality') != 'exact' for row in attachable
            )
            if fuzzy_count and not confirmed_fuzzy:
                return {
                    'requires_confirmation': True,
                    'selected': len(prelogs),
                    'attachable': len(attachable),
                    'fuzzy': fuzzy_count,
                }
            if not attachable:
                return {
                    'attached': 0,
                    'skipped': len(prelogs),
                    'message': _('No selected rows have an attachable suggestion.'),
                }
            payload = [{
                'prelog_id': row['id'],
                'schedule_id': row['suggested']['id'],
                'source': 'suggested',
                'confirmed_override': row.get('match_quality') != 'exact',
            } for row in attachable]
            result = self.fuzzy_match_apply(
                payload,
                program_id,
                week_start,
                version,
            )
            result['skipped'] = len(prelogs) - len(attachable)
            if result['skipped']:
                result['message'] += ' ' + _(
                    '%(count)s selected row(s) without an attachable suggestion were skipped.'
                ) % {'count': result['skipped']}
            return result

        if action_name in {'remove', 'unremove'}:
            return self._fuzzy_set_removed_records(
                prelogs,
                action_name == 'remove',
            )

        deleted = len(prelogs)
        prelogs.unlink()
        return {
            'deleted': deleted,
            'message': _('%(count)s Prelog row(s) permanently deleted.')
            % {'count': deleted},
        }

    @api.model
    def fuzzy_match_detach(
        self,
        prelog_ids,
        program_id=False,
        week_start=False,
        version=False,
        import_job_id=False,
    ):
        self._fuzzy_check_access()
        prelogs = self._fuzzy_validate_selected_prelogs(
            prelog_ids,
            program_id,
            week_start,
            version,
            import_job_id,
        )
        now = fields.Datetime.now()
        detached = 0
        touched_schedule_ids = set()
        for prelog in prelogs.filtered('schedule'):
            prev_id = prelog.schedule.id
            line = _(
                'Schedule %(schedule)s detached in Prelog Operations Workbench '
                'by %(user)s on %(date)s.'
            ) % {
                'schedule': prelog.schedule.display_name,
                'user': self.env.user.display_name,
                'date': fields.Datetime.to_string(now),
            }
            prelog.write({
                'schedule': False,
                'import_match_status': 'unmatched',
                'import_match_detail': '\n'.join(
                    part for part in (prelog.import_match_detail, line) if part
                ),
            })
            self._prelog_store_matching(
                prelog,
                prelog.import_program,
                prelog.import_week_value,
                attach=False,
            )
            touched_schedule_ids.add(prev_id)
            detached += 1
        if touched_schedule_ids:
            self._recompute_prelog_overruns(touched_schedule_ids)
        return {
            'updated': detached,
            'message': _('%(count)s schedule(s) detached.') % {'count': detached},
        }

    @api.model
    def fuzzy_match_row(self, prelog_id):
        """Return one stored row so an open Review drawer can be refreshed."""
        self._fuzzy_check_access()
        prelog = self.browse(self._fuzzy_int(prelog_id)).exists()
        return self._prelog_stored_row(prelog) if prelog else False

    @api.model
    def fuzzy_match_refresh(
        self,
        program_id=False,
        week_start=False,
        version=False,
        import_job_id=False,
    ):
        """Re-run the import matcher for stored unmatched rows only.

        This picks up Schedule corrections without recalculating anything during
        an ordinary page load. Existing attachments are never changed.
        """
        self._fuzzy_check_access()
        program, selected_week, selected_version = (
            self._fuzzy_validate_optional_filters(
                program_id, week_start, version,
            )
        )
        domain = self._fuzzy_prelog_domain(
            program.id if program else False,
            selected_week,
            selected_version,
            unmatched_only=False,
            include_removed=False,
            import_job_id=import_job_id,
        ) + [('import_match_status', '=', 'unmatched')]
        prelogs = self.search(domain)
        if not prelogs:
            return {
                'checked': 0,
                'attached': 0,
                'unmatched': 0,
                'message': _('Nothing to re-check - no unmatched Prelog rows.'),
            }

        touched_before = set(prelogs.mapped('schedule').ids)
        self._prelog_store_matching(
            prelogs, program, selected_week, attach=True,
        )
        attached = prelogs.filtered('schedule')
        touched_after = set(attached.mapped('schedule').ids)
        if touched_before or touched_after:
            self._recompute_prelog_overruns(touched_before | touched_after)

        if attached:
            now = fields.Datetime.now()
            for prelog in attached:
                line = _(
                    'Schedule %(schedule)s attached by Refresh in Prelog '
                    'Workbench by %(user)s on %(date)s - Schedule corrected '
                    'after import.'
                ) % {
                    'schedule': prelog.schedule.display_name,
                    'user': self.env.user.display_name,
                    'date': fields.Datetime.to_string(now),
                }
                prelog.import_match_detail = '\n'.join(
                    part for part in (prelog.import_match_detail, line) if part
                )

        return {
            'checked': len(prelogs),
            'attached': len(attached),
            'unmatched': len(prelogs) - len(attached),
            'message': _(
                'Re-checked %(checked)s row(s): %(attached)s attached, '
                '%(unmatched)s still unmatched.'
            ) % {
                'checked': len(prelogs),
                'attached': len(attached),
                'unmatched': len(prelogs) - len(attached),
            },
        }

    @api.model
    def fuzzy_match_export_csv(self, program_id, week_start, version):
        """Build the filtered exception report as a downloadable CSV."""
        self._fuzzy_check_access()
        program, selected_week, selected_version = self._fuzzy_validate_filters(
            program_id,
            week_start,
            version,
        )
        prelogs = self.search(
            self._fuzzy_prelog_domain(
                program.id,
                selected_week,
                selected_version,
                unmatched_only=False,
            ),
            order='airdate asc, scheduletime asc, id asc',
        )
        rows = self._fuzzy_build_rows(
            prelogs,
            program,
            selected_week,
            use_attached=True,
        )

        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow([
            'Name',
            'Network',
            'Air Date',
            'Day',
            'Air Time',
            'Prelog Length',
            'Rate',
            'Week',
            'Network Deal #',
            'Adv/Product',
            'Reason',
        ])
        exported = 0
        for row in rows:
            if not (
                row['reason']
                or row['time_mismatch']
                or row['length_mismatch']
            ):
                continue
            writer.writerow(self._fuzzy_csv_row([
                row['name'],
                row['network'],
                row['air_date'],
                row['day'],
                row['air_time'],
                row['length'],
                row['rate'],
                row['week'],
                row['deal_number'],
                row['advertiser_product'],
                row['reason'],
            ]))
            exported += 1

        safe_program = re.sub(
            r'[^A-Za-z0-9_-]+',
            '-',
            program.display_name,
        ).strip('-')
        filename = 'PrelogFuzzyMatching-%s-%s-v%s.csv' % (
            safe_program or 'Program',
            fields.Date.to_string(selected_week),
            selected_version,
        )
        return {
            'filename': filename,
            'content': output.getvalue(),
            'count': exported,
        }

    @api.model
    def fuzzy_workbench_export_csv(
        self,
        program_id,
        week_start,
        version,
        status='all',
        search_term='',
        air_date=False,
        issue_filter='',
        sort_by='air_date',
        import_job_id=False,
        sort_direction='asc',
    ):
        """Export the active workbench tab and its current filters."""
        self._fuzzy_check_access()
        program, selected_week, selected_version = self._fuzzy_validate_optional_filters(
            program_id, week_start, version
        )
        prelogs = self.search(
            self._prelog_stored_domain(
                self._fuzzy_prelog_domain(
                    program.id if program else False,
                    selected_week,
                    selected_version,
                    unmatched_only=False,
                    include_removed=status == 'removed',
                    import_job_id=import_job_id,
                ),
                status,
                issue_filter,
                air_date,
                search_term,
            ),
            order=self._prelog_stored_order(sort_by, sort_direction),
        )
        rows = [self._prelog_stored_row(prelog) for prelog in prelogs]

        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow([
            'Prelog', 'Status', 'Match Quality', 'Network', 'Air Date',
            'Air Time', 'Length', 'Rate', 'Week', 'Network Deal #',
            'Adv/Product', 'Schedule', 'Info',
        ])
        for row in rows:
            schedule = row.get('attached') or {}
            writer.writerow(self._fuzzy_csv_row([
                row['name'], row['status_label'], row['match_quality_label'],
                row['network'], row['air_date'], row['air_time'], row['length'],
                row['rate'], row['week'], row['deal_number'],
                row['advertiser_product'], schedule.get('name', ''), row['info'],
            ]))
        safe_program = re.sub(
            r'[^A-Za-z0-9_-]+',
            '-',
            program.display_name if program else 'All-Programs',
        ).strip('-')
        filename = 'PrelogWorkbench-%s-%s-v%s-%s.csv' % (
            safe_program or 'All-Programs',
            fields.Date.to_string(selected_week) if selected_week else 'All-Weeks',
            selected_version or 'All',
            status,
        )
        return {'filename': filename, 'content': output.getvalue(), 'count': len(rows)}

    # ------------------------------------------------------------------
    # Query and result construction
    # ------------------------------------------------------------------

    @api.model
    def _fuzzy_check_access(self):
        if not self.env.user.has_group(
            'marathon_ventures.group_prelog_fuzzy_matching'
        ):
            raise UserError(
                _(
                    'You do not have permission to use Prelog Fuzzy Matching. '
                    'Ask an administrator for the Prelog Fuzzy Matching / '
                    'Operator access right.'
                )
            )

    @api.model
    def _fuzzy_latest_user_upload(self):
        """Return metadata only; the uploaded filename is intentionally hidden."""
        if 'mv.prelog_import_job' not in self.env.registry.models:
            return False
        job = self.env['mv.prelog_import_job'].search([
            ('state', '=', 'completed'),
            ('submitted_by_id', '=', self.env.user.id),
            ('prelog_ids', '!=', False),
        ], order='finished_at desc, id desc', limit=1)
        if not job:
            return False
        return {
            'id': job.id,
            'program_id': job.program_id.id,
            'program_name': job.program_id.display_name,
            'week_start': fields.Date.to_string(job.import_week),
            'version': job.prelog_version,
            'submitted_at': (
                fields.Datetime.to_string(job.finished_at or job.create_date)
            ),
            'submitted_by': job.submitted_by_id.display_name,
            'row_count': len(job.prelog_ids),
            # Authoritative "as uploaded" figure, so the workbench total
            # can be verified against the file. NOTE: the job sums the
            # rate of EVERY parsed row, including ones that then failed
            # to create - so when error_count > 0 this legitimately
            # exceeds the live sum over stored rows. error_count is
            # exposed alongside it so the UI can explain a gap rather
            # than look broken.
            'total_rate_amount': job.total_rate_amount or 0.0,
            'matched_rate_amount': job.matched_rate_amount or 0.0,
            'unmatched_rate_amount': job.unmatched_rate_amount or 0.0,
            'error_count': job.error_count or 0,
            'total_row_count': job.total_row_count or 0,
        }

    @api.model
    def _fuzzy_validate_filters(self, program_id, week_start, version):
        parsed_program_id = self._fuzzy_int(program_id)
        program = self.env['mv.programs'].search(
            [('id', '=', parsed_program_id)],
            limit=1,
        )
        if not program:
            raise UserError(_('Select a valid Program.'))
        try:
            selected_week = fields.Date.to_date(week_start)
        except (TypeError, ValueError):
            selected_week = False
        if not selected_week:
            raise UserError(_('Select a valid week start date.'))
        if selected_week.weekday() != 0:
            raise UserError(
                _('Week must be the Monday that starts the broadcast week.')
            )
        selected_version = self._fuzzy_int(version)
        if not selected_version or selected_version < 1:
            raise UserError(_('Select a valid Prelog version.'))
        return program, selected_week, selected_version

    @api.model
    def _fuzzy_validate_optional_filters(self, program_id, week_start, version):
        """Validate each Workbench filter independently; blank means all."""
        program = False
        if program_id not in (False, None, '', 0, '0'):
            parsed_program_id = self._fuzzy_int(program_id)
            program = self.env['mv.programs'].search(
                [('id', '=', parsed_program_id)],
                limit=1,
            )
            if not program:
                raise UserError(_('Select a valid Program.'))

        selected_week = False
        if week_start not in (False, None, ''):
            try:
                selected_week = fields.Date.to_date(week_start)
            except (TypeError, ValueError):
                selected_week = False
            if not selected_week:
                raise UserError(_('Select a valid week start date.'))
            if selected_week.weekday() != 0:
                raise UserError(
                    _('Week must be the Monday that starts the broadcast week.')
                )

        selected_version = False
        if version not in (False, None, '', 0, '0'):
            selected_version = self._fuzzy_int(version)
            if not selected_version or selected_version < 1:
                raise UserError(_('Select a valid Prelog version.'))
        return program, selected_week, selected_version

    @api.model
    def _fuzzy_prelog_domain(
        self,
        program_id,
        selected_week,
        version,
        unmatched_only=True,
        include_removed=False,
        import_job_id=False,
    ):
        domain = []
        if version:
            domain.append(('version', '=', version))
        if program_id:
            domain.append(('import_program', '=', program_id))
        if selected_week:
            domain.append(('import_week_value', '=', selected_week))
        if not include_removed:
            domain.insert(0, ('removed', '=', False))
        if unmatched_only:
            domain.insert(0, ('schedule', '=', False))
        if import_job_id:
            domain.append(('import_job', '=', self._fuzzy_int(import_job_id)))
        return domain

    # ------------------------------------------------------------------
    # Stored matching, written by import and explicit Refresh
    # ------------------------------------------------------------------

    _PRELOG_FLAG_TOKENS = (
        'day', 'time', 'time_buffer', 'rate', 'length', 'ambiguous',
        'missing_deal', 'missing_air_date', 'missing_air_time',
        'no_schedules', 'network',
    )

    _PRELOG_FLAG_DOMAIN = {
        'day': ('day',),
        'time': ('time', 'time_buffer', 'missing_air_time'),
        'length': ('length',),
        'rate': ('rate',),
        'ambiguous': ('ambiguous',),
        'missing_deal': ('missing_deal',),
    }

    _PRELOG_SORT_COLUMNS = {
        'air_date': 'airdate %(d)s, scheduletime %(d)s, id %(d)s',
        'name': 'name %(d)s, id %(d)s',
        'network': 'broadcast_network %(d)s, network %(d)s, id %(d)s',
        'length': 'schedulelength %(d)s, id %(d)s',
        'rate': 'rate %(d)s, id %(d)s',
        'deal_number': 'network_deal_number %(d)s, id %(d)s',
        'advertiser_product': 'advertiserproduct %(d)s, id %(d)s',
        'status': 'import_match_status %(d)s, is_overrun %(d)s, id %(d)s',
        'schedule': 'schedule %(d)s, id %(d)s',
        'info': 'info %(d)s, id %(d)s',
        # Compatibility with clients loaded before the Reason -> Info rename.
        'reason': 'info %(d)s, id %(d)s',
    }

    @api.model
    def _prelog_flags(self, tokens):
        unknown = [token for token in tokens if token not in self._PRELOG_FLAG_TOKENS]
        if unknown:
            raise ValueError('Unknown match flag(s): %s' % ', '.join(unknown))
        return (',%s,' % ','.join(tokens)) if tokens else ''

    @api.model
    def _prelog_analysis_flags(self, analysis):
        tokens = []
        if not analysis['day_match']:
            tokens.append('day')
        if not analysis['exact_time_match']:
            tokens.append('time_buffer' if analysis['time_match'] else 'time')
        if not analysis['rate_match']:
            tokens.append('rate')
        if not analysis['length_match']:
            tokens.append('length')
        return tokens

    @api.model
    def _prelog_candidate_payload(self, analysis):
        payload = self._fuzzy_schedule_payload(analysis['schedule'])
        flags = self._prelog_analysis_flags(analysis)
        payload.update({
            'flags': self._prelog_flags(flags),
            'why': ', '.join(flags) or _('matches on every check'),
            'attachable': analysis['schedule'].status == 'sold',
            'day_mismatch': not analysis['day_match'],
            'time_mismatch': not analysis['time_match'],
            'rate_mismatch': not analysis['rate_match'],
            'length_mismatch': not analysis['length_match'],
            'time_distance': analysis['time_distance'],
            'exact_time_match': analysis['exact_time_match'],
            'exact': not flags,
        })
        return payload

    @api.model
    def _prelog_store_matching(self, prelogs, program, week, attach=True):
        """Persist attachment, suggestions, flags and Info for each row."""
        groups = {}
        contexts = {}
        for prelog in prelogs.filtered(lambda row: not row.removed):
            row_program = prelog.import_program or program
            row_week = prelog.import_week_value or week
            key = (
                prelog.version,
                row_program.id if row_program else False,
                row_week,
            )
            groups.setdefault(key, []).append(prelog.id)
            contexts[key] = (row_program, row_week)

        counts = {'matched': 0, 'unmatched': 0}
        for key, grouped_ids in groups.items():
            row_program, row_week = contexts[key]
            part = self._prelog_store_matching_group(
                self.browse(grouped_ids), row_program, row_week, attach=attach,
            )
            counts['matched'] += part['matched']
            counts['unmatched'] += part['unmatched']
        return counts

    @api.model
    def _prelog_store_matching_group(self, prelogs, program, week, attach=True):
        if not program or not week:
            return {'matched': 0, 'unmatched': 0}

        candidate_map = self._fuzzy_candidate_map(prelogs, program, week)
        accepted_networks = self._fuzzy_network_names(program)
        counts = {'matched': 0, 'unmatched': 0}

        for prelog in prelogs:
            deal_number = (prelog.network_deal_number or '').strip()
            if not deal_number:
                prelog.write(self._prelog_unresolved_vals('missing_deal'))
                counts['unmatched'] += 1
                continue
            if not prelog.airdate:
                prelog.write(self._prelog_unresolved_vals('missing_air_date'))
                counts['unmatched'] += 1
                continue
            if not (prelog.scheduletime or '').strip():
                prelog.write(self._prelog_unresolved_vals('missing_air_time'))
                counts['unmatched'] += 1
                continue

            analyses = [
                self._fuzzy_analyze_schedule(
                    prelog, program, schedule, accepted_networks,
                )
                for schedule in candidate_map.get(deal_number, [])
            ]
            eligible = [
                analysis for analysis in analyses
                if (
                    analysis['network_match']
                    and analysis['day_match']
                )
            ]
            if not eligible:
                token = self._prelog_no_suggestion_token(analyses)
                prelog.write(self._prelog_unresolved_vals(token))
                counts['unmatched'] += 1
                continue

            eligible.sort(key=self._fuzzy_analysis_sort_key)
            best = eligible[0]
            winning_key = self._fuzzy_analysis_quality_key(best)
            tied = sum(
                1 for analysis in eligible
                if self._fuzzy_analysis_quality_key(analysis) == winning_key
            )
            flags = self._prelog_analysis_flags(best)
            if tied > 1:
                flags = flags + ['ambiguous']
            is_clean = not flags

            if attach and is_clean and best['schedule'].status == 'sold':
                prelog.write({
                    'schedule': best['schedule'].id,
                    'suggested_schedule': False,
                    'possible_schedules': False,
                    'match_flags': self._prelog_flags(flags),
                    'info': False,
                    'import_match_status': 'matched',
                })
                counts['matched'] += 1
                continue

            offered = [
                analysis
                for analysis in eligible
                if len(self._prelog_analysis_flags(analysis))
                <= self._FUZZY_MAX_ALTERNATIVE_DIFFERENCES
            ][:self._FUZZY_MAX_ALTERNATIVES + 1] or [best]
            prelog.write({
                'schedule': False,
                'suggested_schedule': best['schedule'].id,
                'possible_schedules': [
                    self._prelog_candidate_payload(analysis)
                    for analysis in offered
                ],
                'match_flags': self._prelog_flags(flags),
                'info': self._prelog_info_text(flags, len(offered)),
                'import_match_status': 'unmatched',
                'is_overrun': False,
            })
            counts['unmatched'] += 1

        return counts

    @api.model
    def _prelog_no_suggestion_token(self, analyses):
        if not analyses:
            return 'no_schedules'
        network_matches = [a for a in analyses if a['network_match']]
        if not network_matches:
            return 'network'
        if not any(a['day_match'] for a in network_matches):
            return 'day'
        return 'time'

    @api.model
    def _prelog_info_text(self, flags, suggestion_count):
        labels = {
            'missing_deal': _('Missing deal number'),
            'missing_air_date': _('Missing air date'),
            'missing_air_time': _('Missing air time'),
            'no_schedules': _('No schedules found for deal number'),
            'network': _('No network match'),
            'rate': _('No rate match'),
            'day': _('No day match'),
            'time': _('No time match'),
        }
        for token in (
            'missing_deal', 'missing_air_date', 'missing_air_time',
            'no_schedules', 'network', 'rate', 'day',
        ):
            if token in flags and not suggestion_count:
                return labels[token]
        if suggestion_count:
            return _('%(count)s suggestion(s)') % {'count': suggestion_count}
        return labels.get(flags[0], '') if flags else ''

    @api.model
    def _prelog_unresolved_vals(self, token):
        return {
            'schedule': False,
            'suggested_schedule': False,
            'possible_schedules': False,
            'import_match_status': 'unmatched',
            'match_flags': self._prelog_flags([token]),
            'info': self._prelog_info_text([token], 0),
            'is_overrun': False,
        }

    @api.model
    def _prelog_stored_order(self, sort_by, sort_direction):
        direction = 'desc' if str(sort_direction).lower() == 'desc' else 'asc'
        template = self._PRELOG_SORT_COLUMNS.get(
            sort_by, self._PRELOG_SORT_COLUMNS['air_date'],
        )
        return template % {'d': direction}

    @api.model
    def _prelog_stored_domain(
        self, base_domain, status, issue_filter, air_date, search_term,
    ):
        domain = list(base_domain)
        if status == 'matched':
            domain += [
                ('import_match_status', '=', 'matched'),
                ('is_overrun', '=', False),
            ]
        elif status == 'unmatched':
            domain += [('import_match_status', '=', 'unmatched')]
        elif status == 'suggestions':
            domain += [
                ('import_match_status', '=', 'unmatched'),
                ('suggested_schedule', '!=', False),
            ]
        elif status == 'no_suggestion':
            domain += [
                ('import_match_status', '=', 'unmatched'),
                ('suggested_schedule', '=', False),
            ]
        elif status == 'removed':
            domain = [term for term in domain if term != ('removed', '=', False)]
            domain += [('removed', '=', True)]
        elif status == 'overruns':
            domain += [('is_overrun', '=', True)]

        tokens = self._PRELOG_FLAG_DOMAIN.get(issue_filter)
        if tokens:
            domain += ['|'] * (len(tokens) - 1)
            domain += [
                ('match_flags', 'ilike', ',%s,' % token) for token in tokens
            ]

        if air_date:
            try:
                parsed = fields.Date.to_date(air_date)
            except (TypeError, ValueError):
                parsed = False
            if parsed:
                domain += [('airdate', '=', parsed)]

        term = (search_term or '').strip()
        if term:
            domain += [
                '|', '|', '|', '|',
                ('name', 'ilike', term),
                ('advertiserproduct', 'ilike', term),
                ('network_deal_number', 'ilike', term),
                ('schedule.name', 'ilike', term),
                ('suggested_schedule.name', 'ilike', term),
            ]
        return domain

    @api.model
    def _prelog_stored_counts(self, base_domain):
        counts = {
            'all': 0,
            'matched': 0,
            'unmatched': 0,
            'suggestions': 0,
            'no_suggestion': 0,
            'removed': 0,
            'overruns': 0,
        }
        counts['removed'] = self.search_count(
            [term for term in base_domain if term != ('removed', '=', False)]
            + [('removed', '=', True)]
        )
        groups = self._read_group(
            base_domain,
            ['import_match_status', 'is_overrun'],
            ['__count'],
        )
        for status, is_overrun, count in groups:
            counts['all'] += count
            if status == 'unmatched':
                counts['unmatched'] += count
            elif is_overrun:
                counts['overruns'] += count
            elif status == 'matched':
                counts['matched'] += count
        counts['suggestions'] = self.search_count(
            base_domain
            + [('import_match_status', '=', 'unmatched')]
            + [('suggested_schedule', '!=', False)]
        )
        counts['no_suggestion'] = counts['unmatched'] - counts['suggestions']
        return counts

    @api.model
    def _prelog_stored_rate_sum(self, domain):
        """Return a database-backed rate total without loading result rows."""
        groups = self._read_group(domain, [], ['rate:sum'])
        return round(float(groups[0][0] or 0.0), 2) if groups else 0.0

    @api.model
    def _prelog_stored_dollar_totals(self, base_domain):
        """Return full-scope rate totals for each stored Workbench tab."""
        totals = {
            'all': 0.0,
            'matched': 0.0,
            'unmatched': 0.0,
            'suggestions': 0.0,
            'no_suggestion': 0.0,
            'removed': 0.0,
            'overruns': 0.0,
        }
        groups = self._read_group(
            base_domain,
            ['import_match_status', 'is_overrun', 'suggested_schedule'],
            ['rate:sum'],
        )
        for status, is_overrun, suggested_schedule, rate_sum in groups:
            amount = float(rate_sum or 0.0)
            totals['all'] += amount
            if status == 'unmatched':
                totals['unmatched'] += amount
                bucket = 'suggestions' if suggested_schedule else 'no_suggestion'
                totals[bucket] += amount
            elif is_overrun:
                totals['overruns'] += amount
            elif status == 'matched':
                totals['matched'] += amount

        removed_domain = [
            term for term in base_domain if term != ('removed', '=', False)
        ] + [('removed', '=', True)]
        totals['removed'] = self._prelog_stored_rate_sum(removed_domain)
        return {key: round(value, 2) for key, value in totals.items()}

    @api.model
    def _prelog_stored_row(self, prelog):
        candidates = list(prelog.possible_schedules or [])
        flags = [token for token in (prelog.match_flags or '').split(',') if token]
        suggested = candidates[0] if candidates else (
            self._fuzzy_schedule_payload(prelog.suggested_schedule)
            if prelog.suggested_schedule else False
        )
        attached = (
            self._fuzzy_schedule_payload(prelog.schedule)
            if prelog.schedule else False
        )

        if prelog.removed:
            status = 'removed'
        elif prelog.is_overrun:
            status = 'overrun'
        elif prelog.import_match_status == 'matched' and prelog.schedule:
            status = 'matched'
        elif prelog.suggested_schedule:
            status = 'suggestion'
        else:
            status = 'no_suggestion'

        hard = [flag for flag in flags if flag != 'ambiguous']
        return {
            'id': prelog.id,
            'name': prelog.display_name or '',
            'network': (
                prelog.broadcast_network
                or prelog.network
                or (prelog.import_program.display_name if prelog.import_program else '')
                or ''
            ),
            'version': prelog.version or '',
            'air_date': fields.Date.to_string(prelog.airdate) if prelog.airdate else '',
            'day': prelog.airdate.strftime('%a') if prelog.airdate else '',
            'air_time': prelog.scheduletime or '',
            'length': prelog.schedulelength or '',
            'rate': prelog.rate or 0.0,
            'week': (
                fields.Date.to_string(prelog.import_week_value)
                if prelog.import_week_value else ''
            ),
            'deal_number': prelog.network_deal_number or '',
            'advertiser_product': prelog.advertiserproduct or '',
            'agency': prelog.agency or '',
            'title': prelog.title or '',
            'import_job_name': prelog.import_job.name if prelog.import_job else '',
            'match_detail': prelog.import_match_detail or '',
            'info': prelog.info or '',
            # Kept for the existing Review drawer while the list column is Info.
            'reason': prelog.info or '',
            'status': status,
            'status_label': {
                'matched': _('Matched'),
                'removed': _('Removed'),
                'overrun': _('Overrun'),
            }.get(status, _('Unmatched')),
            'removed': bool(prelog.removed),
            'is_overrun': bool(prelog.is_overrun),
            'attached': attached,
            'suggested': suggested,
            'alternatives': candidates[1:],
            'suggestion_attachable': bool(prelog.suggested_schedule)
            and prelog.suggested_schedule.status == 'sold',
            'match_quality': '' if not suggested else ('exact' if not hard else 'fuzzy'),
            'match_quality_label': '' if not suggested else (
                _('Exact') if not hard else _('Fuzzy')
            ),
            'explanation': prelog.info or '',
            'ambiguous_count': 2 if 'ambiguous' in flags else 1,
            'day_mismatch': 'day' in flags,
            'time_mismatch': 'time' in flags or 'time_buffer' in flags,
            'rate_mismatch': 'rate' in flags,
            'length_mismatch': 'length' in flags,
            'network_mismatch': 'network' in flags,
            'deal_mismatch': 'no_schedules' in flags or 'missing_deal' in flags,
            'time_distance': (suggested or {}).get('time_distance'),
            'exact_time_match': 'time' not in flags and 'time_buffer' not in flags,
        }

    @api.model
    def _fuzzy_overrun_map(self, schedule_ids, selected_version=False):
        """Live overrun figures for a batch of schedules.

        Returns {schedule_id: {'attached': n, 'cap': c, 'overrun': o}}
        where overrun = max(0, attached - cap).

        Why this exists
        ---------------
        * CORRECTNESS: the stored `mv.schedules.overrun_amount` is only
          refreshed when a mutation happens to pass through one of our
          hooks, and it is always scoped to the LATEST prelog version.
          The workbench may be viewing an older version, so reading the
          stored field shows a stale / wrong-version number. This
          computes the figure for the version actually in view.
        * PERFORMANCE: the previous code issued one search_count per
          schedule inside a loop. This is a single grouped query for
          the whole batch.
        """
        ids = [int(i) for i in (schedule_ids or []) if i]
        if not ids:
            return {}
        schedules = self.env['mv.schedules'].browse(ids).exists()
        caps = {
            sched.id: int(sched.units_available or 0)
            for sched in schedules
        }
        if not caps:
            return {}

        # One grouped count. Raw SQL keeps this a single round trip and
        # sidesteps read_group API drift across Odoo versions.
        params = [tuple(caps.keys())]
        version_clause = ''
        if selected_version:
            version_clause = 'AND version = %s'
            params.append(int(selected_version))
        self.env.cr.execute(
            """
            SELECT schedule, COUNT(*)
              FROM mv_prelog_data
             WHERE schedule IN %%s
               AND COALESCE(removed, FALSE) = FALSE
               %s
             GROUP BY schedule
            """ % version_clause,
            params,
        )
        counts = {row[0]: int(row[1]) for row in self.env.cr.fetchall()}

        result = {}
        for sched_id, cap in caps.items():
            attached = counts.get(sched_id, 0)
            result[sched_id] = {
                'attached': attached,
                'cap': cap,
                'overrun': max(0, attached - cap),
            }
        return result

    @api.model
    def _fuzzy_apply_overrun_badges(self, all_rows, selected_version=False):
        """Stamp live overrun figures onto every row's schedule chip and
        promote over-capacity rows to status 'overrun'.

        Runs once per request over the whole row set. Replaces reading
        the stale stored `overrun_amount` and the per-schedule
        search_count loop.
        """
        if not all_rows:
            return

        # Collect every schedule referenced by the result set.
        sched_ids = set()
        for row in all_rows:
            for key in ('attached', 'suggested'):
                chip = row.get(key)
                if chip and chip.get('id'):
                    sched_ids.add(chip['id'])
        if not sched_ids:
            return

        overrun_map = self._fuzzy_overrun_map(sched_ids, selected_version)

        # --- attached rows: badge + status straight from the live map.
        for row in all_rows:
            if row.get('status') == 'removed':
                continue
            chip = row.get('attached')

            if not chip or not chip.get('id'):
                continue
            info = overrun_map.get(chip['id'])
            if not info:
                continue
            chip = dict(chip)
            chip['overrun_amount'] = info['overrun']
            chip['units_available'] = info['cap']
            chip['attached_count'] = info['attached']
            row['attached'] = chip
            if info['overrun'] > 0:
                row['status'] = 'overrun'
                row['status_label'] = _('Overrun')
                row['is_overrun'] = True
                row['reason'] = _(
                    'Schedule %(name)s has %(cap)s unit(s) but '
                    '%(n)s prelog(s) are attached - over by %(o)s.'
                ) % {
                    'name': chip.get('name') or '',
                    'cap': info['cap'],
                    'n': info['attached'],
                    'o': info['overrun'],
                }
            else:
                # Back within capacity - make sure a previously stored
                # is_overrun flag doesn't keep the row mislabelled.
                if row.get('status') == 'overrun':
                    row['status'] = 'matched'
                    row['status_label'] = _('Matched')
                row['is_overrun'] = False

        # --- suggestion rows: projected overrun if all were attached.
        self._fuzzy_flag_suggested_overruns(
            all_rows, overrun_map, selected_version,
        )

    @api.model
    def _fuzzy_flag_suggested_overruns(self, all_rows, overrun_map, selected_version=False):
        """Mark suggested rows as 'overrun' when the target schedule
        would exceed capacity if every suggestion attached.

        Rules
        -----
        - Group rows by their suggested schedule id (only rows still
          in status 'suggestion' contribute).
        - Available capacity = schedule.units_available minus rows
          already attached to that same schedule AT THE SAME VERSION
          being viewed (spots carried over from older uploads don't
          consume a seat in the current version's count).
        - If len(suggestions_for_schedule) > available_capacity, EVERY
          suggestion targeting it is flagged 'overrun'.

        Also updates row['suggested'] overrun_amount so the Schedule
        column can render 'A-5170 +N' even for pending suggestions.

        Version scoping: prelog files are uploaded daily and each new
        version re-includes prior days' spots, so overrun is judged
        against a single version. `selected_version` is the version the
        workbench is currently filtered to; when set the attached counts
        in `overrun_map` were already computed at that version.

        `overrun_map` comes from _fuzzy_overrun_map - one grouped query
        for the whole batch, rather than a search_count per schedule.
        """
        # Bucket suggested rows by schedule id, preserving row order.
        suggested_bucket = {}
        for row in all_rows:
            if row.get('status') != 'suggestion':
                continue
            sug = row.get('suggested')
            if not sug or not sug.get('id'):
                continue
            suggested_bucket.setdefault(sug['id'], []).append(row)

        if not suggested_bucket:
            return

        for sched_id, bucket in suggested_bucket.items():
            info = overrun_map.get(sched_id)
            if not info:
                continue
            cap = info['cap']
            already_attached = info['attached']
            available = cap - already_attached
            excess = len(bucket) - max(available, 0)
            if excess <= 0:
                continue
            # Schedule is over-subscribed: mark EVERY suggestion
            # targeting it as Overrun so the whole group surfaces
            # together and the planner reviews them as one batch,
            # rather than singling out only the "last N" excess rows.
            sched_name = (bucket[0].get('suggested') or {}).get('name') or ''
            reason_text = _(
                'Schedule %(name)s has %(cap)s unit(s); '
                '%(pending)s prelog(s) target it - would overrun by '
                '%(excess)s.'
            ) % {
                'name': sched_name,
                'cap': cap,
                'pending': len(bucket) + already_attached,
                'excess': excess,
            }
            for row in bucket:
                row['status'] = 'overrun'
                row['status_label'] = _('Overrun')
                row['is_overrun'] = True
                sug = dict(row.get('suggested') or {})
                sug['overrun_amount'] = max(
                    int(sug.get('overrun_amount') or 0),
                    excess,
                )
                sug['units_available'] = cap
                sug['attached_count'] = already_attached
                row['suggested'] = sug
                row['reason'] = reason_text

    @api.model
    def _fuzzy_classify_row(self, row, prelog):
        # Overrun takes precedence over Matched: a prelog that
        # successfully attached to a schedule but whose attachment
        # exceeds the schedule capacity is Overrun, not Matched.
        if prelog.removed:
            status = 'removed'
        elif prelog.schedule and prelog.is_overrun:
            status = 'overrun'
        elif prelog.schedule:
            status = 'matched'
        elif row.get('suggested'):
            status = 'suggestion'
        else:
            status = 'no_suggestion'
        row.update({
            'status': status,
            'status_label': {
                'matched': _('Matched'),
                'overrun': _('Overrun'),
                'suggestion': _('Fuzzy Suggestion'),
                'no_suggestion': _('No Suggestion'),
                'removed': _('Removed'),
            }[status],
            'removed': bool(prelog.removed),
            'is_overrun': bool(prelog.is_overrun),
            'attached': (
                self._fuzzy_schedule_payload(prelog.schedule)
                if prelog.schedule else False
            ),
            'agency': prelog.agency or '',
            'title': prelog.title or '',
            'match_detail': prelog.import_match_detail or '',
            'import_job_name': prelog.import_job.name if prelog.import_job else '',
        })

    @api.model
    def _fuzzy_filter_workbench_rows(
        self,
        rows,
        status='all',
        search_term='',
        air_date=False,
        issue_filter='',
        sort_by='air_date',
        sort_direction='asc',
    ):
        status = status if status in {
            'all', 'matched', 'unmatched', 'suggestions', 'no_suggestion',
            'removed', 'overruns',
        } else 'all'
        status_map = {
            'matched': 'matched',
            'suggestions': 'suggestion',
            'no_suggestion': 'no_suggestion',
            'removed': 'removed',
            'overruns': 'overrun',
        }
        if status == 'all':
            result = [row for row in rows if row['status'] != 'removed']
        elif status == 'unmatched':
            result = [
                row for row in rows
                if row['status'] in ('suggestion', 'no_suggestion')
            ]
        else:
            result = [row for row in rows if row['status'] == status_map[status]]

        needle = normalize_match_text(search_term)
        if needle:
            result = [
                row for row in result
                if needle in normalize_match_text(' '.join([
                    str(row.get('name') or ''),
                    str(row.get('advertiser_product') or ''),
                    str(row.get('deal_number') or ''),
                    str((row.get('attached') or {}).get('name') or ''),
                    str((row.get('suggested') or {}).get('name') or ''),
                ]))
            ]
        if air_date:
            try:
                normalized_date = fields.Date.to_string(fields.Date.to_date(air_date))
            except (TypeError, ValueError):
                normalized_date = ''
            if normalized_date:
                result = [row for row in result if row['air_date'] == normalized_date]

        issue_checks = {
            'time': lambda row: row['time_mismatch'],
            'length': lambda row: row['length_mismatch'],
            'ambiguous': lambda row: row['ambiguous_count'] > 1,
            'missing_deal': lambda row: row['reason'] == _('Missing deal number'),
        }
        if issue_filter in issue_checks:
            result = [row for row in result if issue_checks[issue_filter](row)]

        def natural_text(value):
            return tuple(
                (0, int(part)) if part.isdigit() else (1, part)
                for part in re.split(r'(\d+)', normalize_match_text(value))
                if part
            )

        def time_value(row):
            parsed = self._fuzzy_parse_time(row.get('air_time'))
            return (
                (parsed.hour * 3600) + (parsed.minute * 60) + parsed.second
                if parsed else 0
            )

        def schedule_name(row):
            schedule = row.get('attached') or row.get('suggested') or {}
            return schedule.get('name') or ''

        def visible_reason(row):
            return row.get('reason') or (
                _('Schedule attached')
                if row.get('status') == 'matched'
                else _('Ready to attach')
            )

        sort_keys = {
            'status': lambda row: (
                natural_text(row.get('status_label')), row['air_date'], row['id']
            ),
            'name': lambda row: (
                natural_text(row.get('name')), row['air_date'], row['id']
            ),
            'network': lambda row: (
                natural_text(row.get('network')), row['air_date'], row['id']
            ),
            'air_date': lambda row: (
                row.get('air_date') or '', time_value(row), row['id']
            ),
            'length': lambda row: (
                self._fuzzy_parse_length(row.get('length')) or 0,
                row['air_date'], row['id'],
            ),
            'rate': lambda row: (
                float(row.get('rate') or 0), row['air_date'], row['id']
            ),
            'deal_number': lambda row: (
                natural_text(row.get('deal_number')), row['air_date'], row['id']
            ),
            'advertiser_product': lambda row: (
                natural_text(row.get('advertiser_product')), row['air_date'], row['id']
            ),
            'schedule': lambda row: (
                natural_text(schedule_name(row)), row['air_date'], row['id']
            ),
            'reason': lambda row: (
                natural_text(visible_reason(row)), row['air_date'], row['id']
            ),
        }
        sort_by = sort_by if sort_by in sort_keys else 'air_date'
        reverse = str(sort_direction or '').lower() == 'desc'
        missing_checks = {
            'status': lambda row: not row.get('status_label'),
            'name': lambda row: not row.get('name'),
            'network': lambda row: not row.get('network'),
            'air_date': lambda row: not row.get('air_date'),
            'length': lambda row: self._fuzzy_parse_length(row.get('length')) is None,
            'rate': lambda row: row.get('rate') in (None, ''),
            'deal_number': lambda row: not row.get('deal_number'),
            'advertiser_product': lambda row: not row.get('advertiser_product'),
            'schedule': lambda row: not schedule_name(row),
            'reason': lambda row: not visible_reason(row),
        }
        has_missing_value = missing_checks[sort_by]
        populated = [row for row in result if not has_missing_value(row)]
        missing = [row for row in result if has_missing_value(row)]
        return (
            sorted(populated, key=sort_keys[sort_by], reverse=reverse)
            + sorted(missing, key=lambda row: row['id'])
        )

    @api.model
    def _fuzzy_validate_selected_prelogs(
        self,
        prelog_ids,
        program_id=False,
        week_start=False,
        version=False,
        import_job_id=False,
    ):
        ids = []
        for value in prelog_ids or []:
            parsed_id = self._fuzzy_int(value)
            if parsed_id and parsed_id not in ids:
                ids.append(parsed_id)
        if not ids:
            raise UserError(_('Select at least one Prelog Data row.'))
        program, selected_week, selected_version = self._fuzzy_validate_optional_filters(
            program_id,
            week_start,
            version,
        )
        domain = self._fuzzy_prelog_domain(
            program.id if program else False,
            selected_week,
            selected_version,
            unmatched_only=False,
            include_removed=True,
            import_job_id=import_job_id,
        ) + [('id', 'in', ids)]
        prelogs = self.search(domain)
        if len(prelogs) != len(ids):
            raise UserError(
                _('One or more selected rows no longer belong to the active upload and filters.')
            )
        return prelogs

    @api.model
    def _fuzzy_resolve_workbench_selection(
        self,
        selection,
        program_id=False,
        week_start=False,
        version=False,
        status='all',
        search_term='',
        air_date=False,
        issue_filter='',
        sort_by='air_date',
        import_job_id=False,
        sort_direction='asc',
    ):
        if not isinstance(selection, dict):
            raise UserError(_('The selected Prelog rows have an invalid format.'))
        program, selected_week, selected_version = (
            self._fuzzy_validate_optional_filters(program_id, week_start, version)
        )
        filtered = self.search(
            self._prelog_stored_domain(
                self._fuzzy_prelog_domain(
                    program.id if program else False,
                    selected_week,
                    selected_version,
                    unmatched_only=False,
                    include_removed=status == 'removed',
                    import_job_id=import_job_id,
                ),
                status,
                issue_filter,
                air_date,
                search_term,
            ),
            order=self._prelog_stored_order(sort_by, sort_direction),
        )
        filtered_ids = filtered.ids
        filtered_id_set = set(filtered_ids)
        if selection.get('all_matching'):
            excluded_ids = {
                self._fuzzy_int(value)
                for value in selection.get('excluded_ids', [])
                if self._fuzzy_int(value)
            }
            selected_ids = [
                row_id for row_id in filtered_ids if row_id not in excluded_ids
            ]
        else:
            selected_ids = []
            for value in selection.get('ids', []):
                row_id = self._fuzzy_int(value)
                if row_id and row_id not in selected_ids:
                    selected_ids.append(row_id)
            if any(row_id not in filtered_id_set for row_id in selected_ids):
                raise UserError(
                    _('One or more selected rows no longer belong to the active view. Refresh and try again.')
                )
        if not selected_ids:
            raise UserError(_('Select at least one Prelog Data row.'))
        selected_prelogs = self.browse(selected_ids)
        selected_rows = [
            self._prelog_stored_row(prelog) for prelog in selected_prelogs
        ]
        return selected_prelogs, selected_rows

    @api.model
    def _fuzzy_build_rows(
        self,
        prelogs,
        program,
        selected_week,
        use_attached=False,
        analyze_attached=True,
    ):
        """Build a row dict per prelog.

        `analyze_attached=False` skips the per-schedule fuzzy analysis
        for rows that ALREADY have a schedule attached. Those rows get
        their status from plain DB columns, so the analysis only feeds
        cosmetic fields (explanation, mismatch flags) which the caller
        can fill in later for just the visible page. This is the main
        lever that makes a 100k-row view load: the expensive
        _fuzzy_analyze_schedule work is skipped for the bulk of rows.
        """
        # Group prelogs by (program, week) so candidate schedules are
        # fetched once per group. Plain lists - the previous version
        # used `recordset |= prelog` inside the loop, which is O(n^2)
        # and dominates runtime on large result sets.
        groups = {}
        contexts = {}
        row_keys = []
        for prelog in prelogs:
            row_program = prelog.import_program or program
            row_week = prelog.import_week_value or selected_week
            key = (row_program.id if row_program else False, row_week)
            groups.setdefault(key, []).append(prelog.id)
            contexts[key] = (row_program, row_week)
            row_keys.append(key)

        candidate_maps = {}
        accepted_networks = {}
        for key, grouped_ids in groups.items():
            row_program, row_week = contexts[key]
            candidate_maps[key] = self._fuzzy_candidate_map(
                self.browse(grouped_ids),
                row_program,
                row_week,
            )
            accepted_networks[key] = self._fuzzy_network_names(row_program)

        result = []
        for prelog, key in zip(prelogs, row_keys):
            row_program = contexts[key][0]
            attached = prelog.schedule if use_attached else False
            # Skip candidate analysis when the row is already attached
            # and the caller does not need the derived fields yet.
            skip_analysis = bool(attached) and not analyze_attached
            result.append(self._fuzzy_build_row(
                prelog,
                row_program,
                candidate_maps[key].get(
                    (prelog.network_deal_number or '').strip(),
                    [],
                ),
                accepted_networks[key],
                attached,
                analyze=not skip_analysis,
            ))
        return result

    @api.model
    def _fuzzy_candidate_map(self, prelogs, program, selected_week):
        if not program or not selected_week:
            return {}
        deal_numbers = sorted({
            (prelog.network_deal_number or '').strip()
            for prelog in prelogs
            if (prelog.network_deal_number or '').strip()
        })
        if not deal_numbers:
            return {}
        schedules = self.env['mv.schedules'].search([
            ('week', '=', selected_week),
            ('deal_parent.program', '=', program.id),
            ('deal_parent.network_deal_number', 'in', deal_numbers),
            ('status', '=', 'sold'),
        ], order='id')
        result = {}
        for schedule in schedules:
            deal_number = (
                schedule.deal_parent.network_deal_number or ''
            ).strip()
            result.setdefault(deal_number, []).append(schedule)
        return result

    @api.model
    def _fuzzy_build_row(
        self,
        prelog,
        program,
        schedules,
        accepted_networks=None,
        attached_schedule=False,
        analyze=True,
    ):
        day = prelog.airdate.strftime('%a') if prelog.airdate else ''
        reason = ''
        suggested = False
        suggested_analysis = False
        ambiguous_count = 0

        if attached_schedule and not analyze:
            # Fast path for already-attached rows: status comes from DB
            # columns, so the fuzzy analysis is not needed here. The
            # derived fields (explanation / mismatch flags) are filled
            # in later for just the visible page by
            # _fuzzy_enrich_visible_rows.
            suggested = attached_schedule
        elif attached_schedule:
            suggested_analysis = self._fuzzy_analyze_schedule(
                prelog,
                program,
                attached_schedule,
                accepted_networks,
            )
            suggested = attached_schedule
        elif not (prelog.network_deal_number or '').strip():
            reason = _('Missing deal number')
        elif not (prelog.scheduletime or '').strip():
            reason = _('Missing air time')
        else:
            analyses = [
                self._fuzzy_analyze_schedule(
                    prelog,
                    program,
                    schedule,
                    accepted_networks,
                )
                for schedule in schedules
            ]
            eligible = [
                analysis
                for analysis in analyses
                if (
                    analysis['network_match']
                    and analysis['day_match']
                )
            ]
            if eligible:
                eligible.sort(key=self._fuzzy_analysis_sort_key)
                suggested_analysis = eligible[0]
                suggested = suggested_analysis['schedule']
                winning_key = self._fuzzy_analysis_quality_key(
                    suggested_analysis
                )
                ambiguous_count = sum(
                    1
                    for analysis in eligible
                    if self._fuzzy_analysis_quality_key(analysis) == winning_key
                )
            else:
                reason = self._fuzzy_no_suggestion_reason(analyses, prelog)

        length_mismatch = False
        time_mismatch = False
        rate_mismatch = False
        deal_mismatch = False
        network_mismatch = False
        day_mismatch = False
        suggestion_attachable = False

        if suggested_analysis:
            time_mismatch = not suggested_analysis['time_match']
            length_mismatch = not suggested_analysis['length_match']
            rate_mismatch = not suggested_analysis['rate_match']
            deal_mismatch = not suggested_analysis['deal_match']
            network_mismatch = not suggested_analysis['network_match']
            day_mismatch = not suggested_analysis['day_match']
            suggestion_attachable = suggested.status == 'sold'

            if not suggested_analysis['network_match']:
                reason = _('Network mismatch')
            elif not suggested_analysis['week_match']:
                reason = _('Week mismatch')
            elif not suggested_analysis['deal_match']:
                reason = _('Deal number mismatch')
            elif not suggested_analysis['rate_match']:
                reason = _('Rate mismatch')
            elif not suggested_analysis['day_match']:
                reason = _('Day mismatch')
            elif suggested.status == 'canceled':
                reason = _('Canceled')
            elif suggested.status and suggested.status != 'sold':
                reason = (
                    self._fuzzy_selection_label(suggested, 'status')
                    or suggested.status
                )
            elif time_mismatch:
                reason = _('Out of Rotation')
            elif length_mismatch:
                reason = _(
                    'Length mismatch: prelog=%(prelog)s, schedule=%(schedule)s'
                ) % {
                    'prelog': (
                        self._fuzzy_parse_length(prelog.schedulelength)
                        or ''
                    ),
                    'schedule': (
                        suggested_analysis['schedule_length']
                        or ''
                    ),
                }
            elif ambiguous_count > 1:
                reason = _(
                    '%(count)s equally ranked schedules; review before attaching'
                ) % {'count': ambiguous_count}
            elif not suggested_analysis['exact_time_match']:
                reason = _(
                    'Within fuzzy buffer (%(minutes)s minute(s) from rotation)'
                ) % {'minutes': suggested_analysis['time_distance'] or 0}

        exact_match = bool(
            suggested_analysis
            and suggested.status == 'sold'
            and ambiguous_count <= 1
            and suggested_analysis['network_match']
            and suggested_analysis['deal_match']
            and suggested_analysis['week_match']
            and suggested_analysis['rate_match']
            and suggested_analysis['day_match']
            and suggested_analysis['exact_time_match']
            and suggested_analysis['length_match']
        )

        return {
            'id': prelog.id,
            'name': prelog.display_name or '',
            'network': (
                prelog.broadcast_network
                or prelog.network
                or (program.display_name if program else '')
                or ''
            ),
            'version': prelog.version or '',
            'air_date': (
                fields.Date.to_string(prelog.airdate)
                if prelog.airdate
                else ''
            ),
            'day': day,
            'air_time': prelog.scheduletime or '',
            'length': prelog.schedulelength or '',
            'rate': prelog.rate or 0.0,
            'week': (
                fields.Date.to_string(prelog.import_week_value)
                if prelog.import_week_value
                else ''
            ),
            'deal_number': prelog.network_deal_number or '',
            'advertiser_product': prelog.advertiserproduct or '',
            'reason': reason or '',
            'suggested': (
                self._fuzzy_schedule_payload(suggested)
                if suggested
                else False
            ),
            'suggestion_attachable': suggestion_attachable,
            'match_quality': 'exact' if exact_match else ('fuzzy' if suggested else ''),
            'match_quality_label': _('Exact') if exact_match else (_('Fuzzy') if suggested else ''),
            'explanation': self._fuzzy_match_explanation(
                suggested_analysis,
                reason,
                ambiguous_count,
            ),
            'ambiguous_count': ambiguous_count,
            'time_mismatch': time_mismatch,
            'length_mismatch': length_mismatch,
            'rate_mismatch': rate_mismatch,
            'deal_mismatch': deal_mismatch,
            'network_mismatch': network_mismatch,
            'day_mismatch': day_mismatch,
        }

    @api.model
    def _fuzzy_analyze_schedule(
        self,
        prelog,
        program,
        schedule,
        accepted_networks=None,
    ):
        schedule_length = self._fuzzy_schedule_length(schedule)
        prelog_length = self._fuzzy_parse_length(prelog.schedulelength)
        time_match, time_distance = self._fuzzy_time_window_analysis(
            prelog.scheduletime,
            self._fuzzy_selection_label(schedule, 'start_time'),
            self._fuzzy_selection_label(schedule, 'end_time'),
            self._FUZZY_TIME_BUFFER_MINUTES,
        )
        exact_time_match, unused_exact_distance = self._fuzzy_time_window_analysis(
            prelog.scheduletime,
            self._fuzzy_selection_label(schedule, 'start_time'),
            self._fuzzy_selection_label(schedule, 'end_time'),
            0,
        )
        length_match = prelog_length == schedule_length
        return {
            'schedule': schedule,
            'network_match': self._fuzzy_network_matches(
                prelog,
                program,
                schedule,
                accepted_networks,
            ),
            'deal_match': bool(
                (
                    schedule.deal_parent.network_deal_number
                    or ''
                ).strip()
                == (prelog.network_deal_number or '').strip()
            ),
            'week_match': schedule.week == prelog.import_week_value,
            'rate_match': self._fuzzy_rate_matches(prelog, schedule),
            'day_match': self._fuzzy_day_matches(prelog, schedule),
            'time_match': time_match,
            'exact_time_match': exact_time_match,
            'time_distance': time_distance,
            'length_match': length_match,
            'schedule_length': schedule_length,
        }

    @api.model
    def _fuzzy_match_explanation(self, analysis, reason, ambiguous_count):
        if not analysis:
            return reason or _('No eligible sold schedule was found.')
        parts = []
        if analysis['network_match']:
            parts.append(_('network matches'))
        if analysis['deal_match']:
            parts.append(_('deal matches'))
        if analysis['rate_match']:
            parts.append(_('rate matches'))
        if analysis['day_match']:
            parts.append(_('air day matches'))
        if analysis['exact_time_match']:
            parts.append(_('airtime is inside rotation'))
        elif analysis['time_match']:
            parts.append(
                _('airtime is %(minutes)s minute(s) from rotation')
                % {'minutes': analysis['time_distance'] or 0}
            )
        if analysis['length_match']:
            parts.append(_('length matches'))
        if ambiguous_count > 1:
            parts.append(_('%(count)s schedules are tied') % {'count': ambiguous_count})
        return '; '.join(parts) + (('. ' + reason) if reason else '')

    @api.model
    def _fuzzy_no_suggestion_reason(self, analyses, prelog=None):
        if not analyses:
            return _('No schedules found for deal number')
        network_matches = [
            analysis
            for analysis in analyses
            if analysis['network_match']
        ]
        if not network_matches:
            return _('No network match')
        day_matches = [
            analysis
            for analysis in network_matches
            if analysis['day_match']
        ]
        if not day_matches:
            return _('No day match')
        return _('No time match')

    @api.model
    def _fuzzy_analysis_sort_key(self, analysis):
        schedule = analysis['schedule']
        return (
            self._fuzzy_status_priority(schedule.status),
            # Rotation is the strongest fuzzy signal after Program/deal/day.
            # In particular, an incorrect schedule rate must not make a
            # nearby but out-of-rotation schedule outrank the correct window.
            0 if analysis['exact_time_match'] else 1,
            (
                analysis['time_distance']
                if analysis['time_distance'] is not None
                else 10 ** 9
            ),
            0 if analysis['rate_match'] else 1,
            0 if analysis['length_match'] else 1,
            schedule.display_name or '',
            schedule.id,
        )

    @api.model
    def _fuzzy_analysis_quality_key(self, analysis):
        schedule = analysis['schedule']
        return (
            self._fuzzy_status_priority(schedule.status),
            0 if analysis['exact_time_match'] else 1,
            analysis['time_distance'],
            0 if analysis['rate_match'] else 1,
            0 if analysis['length_match'] else 1,
        )

    @api.model
    def _fuzzy_schedule_payload(self, schedule):
        if not schedule:
            return False
        days = sorted(
            [
                day.name
                for day in schedule.days_allowed
                if day.name
            ],
            key=lambda value: self._FUZZY_DAY_ORDER.get(
                value[:3].strip().lower(),
                99,
            ),
        )
        start_time = self._fuzzy_selection_label(
            schedule,
            'start_time',
        ) or ''
        end_time = self._fuzzy_selection_label(
            schedule,
            'end_time',
        ) or ''
        return {
            'id': schedule.id,
            'name': schedule.display_name or '',
            'time_range': (
                '%s-%s' % (start_time, end_time)
                if start_time and end_time
                else ''
            ),
            'days_allowed': ', '.join(days),
            'rate': schedule.rate or 0.0,
            'length': self._fuzzy_schedule_length(schedule) or '',
            'deal_number': (
                schedule.deal_parent.network_deal_number
                or ''
            ),
            'network': (
                self._fuzzy_selection_label(schedule, 'networks')
                or ''
            ),
            'status': schedule.status or '',
            'status_label': (
                self._fuzzy_selection_label(schedule, 'status')
                or ''
            ),
            # Overrun figures are stamped later by
            # _fuzzy_apply_overrun_badges, which computes them live for
            # the version in view. The stored overrun_amount is NOT
            # read here: it is only refreshed on mutation and is always
            # scoped to the latest version, so it goes stale.
            'overrun_amount': 0,
            'units_available': int(schedule.units_available or 0),
            'attached_count': 0,
        }

    # ------------------------------------------------------------------
    # Matching helpers
    # ------------------------------------------------------------------

    @api.model
    def _fuzzy_network_names(self, program):
        if not program:
            return False
        config = load_program_config(program.display_name)
        config_names = config.get('networkNames', [])
        field_map = config.get('fieldMap', {})
        if not config_names and not field_map.get('network'):
            return False
        names = set()
        for value in config_names:
            normalized = normalize_match_text(value)
            if normalized:
                names.add(normalized)
        program_name = normalize_match_text(program.display_name)
        if program_name:
            names.add(program_name)
        return names

    @api.model
    def _fuzzy_network_matches(
        self,
        prelog,
        program,
        schedule,
        accepted_networks=None,
    ):
        if (
            not program
            or not schedule.deal_parent
            or schedule.deal_parent.program != program
        ):
            return False
        raw_network = normalize_match_text(
            prelog.broadcast_network or prelog.network
        )
        if not raw_network:
            return True
        accepted_networks = (
            accepted_networks
            if accepted_networks is not None
            else self._fuzzy_network_names(program)
        )
        if accepted_networks is False:
            return True
        return raw_network in accepted_networks

    @api.model
    def _fuzzy_resolve_schedule(self, selection):
        Schedule = self.env['mv.schedules']
        schedule = False
        if selection['schedule_id']:
            schedule = Schedule.search(
                [('id', '=', selection['schedule_id'])],
                limit=1,
            )
        reference = selection['schedule_ref']
        if not schedule and reference:
            if reference.isdigit():
                schedule = Schedule.search(
                    [('id', '=', int(reference))],
                    limit=1,
                )
            if not schedule:
                matches = Schedule.search(
                    [('name', '=', reference)],
                    limit=2,
                )
                if len(matches) > 1:
                    return False, _(
                        'Schedule name "%(name)s" is ambiguous; '
                        'enter its numeric Odoo ID.'
                    ) % {'name': reference}
                schedule = matches[:1]
        if not schedule:
            label = reference or selection['schedule_id'] or ''
            return False, (
                _('Schedule not found: %(schedule)s')
                % {'schedule': label}
            )
        return schedule, False

    @api.model
    def _fuzzy_rate_matches(self, prelog, schedule):
        rounding = (
            prelog.currency_id.rounding
            or schedule.currency_id.rounding
            or 0.01
        )
        return float_compare(
            prelog.rate or 0.0,
            schedule.rate or 0.0,
            precision_rounding=rounding,
        ) == 0

    @api.model
    def _fuzzy_day_matches(self, prelog, schedule):
        if not prelog.airdate or not schedule.days_allowed:
            return False
        prelog_day = prelog.airdate.strftime('%a').lower()
        allowed = {
            (day.name or '')[:3].lower()
            for day in schedule.days_allowed
            if day.name
        }
        return prelog_day in allowed

    @api.model
    def _fuzzy_schedule_length(self, schedule):
        if schedule.unitlength not in (None, False, ''):
            try:
                return int(schedule.unitlength)
            except (TypeError, ValueError):
                pass
        if schedule.deal_parent and schedule.deal_parent.length:
            return self._fuzzy_parse_length(
                self._fuzzy_selection_label(
                    schedule.deal_parent,
                    'length',
                )
            )
        return None

    @api.model
    def _fuzzy_time_window_analysis(
        self,
        air_time,
        start_time,
        end_time,
        buffer_minutes=None,
    ):
        """Return ``(inside buffered rotation, minutes from rotation)``.

        The comparison is circular across midnight and uses real minute
        arithmetic instead of Salesforce's HHMM integer approximation.
        """
        buffer_minutes = (
            self._FUZZY_TIME_BUFFER_MINUTES
            if buffer_minutes is None
            else max(self._fuzzy_int(buffer_minutes, default=0), 0)
        )
        air_value = self._fuzzy_parse_time(air_time)
        start_value = self._fuzzy_parse_time(start_time)
        end_value = self._fuzzy_parse_time(end_time)
        if not air_value or not start_value or not end_value:
            return False, None

        air_minutes = (air_value.hour * 60) + air_value.minute
        start_minutes = (start_value.hour * 60) + start_value.minute
        end_minutes = (end_value.hour * 60) + end_value.minute
        if end_minutes <= start_minutes:
            end_minutes += 24 * 60

        distances = []
        for candidate in (
            air_minutes - (24 * 60),
            air_minutes,
            air_minutes + (24 * 60),
        ):
            if start_minutes <= candidate <= end_minutes:
                distance = 0
            else:
                distance = min(
                    abs(candidate - start_minutes),
                    abs(candidate - end_minutes),
                )
            distances.append(distance)
        distance = min(distances)
        return distance <= buffer_minutes, distance

    @staticmethod
    def _fuzzy_parse_time(value):
        if value in (None, False, ''):
            return None
        text = str(value).strip().upper().replace(' ', '')
        if text.endswith('A') and not text.endswith('AM'):
            text += 'M'
        elif text.endswith('P') and not text.endswith('PM'):
            text += 'M'
        for time_format in (
            '%H:%M:%S',
            '%H:%M',
            '%I:%M:%S%p',
            '%I:%M%p',
        ):
            try:
                return datetime.strptime(text, time_format).time()
            except ValueError:
                continue
        return None

    @staticmethod
    def _fuzzy_parse_length(value):
        if value in (None, False, ''):
            return None
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _fuzzy_selection_label(record, field_name):
        if not record or not record[field_name]:
            return False
        selection = record._fields[field_name].selection
        if callable(selection):
            selection = selection(record.env)
        return dict(selection).get(record[field_name], record[field_name])

    @staticmethod
    def _fuzzy_status_priority(status):
        return {
            'sold': 0,
            'sold_unflighted': 1,
            False: 2,
            'canceled': 3,
        }.get(status, 2)

    @staticmethod
    def _fuzzy_csv_row(values):
        safe_values = []
        for value in values:
            if (
                isinstance(value, str)
                and value.lstrip().startswith(('=', '+', '-', '@'))
            ):
                value = "'%s" % value
            safe_values.append(value)
        return safe_values

    @staticmethod
    def _fuzzy_int(value, default=False):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
