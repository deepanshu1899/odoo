# -*- coding: utf-8 -*-
"""Regression coverage for the record-type-specific Deal form."""

from lxml import etree

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'deal_short_form_layout')
class TestDealShortFormLayout(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.form_view = cls.env.ref('marathon_ventures.view_mv_deal_form')

    def _compiled_form_arch(self):
        result = self.env['mv.deal'].get_view(
            view_id=self.form_view.id,
            view_type='form',
        )
        return etree.fromstring(result['arch'])

    def test_short_form_pages_have_requested_fields(self):
        arch = self._compiled_form_arch()
        details = arch.xpath("//page[@name='page_details']")
        additional = arch.xpath("//page[@name='page_additional_details']")

        self.assertEqual(len(details), 1)
        self.assertEqual(len(additional), 1)
        self.assertEqual(
            details[0].get('invisible'),
            "sf_record_type != 'Short_Form'",
        )
        self.assertEqual(
            additional[0].get('invisible'),
            "sf_record_type != 'Short_Form'",
        )

        self.assertEqual(
            [field.get('name') for field in details[0].xpath('.//field')],
            [
                'program', 'advertiser', 'brands', 'commercial_type',
                'campaign', 'contact', 'length', 'min_sep',
                'network_deal_number', 'product_code', 'access_code',
                'agency_deal_number', 'client_code', 'estimate', 'pi',
                'deal_owner_id',
            ],
        )
        self.assertEqual(
            [field.get('name') for field in additional[0].xpath('.//field')],
            [
                'sales_plan', 'status', 'x_class', 'log_exp_date',
                'hiatus_dates', 'ratings_year', 'ratings_quarter',
                'e_i_friendly', 'total_booked_units',
                'total_booked_dollars', 'currency_id',
            ],
        )

    def test_retired_inline_additional_details_ui_is_absent(self):
        arch = self._compiled_form_arch()

        self.assertFalse(arch.xpath("//*[@name='mv_additional_bar']"))
        self.assertFalse(
            arch.xpath("//button[@name='action_toggle_additional_details']")
        )
        self.assertFalse(
            arch.xpath("//field[@name='show_additional_details']")
        )

    def test_new_deal_defaults_to_short_form_and_current_owner(self):
        defaults = self.env['mv.deal'].default_get([
            'sf_record_type',
            'deal_owner_id',
        ])

        self.assertEqual(defaults['sf_record_type'], 'Short_Form')
        self.assertEqual(defaults['deal_owner_id'], self.env.user.id)
