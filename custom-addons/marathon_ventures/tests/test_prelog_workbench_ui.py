# -*- coding: utf-8 -*-
"""Browser coverage for the stored-data Prelog Workbench."""

from datetime import date

from odoo import Command
from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestPrelogWorkbenchUI(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.week = date(2026, 7, 27)
        cls.program = cls.env['mv.programs'].create({
            'name': 'Prelog UI Test Network',
            'clientcode': 'PUT',
        })
        monday = cls.env['mv.days_allowed.tag'].search(
            [('name', '=ilike', 'Mon%')], limit=1,
        )
        if not monday:
            monday = cls.env['mv.days_allowed.tag'].create({'name': 'Mon'})
        deal = cls.env['mv.deal'].create({
            'program': cls.program.id,
            'network_deal_number': 'PUT-1',
            'length': 'v_30',
        })
        cls.schedule = cls.env['mv.schedules'].create({
            'deal_parent': deal.id,
            'week': cls.week,
            'start_time': 'v_09_00a',
            'end_time': 'v_10_00a',
            'days_allowed': [Command.set(monday.ids)],
            'rate': 100.0,
            'units_available': 10,
            'status': 'sold',
        })
        cls.alternative_schedule = cls.env['mv.schedules'].create({
            'deal_parent': deal.id,
            'week': cls.week,
            'start_time': 'v_08_00a',
            'end_time': 'v_09_00a',
            'days_allowed': [Command.set(monday.ids)],
            'rate': 0.0,
            'units_available': 10,
            'status': 'sold',
        })
        cls.prelog = cls.env['mv.prelog_data'].create({
            'import_program': cls.program.id,
            'import_week_value': cls.week,
            'version': 77,
            'network': cls.program.display_name,
            'broadcast_network': cls.program.display_name,
            'network_deal_number': 'PUT-1',
            'airdate': cls.week,
            'scheduletime': '09:30:00 AM',
            'schedulelength': '30',
            'rate': 100.0,
            'advertiserproduct': 'Prelog UI Fixture Product',
            'import_match_status': 'unmatched',
        })
        # The page must render this stored result without running the matcher.
        cls.env['mv.prelog_data']._prelog_store_matching(
            cls.prelog, cls.program, cls.week, attach=False,
        )

    def test_workbench_tour(self):
        action = self.env.ref(
            'marathon_ventures.action_mv_prelog_fuzzy_match_page',
        )
        self.start_tour(
            '/odoo/action-%s' % action.id,
            'prelog_workbench_stored_data_tour',
            login='admin',
        )
