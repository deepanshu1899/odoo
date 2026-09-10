# -*- coding: utf-8 -*-
{
    "name": "Prelog Generator",
    "summary": "Prepare contact-specific Short Form prelog workbooks and emails.",
    "version": "19.0.1.6.0",
    "license": "LGPL-3",
    "author": "Marathon Ventures",
    "category": "Operations",
    "depends": ["marathon_ventures", "mail"],
    "external_dependencies": {"python": ["openpyxl"]},
    "data": [
        "security/ir.model.access.csv",
        "data/prelog_conductor_sequence.xml",
        "data/prelog_conductor_cron.xml",
        "views/prelog_conductor_wizard_views.xml",
        "views/prelog_conductor_batch_views.xml",
        "views/prelog_conductor_resend_views.xml",
        "views/prelog_conductor_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "marathon_short_form_prelogs/static/src/js/prelog_generator_ui.js",
            "marathon_short_form_prelogs/static/src/xml/prelog_generator_ui.xml",
            "marathon_short_form_prelogs/static/src/scss/prelog_generator.scss",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
