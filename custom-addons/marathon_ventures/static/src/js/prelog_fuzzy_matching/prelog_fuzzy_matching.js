/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";

export class MvPrelogFuzzyMatching extends Component {
    static template = "marathon_ventures.MvPrelogFuzzyMatching";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        onWillUnmount(() => { this.isUnmounted = true; });
        this.requestId = 0;
        this.isUnmounted = false;
        this.state = useState({
            loaded: false,
            querying: false,
            mutating: false,
            exporting: false,
            hasFiltered: false,
            programs: [],
            versions: [],
            latestUpload: false,
            pageSize: 200,
            timeBufferMinutes: 120,
            filters: { programId: false, weekStart: "", version: false, importJobId: false },
            activeTab: "all",
            counts: { all: 0, matched: 0, unmatched: 0, suggestions: 0, no_suggestion: 0, removed: 0, overruns: 0 },
            // Dollar totals per tab, plus the total for the current
            // view (tab + search + filters). Both come from the server
            // over the FULL scope - never sum state.rows, that is only
            // the 200-row page.
            dollars: { all: 0, matched: 0, unmatched: 0, suggestions: 0, no_suggestion: 0, removed: 0, overruns: 0 },
            filteredDollars: 0,
            // False until a search returns totals, so the badge can
            // show "—" rather than a misleading $0.00.
            dollarsAvailable: false,
            searchTerm: "",
            airDate: "",
            issueFilter: "",
            refreshing: false,
            importJob: false,
            sortBy: "air_date",
            sortDirection: "asc",
            rows: [],
            total: 0,
            offset: 0,
            page: 0,
            pages: 0,
            selectedRows: {},
            selectAllMatching: false,
            excludedRows: {},
            drawerRow: false,
            manualSchedule: "",
            drawerOverrun: null,
        });

        onWillStart(async () => {
            const options = await this.orm.call("mv.prelog_data", "fuzzy_match_get_options", []);
            this._setOptions(options, true);
            const latest = this.state.latestUpload;
            if (latest) {
                this.state.filters.programId = latest.program_id;
                this.state.filters.weekStart = latest.week_start;
                this.state.filters.version = latest.version;
                this.state.filters.importJobId = latest.id;
                await this._refreshVersions(latest.version);
                await this._loadResults();
            }
            this.state.loaded = true;
        });
    }

    _setOptions(options, includeLatest = false) {
        options = options || {};
        this.state.programs = options.programs || this.state.programs;
        this.state.versions = options.versions || [];
        this.state.pageSize = options.page_size || 200;
        this.state.timeBufferMinutes = options.time_buffer_minutes || 120;
        if (includeLatest) {
            this.state.latestUpload = options.latest_upload || false;
        }
    }

    async _refreshVersions(keepVersion = false) {
        const { programId, weekStart } = this.state.filters;
        this.state.versions = [];
        const options = await this.orm.call(
            "mv.prelog_data", "fuzzy_match_get_options", [programId || false, weekStart || false]
        );
        this._setOptions(options);
        if (keepVersion && !this.state.versions.includes(Number(keepVersion))) {
            this.state.versions.push(Number(keepVersion));
            this.state.versions.sort((a, b) => a - b);
        }
        this.state.filters.version = keepVersion ? Number(keepVersion) : false;
    }

    _clearResults() {
        this.requestId += 1;
        this.state.hasFiltered = false;
        this.state.rows = [];
        this.state.total = 0;
        this.state.offset = 0;
        this._resetSelection();
        this.state.drawerRow = false;
    }

    async onProgramChange(ev) {
        this._clearResults();
        this.state.filters.programId = Number(ev.target.value) || false;
        this.state.filters.importJobId = false;
        await this._refreshVersions();
    }

    async onWeekChange(ev) {
        this._clearResults();
        this.state.filters.weekStart = ev.target.value;
        this.state.filters.importJobId = false;
        await this._refreshVersions();
    }

    onVersionChange(ev) {
        this._clearResults();
        this.state.filters.version = Number(ev.target.value) || false;
        this.state.filters.importJobId = false;
    }

    _filtersAreValid() {
        const filters = this.state.filters;
        if (filters.weekStart) {
            const date = new Date(`${filters.weekStart}T12:00:00`);
            if (Number.isNaN(date.getTime()) || date.getDay() !== 1) {
                this.notification.add("Week must be the Monday that starts the broadcast week.", { type: "warning" });
                return false;
            }
        }
        return true;
    }

    async onFilter() {
        if (!this._filtersAreValid()) return;
        this.state.offset = 0;
        this._resetSelection();
        await this._loadResults();
    }

    async onSearchKeydown(ev) {
        if (ev.key === "Enter") await this.onFilter();
    }

    async setTab(tab) {
        if (this.state.querying || tab === this.state.activeTab) return;
        this.state.activeTab = tab;
        this.state.offset = 0;
        this._resetSelection();
        this.state.drawerRow = false;
        await this._loadResults();
    }

    _queryArgs() {
        const f = this.state.filters;
        return [
            f.programId || false, f.weekStart || false, f.version || false, this.state.offset,
            this.state.pageSize, this.state.activeTab, this.state.searchTerm,
            this.state.airDate || false, this.state.issueFilter, this.state.sortBy,
            f.importJobId || false, this.state.sortDirection,
        ];
    }

    async _loadResults() {
        const requestId = ++this.requestId;
        this.state.querying = true;
        try {
            const result = await this.orm.call("mv.prelog_data", "fuzzy_match_search", this._queryArgs());
            if (requestId !== this.requestId) return;
            this.state.rows = result.rows || [];
            this.state.total = result.total || 0;
            this.state.offset = result.offset || 0;
            this.state.page = result.page || 0;
            this.state.pages = result.pages || 0;
            this.state.counts = result.counts || this.state.counts;
            // Distinguish "server sent 0" from "server sent nothing".
            // Without this a stale Python process (assets reload on
            // file change, Python only on restart) silently rendered
            // $0.00, which reads as a data bug rather than a stale
            // backend.
            this.state.dollarsAvailable = result.filtered_dollars !== undefined;
            this.state.dollars = result.dollars || this.state.dollars;
            this.state.filteredDollars = Number(result.filtered_dollars || 0);
            if (!this.state.dollarsAvailable) {
                console.warn(
                    "[MV] fuzzy_match_search returned no dollar totals - " +
                    "the Odoo Python process is probably running older " +
                    "code than the assets. Restart Odoo.",
                );
            }
            this.state.hasFiltered = true;
        } finally {
            if (requestId === this.requestId) this.state.querying = false;
        }
    }

    _resetSelection() {
        this.state.selectedRows = {};
        this.state.selectAllMatching = false;
        this.state.excludedRows = {};
    }

    get selectedCount() {
        return this.state.selectAllMatching
            ? Math.max(this.state.total - Object.keys(this.state.excludedRows).length, 0)
            : Object.keys(this.state.selectedRows).length;
    }
    get visibleRangeStart() { return this.state.total ? this.state.offset + 1 : 0; }
    get visibleRangeEnd() { return Math.min(this.state.offset + this.state.pageSize, this.state.total); }
    get allPageSelected() {
        return Boolean(this.state.rows.length) && this.state.rows.every((row) => this.isSelected(row));
    }
    get canSelectAllMatching() {
        return !this.state.selectAllMatching && this.allPageSelected && this.state.total > this.state.rows.length;
    }

    isSelected(row) {
        return this.state.selectAllMatching
            ? !this.state.excludedRows[row.id]
            : Boolean(this.state.selectedRows[row.id]);
    }
    toggleRow(row, ev) {
        if (this.state.selectAllMatching) {
            if (ev.target.checked) delete this.state.excludedRows[row.id];
            else this.state.excludedRows[row.id] = true;
        } else if (ev.target.checked) {
            this.state.selectedRows[row.id] = true;
        } else {
            delete this.state.selectedRows[row.id];
        }
    }
    toggleAll(ev) {
        if (this.state.selectAllMatching && !ev.target.checked) {
            this._resetSelection();
            return;
        }
        for (const row of this.state.rows) {
            if (ev.target.checked) {
                delete this.state.excludedRows[row.id];
                this.state.selectedRows[row.id] = true;
            } else {
                delete this.state.selectedRows[row.id];
            }
        }
    }
    selectEveryMatchingRow() {
        this.state.selectAllMatching = true;
        this.state.selectedRows = {};
        this.state.excludedRows = {};
    }
    clearSelection() { this._resetSelection(); }
    _selectionPayload(row = false) {
        if (row) return { all_matching: false, ids: [row.id], excluded_ids: [] };
        return this.state.selectAllMatching
            ? { all_matching: true, ids: [], excluded_ids: Object.keys(this.state.excludedRows).map(Number) }
            : { all_matching: false, ids: Object.keys(this.state.selectedRows).map(Number), excluded_ids: [] };
    }

    _bulkArgs(actionName, row = false, confirmedFuzzy = false) {
        const f = this.state.filters;
        return [
            actionName, this._selectionPayload(row), f.programId || false,
            f.weekStart || false, f.version || false, this.state.activeTab,
            this.state.searchTerm, this.state.airDate || false,
            this.state.issueFilter, this.state.sortBy, f.importJobId || false,
            confirmedFuzzy, this.state.sortDirection,
        ];
    }

    async attachSuggested() {
        if (!this.selectedCount) {
            this.notification.add("Select at least one Prelog row.", { type: "info" });
            return;
        }
        await this._runBulkAttach(false);
    }

    async attachOneSuggestion(row) {
        await this._runBulkAttach(row);
    }

    async _runBulkAttach(row = false, confirmedFuzzy = false) {
        this.state.mutating = true;
        try {
            let result = await this.orm.call(
                "mv.prelog_data", "fuzzy_workbench_bulk_action",
                this._bulkArgs("attach", row, confirmedFuzzy)
            );
            if (result.requires_confirmation) {
                this.state.mutating = false;
                const warning = `${result.fuzzy} of ${result.attachable} attachable suggestion(s) are fuzzy or contain a mismatch. Attach them anyway?`;
                if (!window.confirm(warning)) return;
                this.state.mutating = true;
                result = await this.orm.call(
                    "mv.prelog_data", "fuzzy_workbench_bulk_action",
                    this._bulkArgs("attach", row, true)
                );
            }
            this.notification.add(result.message, { type: result.attached ? "success" : "info" });
            this._resetSelection();
            this.state.drawerRow = false;
            await this._loadResults();
        } finally { this.state.mutating = false; }
    }

    async _applySchedules(payload) {
        const f = this.state.filters;
        this.state.mutating = true;
        try {
            const result = await this.orm.call("mv.prelog_data", "fuzzy_match_apply", [
                payload, Number(f.programId), f.weekStart, Number(f.version),
            ]);
            this.notification.add(result.message, { type: "success" });
            this._resetSelection();
            this.state.drawerRow = false;
            await this._loadResults();
        } finally { this.state.mutating = false; }
    }

    async setRemoved(removed, row = false) {
        const count = row ? 1 : this.selectedCount;
        if (!count) {
            this.notification.add("Select at least one Prelog row.", { type: "info" });
            return;
        }
        const verb = removed ? "remove" : "unremove";
        const warning = removed
            ? `Remove ${count} row(s)? Any attached Schedule ID will be cleared.`
            : `Unremove ${count} row(s)? Schedule suggestions will be recalculated.`;
        if (!window.confirm(warning)) return;
        this.state.mutating = true;
        try {
            const result = await this.orm.call(
                "mv.prelog_data", "fuzzy_workbench_bulk_action",
                this._bulkArgs(removed ? "remove" : "unremove", row)
            );
            this.notification.add(result.message || `${count} row(s) ${verb}d.`, { type: "success" });
            this._resetSelection();
            this.state.drawerRow = false;
            await this._loadResults();
        } finally { this.state.mutating = false; }
    }

    async deleteSelected(row = false) {
        const count = row ? 1 : this.selectedCount;
        if (!count) {
            this.notification.add("Select at least one Prelog row.", { type: "info" });
            return;
        }
        if (!window.confirm(`Permanently delete ${count} Prelog row(s)? This cannot be undone.`)) return;
        this.state.mutating = true;
        try {
            const result = await this.orm.call(
                "mv.prelog_data", "fuzzy_workbench_bulk_action",
                this._bulkArgs("delete", row)
            );
            this.notification.add(result.message, { type: "success" });
            this._resetSelection();
            this.state.drawerRow = false;
            await this._loadResults();
        } finally { this.state.mutating = false; }
    }

    get importInFlight() {
        return Boolean(this.state.importJob);
    }

    async onImport() {
        const latest = await this.orm.search("mv.prelog_import_job", [], {
            limit: 1, order: "id desc",
        });
        const previousId = latest.length ? latest[0] : 0;
        await this.action.doAction(
            "marathon_ventures.action_open_prelog_import_wizard",
            { onClose: () => { this._watchImportJob(previousId); } },
        );
    }

    clearResultsForImport() {
        this.closeDrawer();
        Object.assign(this.state, {
            rows: [], total: 0, offset: 0, page: 0, pages: 0,
            counts: {
                all: 0, matched: 0, unmatched: 0, suggestions: 0,
                no_suggestion: 0, removed: 0, overruns: 0,
            },
            selectedRows: {}, selectAllMatching: false, excludedRows: {},
        });
    }

    async _watchImportJob(previousId) {
        const created = await this.orm.searchRead(
            "mv.prelog_import_job", [["id", ">", previousId]], ["state"],
            { limit: 1, order: "id desc" },
        );
        if (!created.length) return;

        const jobId = created[0].id;
        this.clearResultsForImport();
        const fields = [
            "state", "total_row_count", "matched_count", "unmatched_count",
            "error_count", "failure_message", "program_id", "import_week",
            "prelog_version",
        ];
        const deadline = Date.now() + 10 * 60 * 1000;
        while (!this.isUnmounted && Date.now() < deadline) {
            const [job] = await this.orm.read(
                "mv.prelog_import_job", [jobId], fields,
            );
            this.state.importJob = job;
            if (job.state === "completed" || job.state === "failed") break;
            await new Promise((resolve) => setTimeout(resolve, 3000));
        }
        if (this.isUnmounted) return;

        const job = this.state.importJob;
        if (job && job.state === "completed") {
            this.notification.add(
                `Import finished: ${job.total_row_count} row(s), ` +
                `${job.matched_count} matched, ${job.unmatched_count} unmatched.`,
                { type: "success" },
            );
            if (job.program_id) this.state.filters.programId = job.program_id[0];
            if (job.import_week) this.state.filters.weekStart = job.import_week;
            if (job.prelog_version) {
                this.state.filters.version = job.prelog_version;
                await this._refreshVersions(job.prelog_version);
            }
            this.state.filters.importJobId = jobId;
            await this._loadResults();
        } else if (job && job.state === "failed") {
            this.notification.add(
                job.failure_message || "The import failed.", { type: "danger" },
            );
            await this._loadResults();
        } else {
            this.notification.add(
                "The import is still running. Press View Prelogs when it finishes.",
                { type: "info" },
            );
            await this._loadResults();
        }
        this.state.importJob = false;
    }

    async onRefresh() {
        const f = this.state.filters;
        this.state.refreshing = true;
        try {
            const result = await this.orm.call(
                "mv.prelog_data", "fuzzy_match_refresh", [
                    f.programId || false, f.weekStart || false,
                    f.version || false, f.importJobId || false,
                ],
            );
            this.notification.add(result.message, {
                type: result.attached ? "success" : "info",
            });
            await this._loadResults();
        } finally {
            this.state.refreshing = false;
        }
    }

    async detachSchedule(row) {
        if (!window.confirm(`Detach ${row.attached?.name || "the schedule"} from this Prelog row?`)) return;
        const f = this.state.filters;
        this.state.mutating = true;
        try {
            const result = await this.orm.call("mv.prelog_data", "fuzzy_match_detach", [[
                row.id,
            ], f.programId || false, f.weekStart || false, f.version || false, f.importJobId || false]);
            this.notification.add(result.message, { type: "success" });
            this.state.drawerRow = false;
            await this._loadResults();
        } finally { this.state.mutating = false; }
    }

    async attachAlternative(row, alternative) {
        if (!window.confirm(`Attach ${alternative.name} to ${row.name}?`)) return;
        await this._applySchedules([{
            prelog_id: row.id,
            schedule_id: alternative.id,
            source: "manual",
            confirmed_override: true,
            replace_existing: false,
        }]);
    }

    async attachManual(row) {
        const reference = this.state.manualSchedule.trim();
        if (!reference) {
            this.notification.add("Enter a schedule name or Odoo ID.", { type: "warning" });
            return;
        }
        const replacing = row.status === "matched";
        if (!window.confirm(`${replacing ? "Replace the current" : "Attach this"} schedule using a manual override?`)) return;
        await this._applySchedules([{
            prelog_id: row.id, schedule_id: false, schedule_ref: reference,
            source: "manual", confirmed_override: true, replace_existing: replacing,
        }]);
    }

    openDrawer(row) {
        this.state.drawerRow = row;
        this.state.manualSchedule = "";
        this.state.drawerOverrun = null;
        // For overrun rows, fetch the schedule's overrun context
        // (total prelogs, capped units, first N prelogs) so the
        // drawer can render the "Overruns" section shown in the spec.
        const schedule = row && (row.attached || row.suggested);
        if (row && row.is_overrun && schedule && schedule.id) {
            this._loadDrawerOverrun(schedule.id);
        }
    }
    async _loadDrawerOverrun(scheduleId) {
        try {
            const f = this.state.filters;
            const details = await this.orm.call(
                "mv.prelog_data",
                "fuzzy_workbench_overrun_details",
                [
                    scheduleId,
                    5,
                    f.programId || false,
                    f.weekStart || false,
                    f.version || false,
                    f.importJobId || false,
                ],
            );
            // Only apply if the drawer is still open on the same row.
            if (this.state.drawerRow) {
                this.state.drawerOverrun = details || null;
            }
        } catch (err) {
            this.state.drawerOverrun = null;
        }
    }
    closeDrawer() {
        this.state.drawerRow = false;
        this.state.manualSchedule = "";
        this.state.drawerOverrun = null;
    }
    async onExportOverrunDiagnostics() {
        if (!this._filtersAreValid()) return;
        const f = this.state.filters;
        this.state.exporting = true;
        try {
            const result = await this.orm.call(
                "mv.prelog_data", "fuzzy_overrun_diagnostics_csv",
                [
                    f.programId || false, f.weekStart || false,
                    f.version || false, f.importJobId || false,
                ],
            );
            const blob = new Blob(
                ["﻿", result.content || ""],
                { type: "text/csv;charset=utf-8" },
            );
            const url = window.URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = result.filename || "PrelogOverrunDiagnostics.csv";
            link.click();
            window.URL.revokeObjectURL(url);
        } catch (e) {
            this.notification.add(
                (e && e.data && e.data.message)
                || (e && e.message) || String(e),
                { type: "danger" },
            );
        } finally {
            this.state.exporting = false;
        }
    }

    async onExport() {
        if (!this._filtersAreValid()) return;
        const f = this.state.filters;
        this.state.exporting = true;
        try {
            const result = await this.orm.call("mv.prelog_data", "fuzzy_workbench_export_csv", [
                f.programId || false, f.weekStart || false, f.version || false, this.state.activeTab,
                this.state.searchTerm, this.state.airDate || false, this.state.issueFilter,
                this.state.sortBy, f.importJobId || false, this.state.sortDirection,
            ]);
            const blob = new Blob(["\ufeff", result.content || ""], { type: "text/csv;charset=utf-8" });
            const url = window.URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url; link.download = result.filename || "PrelogWorkbench.csv";
            document.body.appendChild(link); link.click(); link.remove(); window.URL.revokeObjectURL(url);
            this.notification.add(`${result.count || 0} row(s) exported.`, { type: "success" });
        } finally { this.state.exporting = false; }
    }

    async previousPage() {
        if (this.state.offset <= 0) return;
        this.state.offset = Math.max(this.state.offset - this.state.pageSize, 0);
        await this._loadResults();
    }
    async nextPage() {
        if (this.state.offset + this.state.pageSize >= this.state.total) return;
        this.state.offset += this.state.pageSize;
        await this._loadResults();
    }

    async onSort(column) {
        if (this.state.querying || this.state.mutating) return;
        if (this.state.sortBy === column) {
            this.state.sortDirection = this.state.sortDirection === "asc" ? "desc" : "asc";
        } else {
            this.state.sortBy = column;
            this.state.sortDirection = "asc";
        }
        this.state.offset = 0;
        this._resetSelection();
        this.state.drawerRow = false;
        await this._loadResults();
    }

    sortAria(column) {
        if (this.state.sortBy !== column) return "none";
        return this.state.sortDirection === "desc" ? "descending" : "ascending";
    }

    sortIcon(column) {
        if (this.state.sortBy !== column) return "fa fa-sort mv-fuzzy__sort-icon";
        const direction = this.state.sortDirection === "desc" ? "down" : "up";
        return `fa fa-sort-${direction} mv-fuzzy__sort-icon is-active`;
    }

    candidates() {
        const row = this.state.drawerRow;
        if (!row || !row.suggested) return [];
        const shape = (schedule, flags, extra) => ({
            schedule,
            day_mismatch: Boolean(flags.day_mismatch),
            rate_mismatch: Boolean(flags.rate_mismatch),
            length_mismatch: Boolean(flags.length_mismatch),
            time_distance: flags.time_distance,
            exact_time_match: Boolean(flags.exact_time_match),
            ...extra,
        });
        return [
            shape(row.suggested, row, {
                suggested: true,
                attachable: row.suggestion_attachable,
                alternative: null,
            }),
            ...(row.alternatives || []).map((alternative) =>
                shape(alternative, alternative, {
                    suggested: false,
                    attachable: alternative.attachable,
                    alternative,
                })
            ),
        ];
    }

    rateDelta(schedule) {
        const prelogRate = Number(this.state.drawerRow?.rate ?? 0);
        const scheduleRate = Number(schedule?.rate ?? 0);
        const difference = scheduleRate - prelogRate;
        if (!Number.isFinite(difference) || Math.abs(difference) < 0.005) return "";
        return `${difference > 0 ? "+" : "-"}$${this.formatRate(Math.abs(difference))}`;
    }

    differenceSummary(candidate) {
        const parts = [];
        // Rate appears first so an incorrectly entered schedule rate is the
        // first warning Operations sees, even when rotation drives the rank.
        if (candidate.rate_mismatch) {
            const delta = this.rateDelta(candidate.schedule);
            parts.push(delta ? `Rate mismatch (${delta})` : "Rate mismatch");
        }
        if (!candidate.exact_time_match) {
            const distance = candidate.time_distance;
            const howFar = distance === null || distance === undefined || distance === false
                ? "Outside rotation"
                : `${distance} min outside rotation`;
            parts.push(`${howFar} - ${this.state.drawerRow?.air_time || "unknown time"}`);
        }
        if (candidate.day_mismatch) {
            const day = this.spotDayName();
            parts.push(day ? `Day not allowed - ${day}` : "Day not allowed");
        }
        if (candidate.length_mismatch) {
            parts.push(`Length mismatch (${candidate.schedule.length || "unknown"})`);
        }
        return {
            count: parts.length,
            label: parts.length
                ? `${parts.length} difference${parts.length === 1 ? "" : "s"}`
                : "Exact match",
            parts,
        };
    }

    weekdayName(value) {
        const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ""));
        if (!match) return "";
        const localDate = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
        return Number.isNaN(localDate.getTime())
            ? ""
            : localDate.toLocaleDateString(undefined, { weekday: "long" });
    }

    spotDayName() {
        const row = this.state.drawerRow;
        return this.weekdayName(row?.air_date) || row?.day || "";
    }

    condenseDays(value) {
        const order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
        const parts = String(value || "").split(",").map((day) => day.trim()).filter(Boolean);
        if (parts.length < 3) return parts.join(", ") || "—";
        const indexes = parts.map((day) => order.indexOf(day.slice(0, 3)));
        if (indexes.some((index) => index < 0)) return parts.join(", ");
        const sorted = [...indexes].sort((a, b) => a - b);
        const contiguous = sorted.every(
            (value, index) => index === 0 || value === sorted[index - 1] + 1,
        );
        return contiguous
            ? `${order[sorted[0]]}–${order[sorted[sorted.length - 1]]}`
            : parts.join(", ");
    }

    formatRate(value) {
        const number = Number(value || 0);
        return Number.isFinite(number) ? number.toLocaleString(undefined, {
            minimumFractionDigits: 2, maximumFractionDigits: 2,
        }) : "";
    }
    // ---- Total Dollars ------------------------------------------
    // Server-computed over the full scope. `filteredDollars` follows
    // the tab + search + air-date + issue filter, so it is what the
    // user is actually looking at; `dollars.all` is the whole upload
    // and is what should reconcile against the file.
    formatDollars(value) {
        const n = Number(value || 0);
        return n.toLocaleString(undefined, {
            minimumFractionDigits: 2, maximumFractionDigits: 2,
        });
    }

    get totalDollarsLabel() {
        // "—" not "$0.00" when the backend didn't supply totals, so a
        // stale server is visibly different from a genuine zero.
        if (!this.state.dollarsAvailable) return "—";
        return `$${this.formatDollars(this.state.filteredDollars)}`;
    }

    get totalDollarsTitle() {
        if (!this.state.dollarsAvailable) {
            return "Totals unavailable: the server did not return dollar " +
                   "figures. Restart Odoo so the Python process picks up " +
                   "the current code.";
        }
        return this.dollarsReconcileTitle ||
               "Total for the rows currently in view (all pages, not just this one).";
    }

    /** The uploaded file's own total, when a specific upload is in view. */
    get uploadedDollars() {
        const up = this.state.latestUpload;
        if (!up || !this.state.filters.importJobId) return null;
        if (up.id !== this.state.filters.importJobId) return null;
        return Number(up.total_rate_amount || 0);
    }

    get uploadedDollarsLabel() {
        const v = this.uploadedDollars;
        return v === null ? "" : `$${this.formatDollars(v)}`;
    }

    /**
     * Reconcile the processed total against the file total.
     *
     * Compared against dollars.all (the whole upload) rather than the
     * filtered figure, and only when the view is scoped to that single
     * import job - otherwise the two describe different row sets and a
     * mismatch would be meaningless.
     *
     * A tolerance of half a cent absorbs float noise from summing
     * thousands of rows.
     */
    get dollarsReconcile() {
        const uploaded = this.uploadedDollars;
        if (uploaded === null) return null;
        const processed = Number(this.state.dollars.all || 0);
        const diff = processed - uploaded;
        const matches = Math.abs(diff) < 0.005;
        const errors = Number(
            (this.state.latestUpload && this.state.latestUpload.error_count) || 0,
        );
        return {
            matches,
            diff,
            errors,
            processedLabel: `$${this.formatDollars(processed)}`,
            uploadedLabel: `$${this.formatDollars(uploaded)}`,
            diffLabel: `${diff > 0 ? "+" : "-"}$${this.formatDollars(Math.abs(diff))}`,
            // A gap is EXPECTED when rows failed to import: the job
            // totals every parsed row, including ones that never
            // became records.
            explained: !matches && errors > 0,
        };
    }

    get dollarsReconcileClass() {
        const r = this.dollarsReconcile;
        if (!r) return "";
        if (r.matches) return "mv-fuzzy__dollars-ok";
        return r.explained
            ? "mv-fuzzy__dollars-warn"
            : "mv-fuzzy__dollars-bad";
    }

    get dollarsReconcileTitle() {
        const r = this.dollarsReconcile;
        if (!r) return "";
        if (r.matches) {
            return `Processed total matches the uploaded file (${r.uploadedLabel}).`;
        }
        if (r.explained) {
            return `Uploaded file totalled ${r.uploadedLabel} but ${r.errors} row(s) failed to import, so ${r.processedLabel} was processed (${r.diffLabel}).`;
        }
        return `Processed ${r.processedLabel} but the uploaded file totalled ${r.uploadedLabel} (${r.diffLabel}). No rows errored, so this gap needs investigating.`;
    }

    statusBadge(row) { return `mv-fuzzy__status mv-fuzzy__status--${row.status}`; }
    reasonFallback(row) {
        // An attached row must never read "Ready to attach" - it is
        // already attached. Overrun rows normally carry an explicit
        // reason from the server; this is the safety net.
        if (row.status === "matched") return "Schedule attached";
        if (row.status === "overrun") return "Over schedule capacity";
        if (row.status === "removed") return "Removed";
        return "Ready to attach";
    }
    tabTitle() {
        return {
            all: "All",
            matched: "Matched",
            unmatched: "Unmatched",
            suggestions: "Suggestions",
            no_suggestion: "No Suggestion",
            removed: "Removed",
            overruns: "Overruns",
        }[this.state.activeTab] || "All";
    }

    // ---- Record URLs, for opening in a NEW browser tab -----------
    // Action-based form (/odoo/action-<module>.<xmlid>/<res_id>) rather
    // than the bare /odoo/<model>/<id> form: the latter loads a
    // chrome-less page with no top menu, which is what we hit before.
    // Routing via the action makes the web client resolve the action
    // AND its menu, so the new tab looks like a normal Odoo screen.
    scheduleOpenUrl(schedule) {
        if (!schedule?.id) return "#";
        return `/odoo/action-marathon_ventures.action_mv_schedules/${schedule.id}`;
    }
    prelogOpenUrl(row) {
        if (!row?.id) return "#";
        return `/odoo/action-marathon_ventures.action_mv_prelog_data/${row.id}`;
    }
    async viewAllOverrunPrelogs() {
        // Open a proper list action of every prelog tied to this
        // overrun schedule (attached OR pending-suggestion sharing the
        // same deal number), so the user gets a full-chrome list with
        // the nav menu - not just a re-filter of the workbench.
        const details = this.state.drawerOverrun;
        if (!details) { this.closeDrawer(); return; }
        const ids = details.all_prelog_ids || [];
        this.closeDrawer();
        if (!ids.length) return;
        // Bulletproof domain: the server already resolved exactly which
        // prelogs belong to this overrun (attached + pending), so we
        // just open those ids directly.
        await this.action.doAction({
            type: "ir.actions.act_window",
            name: `Prelogs · ${details.schedule_name || ""}`,
            res_model: "mv.prelog_data",
            domain: [["id", "in", ids]],
            views: [[false, "list"], [false, "form"]],
            target: "current",
        });
    }
}

registry.category("actions").add("mv_prelog_fuzzy_matching", MvPrelogFuzzyMatching);
