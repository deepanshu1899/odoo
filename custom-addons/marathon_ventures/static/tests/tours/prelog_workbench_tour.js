/** @odoo-module **/

import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("prelog_workbench_stored_data_tour", {
    steps: () => [
        {
            content: "the Prelog Workbench mounted",
            trigger: ".mv-fuzzy__page-title:contains('Prelog Workbench')",
        },
        {
            content: "pick the test Program",
            trigger: ".mv-fuzzy__filter-grid select",
            run() {
                const select = document.querySelector(".mv-fuzzy__filter-grid select");
                const option = [...select.options].find(
                    (item) => item.textContent.trim() === "Prelog UI Test Network",
                );
                if (!option) throw new Error("test Program is missing");
                select.value = option.value;
                select.dispatchEvent(new Event("change", { bubbles: true }));
            },
        },
        {
            content: "set the broadcast week",
            trigger: ".mv-fuzzy__filter-grid input[type=date]",
            run() {
                const input = document.querySelector(
                    ".mv-fuzzy__filter-grid input[type=date]",
                );
                input.value = "2026-07-27";
                input.dispatchEvent(new Event("change", { bubbles: true }));
            },
        },
        {
            content: "pick the stored version",
            trigger: ".mv-fuzzy__filter-grid label:nth-child(3) select:has(option:contains('Version 77'))",
            run() {
                const selects = document.querySelectorAll(
                    ".mv-fuzzy__filter-grid select",
                );
                selects[1].value = "77";
                selects[1].dispatchEvent(new Event("change", { bubbles: true }));
            },
        },
        {
            content: "load stored Prelog rows",
            trigger: ".mv-fuzzy__filter-actions .btn-primary",
            run: "click",
        },
        {
            content: "the fixture rendered",
            trigger: ".mv-fuzzy__table:contains('Prelog UI Fixture Product')",
        },
        {
            content: "the status comes from the record",
            trigger: ".mv-fuzzy__status--suggestion:contains('Unmatched')",
        },
        {
            content: "Prelog is the third column",
            trigger: ".mv-fuzzy__table thead th:nth-child(3):contains('Prelog')",
        },
        {
            content: "Schedule sits directly beside Prelog",
            trigger: ".mv-fuzzy__table thead th:nth-child(4):contains('Schedule')",
        },
        {
            content: "an unmatched suggestion is not shown as attached",
            trigger: ".mv-fuzzy__table tbody tr",
            run() {
                const scheduleCell = document.querySelector(
                    ".mv-fuzzy__table tbody tr td:nth-child(4)",
                );
                if (scheduleCell.textContent.trim()) {
                    throw new Error("the Schedule column displayed a suggestion");
                }
            },
        },
        {
            content: "Info displays the stored value",
            trigger: ".mv-fuzzy__table .mv-fuzzy__reason:contains('2 suggestion(s)')",
        },
        {
            content: "the renamed Suggestions tab is available",
            trigger: ".mv-fuzzy__tabs button:contains('Suggestions')",
        },
        {
            content: "Import is available from the Workbench",
            trigger: ".mv-fuzzy__filter-actions button:contains('Import')",
        },
        {
            content: "Refresh sits with the table actions",
            trigger: ".mv-fuzzy__bulk-actions button:contains('Refresh')",
        },
        {
            content: "Info is sortable",
            trigger: ".mv-fuzzy__table thead button:contains('Info')",
            run: "click",
        },
        {
            content: "the Info sort reached the server and completed",
            trigger: ".mv-fuzzy__table thead th[aria-sort='ascending'] button:contains('Info')",
        },
        {
            content: "open the candidate review",
            trigger: ".mv-fuzzy__table tbody button:contains('Review')",
            run: "click",
        },
        {
            content: "the review displays ranked possible schedules",
            trigger: ".mv-fuzzy__drawer h3:contains('Possible Schedules')",
        },
        {
            content: "the suggestion and runner-up are both available",
            trigger: ".mv-prelog-candidates tbody tr:nth-child(2)",
            run() {
                const rows = document.querySelectorAll(
                    ".mv-prelog-candidates tbody tr",
                );
                if (rows.length !== 2) {
                    throw new Error(`expected 2 ranked candidates, got ${rows.length}`);
                }
            },
        },
        {
            content: "the runner-up's rate mismatch is prominent",
            trigger: ".mv-prelog-candidates tbody tr:nth-child(2) .mv-prelog-candidates__rate-warning",
        },
        {
            content: "each eligible candidate has its own Attach action",
            trigger: ".mv-prelog-candidates tbody tr:nth-child(2) button:contains('Attach')",
        },
        {
            content: "close the candidate review",
            trigger: ".mv-fuzzy__drawer > header button",
            run: "click",
        },
        {
            content: "Refresh re-runs matching outside ordinary page loads",
            trigger: ".mv-fuzzy__bulk-actions button:contains('Refresh')",
            run: "click",
        },
        {
            content: "Refresh attached the exact stored suggestion",
            trigger: ".mv-fuzzy__table tbody tr td:nth-child(4) a",
        },
        {
            content: "and the row now reports its stored matched status",
            trigger: ".mv-fuzzy__status--matched:contains('Matched')",
        },
    ],
});
