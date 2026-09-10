#!/usr/bin/env python3
"""OSAC Workflow Exporter — GitHub Actions job queue and history metrics for Prometheus.

Polls the GitHub API for workflow run status across all repos in the org and exposes:
  - Queue depth (queued + waiting runs)
  - In-progress runs
  - Completed run counts by conclusion
  - Run duration histogram
  - JSON API at /api/jobs with detailed recent job info for Grafana Infinity
"""

import os
import re
import sys
import time
import json
import logging
import sqlite3
import statistics
import threading
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import ClassVar
from urllib.parse import urlparse, parse_qs

import requests
from prometheus_client import (
    generate_latest,
    CONTENT_TYPE_LATEST,
    Gauge,
    Counter,
    Histogram,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ORG = os.getenv("GITHUB_ORG", "osac-project")
TOKEN = os.getenv("PRIVATE_GITHUB_TOKEN")
API_URL = os.getenv("API_URL", "https://api.github.com")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "90"))
PORT = int(os.getenv("PORT", "9103"))
# Comma-separated list of repos to monitor. Empty = auto-discover active repos.
REPOS_FILTER = [r.strip() for r in os.getenv("REPOS", "").split(",") if r.strip()]
# How many days of job history to retain in the DB. A count cap alone (the
# old JOBS_HISTORY_SIZE behavior, 500 jobs shared across all repos) gets
# exhausted in ~10 hours during busy periods since PR/comment-triggered runs
# vastly outnumber scheduled ones -- silently truncating dashboards that
# select "last 7 days" (see OSAC-2211).
JOBS_HISTORY_DAYS = int(os.getenv("JOBS_HISTORY_DAYS", "60"))
# Hard cap on stored job count regardless of age, as a memory/disk safety
# net -- not expected to be the binding constraint at normal CI volume.
JOBS_HISTORY_MAX_COUNT = int(os.getenv("JOBS_HISTORY_MAX_COUNT", "100000"))
# Data directory for the SQLite DB (persists across restarts) and the
# legacy JSON cache file this exporter migrates from on first startup.
CACHE_DIR = os.getenv("CACHE_DIR", os.path.expanduser("~/.monitoring-server/data"))
DB_FILE = os.path.join(CACHE_DIR, "workflow-exporter.db")
LEGACY_CACHE_FILE = os.path.join(CACHE_DIR, "workflow-exporter-cache.json")

JOB_COLUMNS = [
    "id", "repo", "workflow", "display_name", "category", "branch",
    "pr_url", "pr_display", "status",
    "conclusion", "event", "trigger", "duration_s", "duration", "actor",
    "url", "created_at", "updated_at", "run_number", "run_attempt",
    "failed_step", "steps_json", "failure_reason", "runner_name",
]

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
queued_runs = Gauge(
    "github_actions_queued_runs",
    "Queued workflow runs per repo",
    ["org", "repo"],
)
in_progress_runs = Gauge(
    "github_actions_in_progress_runs",
    "In-progress workflow runs per repo",
    ["org", "repo"],
)
queued_total = Gauge(
    "github_actions_queued_runs_org",
    "Total queued workflow runs across all repos",
    ["org"],
)
in_progress_total = Gauge(
    "github_actions_in_progress_runs_org",
    "Total in-progress workflow runs across all repos",
    ["org"],
)
queued_by_category = Gauge(
    "github_actions_queued_runs_by_category",
    "Queued workflow runs by category (e2e, ai, lint, ci, automation, release)",
    ["org", "category"],
)
in_progress_by_category = Gauge(
    "github_actions_in_progress_runs_by_category",
    "In-progress workflow runs by category (e2e, ai, lint, ci, automation, release)",
    ["org", "category"],
)
completed_runs = Counter(
    "github_actions_completed_runs_total",
    "Completed workflow runs",
    ["org", "repo", "workflow", "conclusion"],
)
run_duration = Histogram(
    "github_actions_run_duration_seconds",
    "Workflow run duration in seconds",
    ["org", "repo", "conclusion"],
    buckets=[60, 120, 300, 600, 900, 1200, 1800, 2700, 3600, 5400, 7200],
)
failed_step_total = Counter(
    "github_actions_failed_step_total",
    "Failed workflow run steps",
    ["org", "workflow", "step"],
)
api_remaining = Gauge(
    "github_actions_api_rate_limit_remaining",
    "GitHub API rate limit remaining (workflow exporter)",
    ["org"],
)


# ---------------------------------------------------------------------------
# Exporter logic
# ---------------------------------------------------------------------------
class WorkflowExporter:
    # Ordered category mapping — first match wins (case-insensitive substring).
    # automation and ai are both checked before BOTH "e2e" and the broad
    # "ci" catch-all (test/check/build): before ci so bot-maintenance
    # workflows like "Remove ok-to-test on new push" don't get caught by
    # ci's "test" pattern, and before e2e so bot workflows that merely
    # reference e2e without running it don't get caught by e2e's bare
    # "e2e" substring -- confirmed live: "E2E on CodeRabbit approval"
    # (kicks off the real e2e workflows via the GitHub API, runs none
    # itself), "Remove e2e-ready label on new push" (label housekeeping),
    # "Scan E2E logs" (log retention), "E2E on unlock label" (same
    # API-trigger pattern as the CodeRabbit-approval workflow, just a
    # different label as the source event -- its only job is "start-e2e",
    # which calls the GitHub API and runs no test of its own), and
    # "AI diagnostic (E2E fork PR failures)" (a post-hoc bot that inspects
    # an already-completed e2e run's logs and posts/updates a PR comment --
    # "Resolve PR"/"Mark Resolved"/"Diagnose" jobs, no test execution)
    # were all miscategorized "e2e" and polluting e2e pass-rate/infra-
    # failure stats with runs that never executed a single real test.
    # ai is its own category (split out from automation) rather than
    # folded into it, so it gets its own queued/in-progress gauges and
    # failure_reason semantics (see AI_DIAGNOSIS_STEP and
    # _classify_failure_reason's "ai" branch) instead of being invisible
    # inside automation's aggregate counts.
    # release is checked before ci too, so "Build container image" matches
    # "container image" instead of ci's generic "build" pattern.
    WORKFLOW_CATEGORIES: ClassVar[dict[str, list[str]]] = {
        "automation": ["bump", "dependabot", "copilot", "slash", "ok-to-test",
                       "coderabbit approval", "e2e-ready label", "scan e2e logs",
                       "e2e on unlock label"],
        "ai":         ["ai diagnostic"],
        "e2e":        ["e2e"],
        "lint":       ["pre-commit", "lint", "checklist", "kustomize", "check image"],
        "release":    ["publish", "container image", "mirror"],
        "ci":         ["ci", "test", "check", "build"],
    }

    # GitHub Actions synthetic "workflow runs" that aren't real CI: e.g. the
    # Dependency Graph's auto-generated "Configured Graph Update: ... #<id>"
    # entries (actor dependabot[bot], event "dynamic"). Each has a unique
    # auto-generated name embedding a numeric ID, so it can never be merged
    # with anything else -- left unfiltered, every one of these becomes a
    # permanent single-occurrence entry bloating every workflow-grouped panel.
    IGNORED_EVENTS = {"dynamic"}

    # Steps across e2e flavors (VMaaS, BMaaS, CaaS, CaaS Netris) that
    # represent the product actually being installed/tested, as opposed to
    # CI plumbing (checkout, secret-fetching, cluster/infra provisioning,
    # teardown, notifications). Deliberately a *small*, stable allowlist
    # of "real work" steps rather than an ever-growing list of every infra
    # step name -- each e2e flavor invents its own setup/teardown step
    # names (e.g. BMaaS's "Setup virtual BareMetalHosts", Netris's "Deploy
    # Netris lab"), so enumerating infra steps requires updating this list
    # every time a new flavor ships; enumerating the few genuine test/
    # install steps does not. A step failing here means OSAC itself likely
    # broke; anything else failing means CI/environment broke instead.
    # Used to classify presubmit failures into failure_reason "infra" vs
    # "test" (see _classify_failure_reason).
    TEST_STEPS = frozenset({
        "Install OSAC",
        "Install infrastructure operators",
        "Deploy OSAC (make deploy-osac)",
        "Deploy OSAC from snapshot (make deploy-osac)",
        "Run E2E tests",
        "Run CaaS e2e tests",
    })

    # The one step, shared verbatim by both phases of the AI diagnostic
    # pipeline (Phase 1: embedded directly in a failed E2E *-Full-Install
    # job, categorized "e2e" not "ai" -- see WORKFLOW_CATEGORIES; Phase 2:
    # the "diagnose / Diagnose" job in the "AI diagnostic (E2E fork PR
    # failures)" listener workflow, categorized "ai"), that represents the
    # AI actually being invoked -- see osac-test-infra's own
    # ai-diagnose-failure.py, called from this exact step name in both
    # ai-diagnostic-e2e.yml and each e2e-*-full-install.yml. Every OTHER
    # step in the Diagnose job (minting a token, fetching the diff,
    # posting the comment, ...) is plumbing around this one; a "skipped"
    # Diagnose job never reaches it at all -- most commonly because there
    # was no open PR to diagnose, or the E2E job it's listening to never
    # actually ran a test in the first place (see _is_gate_job). Used only
    # for the "ai" category's failure_reason classification below -- e2e's
    # own TEST_STEPS/failure_reason logic is untouched by this.
    AI_DIAGNOSIS_STEP = "Run AI failure diagnosis"

    # Suffix used by pure pass-through relay jobs/workflows that exist only
    # to give the merge-queue ruleset a stable required-check name -- e.g.
    # "label-gate" (a whole workflow) and "e2e-caas-gate" (one job within a
    # bigger e2e workflow, alongside the real "e2e-caas-full-install" job).
    # Neither adds test/infra signal of its own: a job-level gate's only
    # step is "if an upstream job failed, exit 1", which would otherwise
    # misclassify as failure_reason "infra" (it never matches TEST_STEPS)
    # even when the real failure was already correctly classified from the
    # actual e2e job in the same run; a workflow-level gate like
    # "label-gate" is almost always a trivial auto-pass on merge_group and
    # a real-but-unrelated label check on pull_request, neither of which is
    # "CI health" signal worth counting.
    GATE_NAME_SUFFIX = "-gate"

    # Other known plumbing jobs that precede the real e2e job within each
    # e2e-*-full-install run (see osac/.github/workflows/e2e-*-full-install.yml)
    # but don't follow the "-gate" suffix convention: "changes" (dorny/paths-filter
    # precondition -- should the expensive e2e job run at all) and
    # "e2e-readiness" (fleet/capacity precondition). A failure here means a
    # precondition wasn't met, not that OSAC itself broke -- when
    # "e2e-readiness" fails, the real e2e job is skipped entirely (confirmed
    # live), so there's no product signal to attribute the failure to.
    # A small, stable, explicit list rather than a broader substring guess,
    # for the same reason TEST_STEPS above is a small allowlist rather than
    # an ever-growing denylist of infra step names.
    GATE_JOB_NAMES = frozenset({"changes", "e2e-readiness"})

    @staticmethod
    def _is_gate_name(name):
        """Whole-workflow gate check (e.g. "label-gate") -- suffix only."""
        return (name or "").lower().endswith(WorkflowExporter.GATE_NAME_SUFFIX)

    @staticmethod
    def _is_gate_job(name):
        """Job-level gate/precondition check within a bigger workflow run --
        suffix match (e.g. "e2e-caas-gate") or one of GATE_JOB_NAMES.
        """
        lname = (name or "").lower()
        return lname.endswith(WorkflowExporter.GATE_NAME_SUFFIX) or lname in WorkflowExporter.GATE_JOB_NAMES

    @staticmethod
    def _is_gate_only_failure(jobs):
        """True if every FAILED job in this run's job list is a gate/
        precondition job (see _is_gate_job) -- i.e. "e2e-readiness" (or
        another precondition) failed, the real e2e job was never even
        started (GitHub reports it "skipped"), and the only "failure"
        conclusions in the whole run belong to gate jobs relaying that
        upstream skip (confirmed live: osac run 32196167957 -- changes:
        success, e2e-readiness: failure, e2e-vmaas-gate: failure,
        e2e-vmaas-full-install: skipped). Nothing broke (not infra) and
        nothing ran (not test) -- there's no e2e signal in this run at all.
        """
        failed_jobs = [j for j in (jobs or []) if j.get("conclusion") == "failure"]
        if not failed_jobs:
            return False
        return all(WorkflowExporter._is_gate_job(j.get("name")) for j in failed_jobs)

    @staticmethod
    def _classify_failure_reason(category, failed_steps, jobs=None):
        """category: only "e2e" and "ai" jobs get classified. Returns
        "n/a" for any other category.

        failed_steps: the list from _extract_failed_steps
        ([{"display":.., "step":..}, ...]), already excluding gate/
        precondition jobs (see _is_gate_job). Only used for "e2e".
        jobs: the raw per-job list from _fetch_run_jobs. Used by "e2e" only
        to detect the gate-only-failure case below (pass None when
        unavailable, e.g. reclassifying from already-stored text, to skip
        that check); used by "ai" as its ONLY input (see below).

        For "e2e": returns "gate" if every job that actually failed was a
        gate/precondition job (see _is_gate_only_failure) -- excluded from
        all stats by get_jobs_json, not just this infra/test breakdown,
        since it's neither. Otherwise "test" if any failed step is in
        TEST_STEPS (OSAC's own install/test execution), "infra" if there's
        failure detail but none of it is a test step, and "infra" too when
        there's no per-step detail at all (e.g. the job itself shows
        conclusion "cancelled" with zero recorded steps even though the
        run's overall conclusion is "failure" -- a real observed case: a
        runner-level crash/timeout that GitHub cancelled mid-job). A
        genuine product/test failure always produces step-level data (a
        red "Run E2E tests" step); the total absence of any step data is
        itself an infra-level symptom, not an ambiguous third category.

        For "ai": returns "ran" if AI_DIAGNOSIS_STEP appears, with a
        non-skipped conclusion, in ANY job in `jobs`; "skipped" otherwise;
        "n/a" if `jobs` itself is falsy (e.g. the fetch legitimately
        returned zero job entries). Deliberately independent of the run's
        own conclusion, unlike e2e's classification above -- an AI
        diagnostic run's overall conclusion is essentially always
        "success" whether or not the Diagnose job's real work happened at
        all (see AI_DIAGNOSIS_STEP's docstring), so gating this on
        conclusion == "failure" the way callers do for e2e would never
        fire. Every ingestion call site instead calls this unconditionally
        for "ai" category records via _apply_ai_status.
        """
        if category == "ai":
            if not jobs:
                return "n/a"
            for job in jobs:
                for step in job.get("steps") or []:
                    if (step.get("name") == WorkflowExporter.AI_DIAGNOSIS_STEP
                            and step.get("conclusion") not in (None, "skipped")):
                        return "ran"
            return "skipped"
        if category != "e2e":
            return "n/a"
        if jobs is not None and WorkflowExporter._is_gate_only_failure(jobs):
            return "gate"
        if not failed_steps:
            return "infra"
        for f in failed_steps:
            if f["step"] in WorkflowExporter.TEST_STEPS:
                return "test"
        return "infra"

    @staticmethod
    def _apply_ai_status(record, jobs):
        """Set record["failure_reason"] to "ran"/"skipped"/"n/a" for
        category "ai" records, independent of the run's own conclusion --
        see _classify_failure_reason's "ai" branch for why this can't be
        gated on conclusion == "failure" the way e2e's infra/test/gate
        classification is. No-op for every other category.

        Called unconditionally (not just on failure) from each of this
        exporter's ingestion call sites (_backfill_page, _backfill_range,
        initial_load, _process_completed_run), right alongside where
        record["steps"] gets set from the same `jobs` fetch.
        """
        if record.get("category") == "ai":
            record["failure_reason"] = WorkflowExporter._classify_failure_reason("ai", None, jobs)

    @staticmethod
    def _categorize_workflow(name):
        """Categorize a workflow name using substring matching.

        Iterates WORKFLOW_CATEGORIES in order, returns first match.
        Defaults to 'ci' for unknown workflows.
        """
        lower = name.lower()
        for category, patterns in WorkflowExporter.WORKFLOW_CATEGORIES.items():
            for pattern in patterns:
                if pattern in lower:
                    return category
        return "ci"

    def __init__(self):
        self.headers = {
            "Authorization": f"token {TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        }
        self._repos_cache = []
        self._repos_cache_ts = 0
        self._active_repos = None
        self._active_repos_ts = 0
        # Current in-flight runs (queued + in_progress) — pure in-memory,
        # unrelated to the persisted job history below.
        self.active_runs = []
        self._lock = threading.Lock()
        # Branch-to-PR mapping: {repo: {branch: (pr_num, pr_url)}}
        self._pr_map = {}
        self._pr_map_ts = 0
        self._pr_backfill_done = False
        self._init_db()

    # -- SQLite persistence ---------------------------------------------------
    #
    # Job history lives in a SQLite DB (DB_FILE), not in memory: at 60 days
    # of retention (~70-80k jobs at current CI volume) a JSON-file dump on
    # every 90s poll cycle -- the previous design -- rewrites the entire
    # history every cycle, and every dashboard query re-scans the entire
    # list in Python. SQLite gives cheap appends and indexed queries
    # instead. Connections are opened short-lived, per operation, rather
    # than shared across threads (the collect() polling loop and the HTTP
    # handler both touch the DB) -- simplest way to avoid sqlite3's
    # same-thread restriction without adding a lock, and cheap enough at
    # this call frequency.

    def _db(self):
        conn = sqlite3.connect(DB_FILE, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS jobs (
                    {JOB_COLUMNS[0]} INTEGER PRIMARY KEY,
                    {", ".join(f"{c} TEXT" if c not in ("duration_s", "run_number", "run_attempt")
                                else f"{c} INTEGER" for c in JOB_COLUMNS[1:])}
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")
            # Add columns introduced after the table was first created --
            # CREATE TABLE IF NOT EXISTS doesn't alter an existing table's
            # schema, so a DB from before pr_url/pr_display existed needs
            # an explicit migration.
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            for col in ("pr_url", "pr_display", "failure_reason", "runner_name"):
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT DEFAULT ''")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pr_merges (
                    id INTEGER PRIMARY KEY,
                    repo TEXT,
                    number INTEGER,
                    title TEXT,
                    author TEXT,
                    created_at TEXT,
                    merged_at TEXT,
                    merge_seconds INTEGER,
                    first_approval_at TEXT,
                    approval_to_merge_seconds INTEGER,
                    retest_count INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pr_merges_merged_at ON pr_merges(merged_at)")
            pr_merges_cols = {row[1] for row in conn.execute("PRAGMA table_info(pr_merges)")}
            for col in ("first_approval_at", "queued_at"):
                if col not in pr_merges_cols:
                    conn.execute(f"ALTER TABLE pr_merges ADD COLUMN {col} TEXT")
            for col in ("approval_to_merge_seconds", "retest_count", "queue_wait_seconds",
                        "approval_to_queue_seconds", "via_merge_queue"):
                if col not in pr_merges_cols:
                    conn.execute(f"ALTER TABLE pr_merges ADD COLUMN {col} INTEGER")
        self._migrate_json_cache_if_needed()
        self._backfill_pr_data_from_legacy_cache()
        self._backfill_pr_approval_data_if_needed()
        self._purge_ignored_events_if_needed()
        self._recategorize_jobs_if_needed()
        self._reclassify_failure_reasons_if_needed()
        self._reclassify_ai_status_if_needed()
        self._clean_hosted_runner_noise_if_needed()

    def _purge_ignored_events_if_needed(self):
        """One-time cleanup of already-stored jobs whose event is now in
        IGNORED_EVENTS (e.g. GitHub's Dependency Graph auto-submission runs,
        event "dynamic") -- these were persisted before that filter existed
        at ingestion time, and each has a unique auto-generated name that
        permanently bloats every workflow-grouped panel. Safe to re-run:
        no-op once they're gone.
        """
        placeholders = ", ".join("?" for _ in self.IGNORED_EVENTS)
        with self._db() as conn:
            cur = conn.execute(
                f"DELETE FROM jobs WHERE event IN ({placeholders})",
                tuple(self.IGNORED_EVENTS),
            )
            if cur.rowcount:
                logger.info(
                    "Purged %d stored job(s) with now-ignored event type(s) %s",
                    cur.rowcount, sorted(self.IGNORED_EVENTS),
                )

    def _recategorize_jobs_if_needed(self):
        """Re-apply _categorize_workflow to already-stored jobs.

        Category is computed once at insert time and stored, so a
        WORKFLOW_CATEGORIES change (e.g. checking automation before the
        broad "ci" catch-all) only affects new rows unless existing ones
        are re-walked here. Safe to re-run every startup: no-op once every
        row's stored category already matches what _categorize_workflow
        would assign today.
        """
        with self._db() as conn:
            rows = conn.execute("SELECT id, workflow, category FROM jobs").fetchall()
            updated = 0
            for row in rows:
                correct = self._categorize_workflow(row["workflow"])
                if correct != row["category"]:
                    conn.execute(
                        "UPDATE jobs SET category = ? WHERE id = ?", (correct, row["id"])
                    )
                    updated += 1
            if updated:
                logger.info(
                    "Recategorized %d job(s) after a WORKFLOW_CATEGORIES change", updated
                )

    def _reclassify_failure_reasons_if_needed(self):
        """Re-apply _classify_failure_reason to already-stored failed jobs.

        failure_reason is computed at ingestion time from live per-step API
        data, but rows stored before this field existed only have the
        already-flattened `failed_step` text ("job -> step; job2 -> step2").
        Re-parses that stored text (no GitHub API calls needed) so existing
        history gets (re)classified too, not just new rows. Also re-checks
        rows already classified "infra" or "test" -- a TEST_STEPS change
        (e.g. covering a new e2e flavor's step names) only affects new rows
        unless existing ones are re-walked here, same rationale as
        _recategorize_jobs_if_needed for WORKFLOW_CATEGORIES. Safe to
        re-run every startup: no-op once every failed row already matches
        what _classify_failure_reason would assign today.

        The live-jobs list _classify_failure_reason optionally uses to
        detect a gate-only failure (see _is_gate_only_failure) isn't
        available here -- only the flattened text is stored, not each
        job's raw conclusion. Approximated instead from the stored
        "job -> step" entries themselves: if every entry's job name is a
        gate job (rows recorded before the ingestion-time gate filter
        existed still have these), it's a gate-only failure by the same
        definition. Rows recorded after that filter existed have an empty
        failed_step for a true gate-only failure (the entries were already
        dropped before storage) -- for those, an empty failed_step is
        genuinely ambiguous from text alone (it could equally mean "no
        data at all", which is legitimately "infra"), so a row already
        classified "gate" is left as-is rather than re-derived here.
        Without that, every exporter restart would silently flip an
        already-correct "gate" row back to "infra" the moment its
        failed_step happens to be empty -- confirmed live, e.g. osac run
        #32265159432, reclassified back and forth across restarts before
        this guard existed.

        Excludes category "ai" rows: this function only knows how to
        re-derive e2e's gate/test/infra reasons from stored failed_step
        text, which doesn't apply to "ai"'s ran/skipped classification at
        all (see _classify_failure_reason's "ai" branch) -- calling it on
        one with jobs=None would return "n/a" and silently clobber an
        already-correct "ran"/"skipped" value on every restart.
        _reclassify_ai_status_if_needed handles "ai" rows separately, from
        stored steps_json instead of failed_step text.
        """
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, category, failed_step, failure_reason FROM jobs "
                "WHERE conclusion = 'failure' AND category != 'ai'"
            ).fetchall()
            updated = 0
            for row in rows:
                entries = [e for e in (row["failed_step"] or "").split("; ") if e]
                if row["category"] == "e2e" and entries and all(
                    self._is_gate_job(entry.split(" → ")[0]) for entry in entries
                ):
                    reason = "gate"
                elif row["category"] == "e2e" and not entries and row["failure_reason"] == "gate":
                    reason = "gate"
                else:
                    steps = [{"step": entry.split(" → ")[-1]} for entry in entries]
                    reason = self._classify_failure_reason(row["category"], steps)
                if reason != row["failure_reason"]:
                    conn.execute(
                        "UPDATE jobs SET failure_reason = ? WHERE id = ?", (reason, row["id"])
                    )
                    updated += 1
            if updated:
                logger.info(
                    "Reclassified failure_reason for %d already-stored failed job(s)", updated
                )

    def _reclassify_ai_status_if_needed(self):
        """Backfill failure_reason ("ran"/"skipped") for already-stored
        "ai" category rows -- e.g. rows just recategorized from
        "automation" to "ai" by _recategorize_jobs_if_needed above (a
        fresh deploy of the "ai" category itself), or upgraded via a
        WORKFLOW_CATEGORIES/AI_DIAGNOSIS_STEP change later.

        No GitHub API calls: reuses the already-stored steps_json (see
        _extract_step_durations). Its presence for AI_DIAGNOSIS_STEP is a
        reliable enough signal from stored data alone even though
        steps_json doesn't retain each step's conclusion (unlike the live
        `jobs` _classify_failure_reason's "ai" branch prefers) -- the
        Diagnose job is only ever entirely skipped or fully executed,
        never partially, and _extract_step_durations already drops every
        step belonging to a job whose OWN conclusion isn't success/failure
        (i.e. a skipped Diagnose job contributes zero steps at all) -- so
        "does AI_DIAGNOSIS_STEP appear in the stored list" and "did it
        have a non-skipped conclusion" agree in every real case this
        pipeline produces.

        Safe to re-run every startup: no-op once every "ai" row already
        has the correct value.

        A row with empty/malformed steps_json is genuinely ambiguous from
        stored data alone -- it can mean either a legitimately skipped
        Diagnose job (the common case) or "the jobs fetch itself returned
        zero entries" (the rare, degenerate case _classify_failure_reason's
        "ai" branch calls "n/a" at ingestion time, when it still had the
        live `jobs` list to tell the two apart). Rewriting an existing
        "n/a" to "skipped" here would assert something this migration
        can't actually verify -- so an existing "n/a" is left alone in
        that case rather than downgraded; only a row with real step data
        gets (re)classified to "ran"/"skipped".
        """
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, steps_json, failure_reason FROM jobs WHERE category = 'ai'"
            ).fetchall()
            updated = 0
            for row in rows:
                try:
                    steps = json.loads(row["steps_json"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    steps = []
                if not steps:
                    if row["failure_reason"] == "n/a":
                        continue
                    reason = "skipped"
                else:
                    reason = "ran" if any(
                        s.get("name") == WorkflowExporter.AI_DIAGNOSIS_STEP for s in steps
                    ) else "skipped"
                if reason != row["failure_reason"]:
                    conn.execute(
                        "UPDATE jobs SET failure_reason = ? WHERE id = ?", (reason, row["id"])
                    )
                    updated += 1
            if updated:
                logger.info(
                    "Reclassified failure_reason for %d already-stored 'ai' job(s)", updated
                )

    def _clean_hosted_runner_noise_if_needed(self):
        """Strip GitHub-hosted runner entries ("GitHub Actions <id>") from
        runner_name on already-stored rows -- that exclusion was added
        after runner_name tracking already existed, so rows ingested in
        between have the noise baked in. Pure string cleanup on the
        already-stored value, no GitHub API calls needed. Safe to re-run:
        no-op once every stored row is already clean.
        """
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, runner_name FROM jobs WHERE runner_name LIKE '%GitHub Actions %'"
            ).fetchall()
            updated = 0
            for row in rows:
                names = [
                    n for n in row["runner_name"].split(", ")
                    if n and not n.startswith("GitHub Actions ")
                ]
                cleaned = ", ".join(names)
                if cleaned != row["runner_name"]:
                    conn.execute(
                        "UPDATE jobs SET runner_name = ? WHERE id = ?", (cleaned, row["id"])
                    )
                    updated += 1
            if updated:
                logger.info(
                    "Cleaned GitHub-hosted-runner noise from %d already-stored job(s)", updated
                )

    def _migrate_json_cache_if_needed(self):
        """One-time import of the legacy JSON cache into the new DB.

        Only runs if the jobs table is empty and the old cache file exists
        -- preserves whatever history was already collected under the old
        design instead of starting from zero. The old file is renamed
        (not deleted) so it isn't re-imported on the next restart.
        """
        if not os.path.exists(LEGACY_CACHE_FILE):
            return
        with self._db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        if count > 0:
            return

        try:
            with open(LEGACY_CACHE_FILE) as f:
                data = json.load(f)
            jobs = data.get("recent_jobs", [])
            imported = 0
            for job in jobs:
                if "display_name" not in job:
                    repo = job.get("repo", "")
                    wf = job.get("workflow", "unknown")
                    job["display_name"] = f"{repo} / {wf}"
                if "category" not in job:
                    job["category"] = self._categorize_workflow(
                        job.get("workflow", "unknown"))
                if self._upsert_job(job):
                    imported += 1
            os.replace(LEGACY_CACHE_FILE, LEGACY_CACHE_FILE + ".migrated")
            logger.info("Migrated %d/%d jobs from legacy JSON cache into SQLite",
                         imported, len(jobs))
        except Exception:
            logger.exception("Failed to migrate legacy JSON cache")

    def _backfill_pr_data_from_legacy_cache(self):
        """One-time backfill of pr_url/pr_display for jobs already imported
        from the legacy JSON cache before pr_url/pr_display were tracked.

        _migrate_json_cache_if_needed() only imports once (it skips the
        whole file if the jobs table is already non-empty), so if that
        import already ran before pr_url/pr_display existed in
        JOB_COLUMNS, those rows are stuck without PR info even though the
        renamed cache file still has it. Re-reads that renamed file (if
        still present) and patches matching rows, then renames it again so
        this doesn't re-scan it on every restart.
        """
        migrated_file = LEGACY_CACHE_FILE + ".migrated"
        if not os.path.exists(migrated_file):
            return
        try:
            with open(migrated_file) as f:
                data = json.load(f)
            updated = 0
            with self._db() as conn:
                for job in data.get("recent_jobs", []):
                    if not job.get("pr_url"):
                        continue
                    cur = conn.execute(
                        "UPDATE jobs SET pr_url = :pr_url, pr_display = :pr_display "
                        "WHERE id = :id AND (pr_url IS NULL OR pr_url = '')",
                        {
                            "pr_url": job["pr_url"],
                            "pr_display": job.get("pr_display", ""),
                            "id": job["id"],
                        },
                    )
                    updated += cur.rowcount
            os.replace(migrated_file, migrated_file + ".pr-backfilled")
            logger.info("Backfilled pr_url/pr_display for %d historical jobs", updated)
        except Exception:
            logger.exception("Failed to backfill PR data from legacy cache")

    def _upsert_job(self, record):
        """Insert a job record, or overwrite it if a row with the same id
        already exists but with an older run_attempt.

        A GitHub run keeps the same id across re-runs (e.g. "re-run failed
        jobs") -- only run_attempt increments and the run's own
        conclusion/duration/steps change to reflect the latest attempt.
        Without this, a failed run that's later successfully re-run would
        leave a permanently stale "failure" row, since the id alone would
        already look "seen".

        Returns True if a row was inserted or updated, False if an
        existing row's run_attempt was already >= the incoming one (i.e.
        nothing changed).
        """
        row = {c: record.get(c) for c in JOB_COLUMNS if c != "steps_json"}
        row["id"] = record.get("id")
        row["steps_json"] = json.dumps(record.get("steps", []))
        placeholders = ", ".join(f":{c}" for c in JOB_COLUMNS)
        update_clause = ", ".join(
            f"{c} = excluded.{c}" for c in JOB_COLUMNS if c != "id"
        )
        with self._db() as conn:
            cur = conn.execute(
                f"INSERT INTO jobs ({', '.join(JOB_COLUMNS)}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {update_clause} "
                f"WHERE excluded.run_attempt > jobs.run_attempt",
                row,
            )
            return cur.rowcount > 0

    def _prune_jobs(self):
        """Evict jobs older than JOBS_HISTORY_DAYS, then enforce the hard
        JOBS_HISTORY_MAX_COUNT cap as a disk safety net.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=JOBS_HISTORY_DAYS)).isoformat()
        with self._db() as conn:
            conn.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))
            conn.execute(
                "DELETE FROM jobs WHERE id NOT IN "
                "(SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)",
                (JOBS_HISTORY_MAX_COUNT,),
            )

    def _prune_pr_merges(self):
        """Evict merged-PR records older than JOBS_HISTORY_DAYS (same
        retention window as jobs), then enforce JOBS_HISTORY_MAX_COUNT as a
        disk safety net -- same pattern as _prune_jobs, keyed on merged_at
        instead of created_at since "how far back does PR merge-time data
        go" is what matters for this table.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=JOBS_HISTORY_DAYS)).isoformat()
        with self._db() as conn:
            conn.execute("DELETE FROM pr_merges WHERE merged_at < ?", (cutoff,))
            conn.execute(
                "DELETE FROM pr_merges WHERE id NOT IN "
                "(SELECT id FROM pr_merges ORDER BY merged_at DESC LIMIT ?)",
                (JOBS_HISTORY_MAX_COUNT,),
            )

    def get_cache_coverage(self):
        """Return the oldest job's created_at in the DB -- how far back the
        exporter's data actually goes, independent of any dashboard query
        filters. None if empty.
        """
        with self._db() as conn:
            row = conn.execute("SELECT MIN(created_at) FROM jobs").fetchone()
        return row[0] if row else None

    # -- helpers -------------------------------------------------------------

    def _update_rate_limit(self, resp):
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            api_remaining.labels(org=ORG).set(int(remaining))

    def _get(self, url):
        resp = requests.get(url, headers=self.headers, timeout=15)
        self._update_rate_limit(resp)
        return resp

    # -- repo listing --------------------------------------------------------

    def get_repos(self):
        """List non-archived repos in the org (cached 10 min)."""
        now = time.time()
        if self._repos_cache and now - self._repos_cache_ts < 600:
            return self._repos_cache

        repos = []
        url = f"{API_URL}/orgs/{ORG}/repos?per_page=100&type=all"
        while url:
            resp = self._get(url)
            if not resp.ok:
                logger.error("Failed to list repos: %s %s", resp.status_code, resp.text[:200])
                return self._repos_cache
            repos.extend(r["name"] for r in resp.json() if not r.get("archived"))
            url = resp.links.get("next", {}).get("url")

        self._repos_cache = repos
        self._repos_cache_ts = now
        logger.info("Cached %d repos", len(repos))
        return repos

    def get_active_repos(self):
        """Return repos that have at least one workflow run (cached 30 min)."""
        now = time.time()
        if self._active_repos is not None and now - self._active_repos_ts < 1800:
            return self._active_repos

        all_repos = self.get_repos()
        active = []
        for repo in all_repos:
            resp = self._get(
                f"{API_URL}/repos/{ORG}/{repo}/actions/runs?per_page=1"
            )
            if resp.ok and resp.json().get("total_count", 0) > 0:
                active.append(repo)
            elif resp.status_code == 409:
                continue

        self._active_repos = active
        self._active_repos_ts = now
        logger.info("Active repos (with Actions): %d / %d", len(active), len(all_repos))
        return active

    def _refresh_pr_map(self, repos):
        """Fetch open+recently-closed PRs per repo, build branch->PR map.

        Refreshed at most once per poll cycle (cached for POLL_INTERVAL).
        Costs 1 API call per repo (~5 calls total). The runs API's own
        `pull_requests` field is often empty for a pull_request-triggered
        run, so this is the fallback used to resolve a PR from the run's
        head branch instead.
        """
        now = time.time()
        if self._pr_map and now - self._pr_map_ts < POLL_INTERVAL:
            return
        pr_map = {}
        for repo in repos:
            mapping = {}
            for state in ("open", "closed"):
                url = (f"{API_URL}/repos/{ORG}/{repo}/pulls"
                       f"?state={state}&per_page=30&sort=updated"
                       f"&direction=desc")
                try:
                    resp = self._get(url)
                    if resp.status_code == 200:
                        for pr in resp.json():
                            branch = pr.get("head", {}).get("ref", "")
                            num = pr.get("number")
                            if branch and num:
                                mapping[branch] = (
                                    num,
                                    f"https://github.com/{ORG}/{repo}/pull/{num}",
                                )
                            if state == "closed" and pr.get("merged_at"):
                                self._upsert_pr_merge(repo, pr)
                except Exception:
                    logger.debug("Failed to fetch PRs for %s/%s", repo, state)
            pr_map[repo] = mapping
        self._pr_map = pr_map
        self._pr_map_ts = now
        total = sum(len(v) for v in pr_map.values())
        logger.info("PR map refreshed: %d branches across %d repos", total, len(pr_map))

    def _lookup_pr(self, repo, branch):
        """Look up PR number and URL from the branch->PR map."""
        mapping = self._pr_map.get(repo, {})
        return mapping.get(branch, (None, ""))

    def _fetch_first_approval(self, repo, pr_number):
        """Earliest human-reviewer APPROVED review's timestamp for a PR.

        Excludes bot reviewers (e.g. coderabbitai[bot]) deliberately --
        automated review tools typically approve within seconds of a PR
        being opened, which would collapse this metric back to roughly
        "time since PR opened" for any PR that has one, defeating the
        entire point of separating approval-to-merge from open-to-merge.

        Returns one of three distinct outcomes, which callers must not
        conflate:
        - a timestamp string: found a human approval.
        - "": the API call succeeded but there's genuinely no human
          approval on record (e.g. merged by an admin override) -- safe
          to store and never re-check.
        - None: the fetch itself failed (network error, rate limit, non-2xx
          response) -- callers must treat this as "unknown, try again
          later", never as "confirmed no approval", or a transient failure
          would get permanently misrecorded as "this PR was never
          approved" and silently exclude it from the metric forever.
        """
        try:
            resp = self._get(f"{API_URL}/repos/{ORG}/{repo}/pulls/{pr_number}/reviews?per_page=100")
        except requests.exceptions.RequestException:
            logger.warning("Failed to fetch reviews for %s#%s (network error), will retry later",
                            repo, pr_number)
            return None
        if not resp.ok:
            return None
        approvals = [
            r for r in resp.json()
            if r.get("state") == "APPROVED" and r.get("user", {}).get("type") != "Bot"
        ]
        if not approvals:
            return ""
        approvals.sort(key=lambda r: r["submitted_at"])
        return approvals[0]["submitted_at"]

    def _count_e2e_retests(self, repo, pr_number):
        """Number of e2e runs for this PR beyond the first -- each
        /retest (or /test) slash command, or new push, triggers another
        full e2e run (see AGENTS.md's slash-command.yml), so this counts
        purely from already-stored job history, no extra API calls.
        """
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE repo = :repo AND pr_display = :pr_display "
                "AND category = 'e2e' AND event = 'pull_request'",
                {"repo": repo, "pr_display": f"#{pr_number}"},
            ).fetchone()
        return max(0, (row["c"] if row else 0) - 1)

    def _lookup_queued_at(self, repo, pr_number):
        """Latest (not earliest) merge_group job's created_at for this PR,
        i.e. when the queue attempt that actually led to the merge started
        -- a PR that gets dequeued (failed batch, bisection) and re-enters
        later would otherwise have its very first, unrelated attempt
        counted as "entered queue", inflating queue-wait for a reason that
        has nothing to do with the merge that eventually happened.

        Pure local query against already-stored jobs (see _make_job_record's
        merge_group branch, which populates pr_display the same way
        pull_request runs already are) -- no GitHub API call, so safe to
        call on every _upsert_pr_merge invocation rather than caching.
        Returns None if this PR never went through the merge queue.
        """
        with self._db() as conn:
            row = conn.execute(
                "SELECT MAX(created_at) AS queued_at FROM jobs "
                "WHERE repo = :repo AND pr_display = :pr_display AND event = 'merge_group'",
                {"repo": repo, "pr_display": f"#{pr_number}"},
            ).fetchone()
        return row["queued_at"] if row else None

    @staticmethod
    def _seconds_between(start_iso, end_iso):
        """max(0, end - start) in whole seconds between two ISO-8601
        timestamps, or None if either is falsy/unparseable. Shared by
        every pr_merges timing calculation (approval_to_merge_seconds and
        now the queue-wait fields) to avoid re-deriving the same
        try/except dance at each call site.
        """
        if not start_iso or not end_iso:
            return None
        try:
            start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            return None
        return max(0, round((end_dt - start_dt).total_seconds()))

    def _upsert_pr_merge(self, repo, pr):
        """Persist a merged PR's timing/retest data into pr_merges.

        Piggybacks on _refresh_pr_map's existing per-repo "closed" PR fetch
        (already polled every cycle for the branch->PR map) -- no extra API
        calls for the open-to-merge time itself. Keyed by the PR's
        GitHub-global `id` (stable, unique across repos), so this is
        naturally idempotent across polls.

        first_approval_at requires one extra API call per PR (reviews
        aren't in the /pulls list response) -- only fetched when the
        column is truly NULL (never checked), not just falsy, so a PR
        confirmed to have no human approval (stored as "", distinct from
        NULL) doesn't get re-fetched every single 90s poll cycle for as
        long as it sits in _refresh_pr_map's 30-most-recent window --
        confirmed live this was happening for every one of the ~55% of
        PRs here merged without a formal approval, before this fix.
        """
        try:
            created = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
            merged = datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError):
            return
        merge_seconds = max(0, round((merged - created).total_seconds()))

        with self._db() as conn:
            existing = conn.execute(
                "SELECT first_approval_at FROM pr_merges WHERE id = ?", (pr["id"],)
            ).fetchone()
        if existing is None or existing["first_approval_at"] is None:
            # NULL means "never checked" (brand new row, or a fetch
            # previously failed and left it unresolved) -- fetch now. A
            # failed fetch here again returns None, which we store as-is
            # (NULL), naturally retrying on a future poll cycle rather
            # than getting stuck as a false "confirmed no approval".
            first_approval_at = self._fetch_first_approval(repo, pr["number"])
        else:
            first_approval_at = existing["first_approval_at"]

        approval_to_merge_seconds = None
        if first_approval_at:
            try:
                approved = datetime.fromisoformat(first_approval_at.replace("Z", "+00:00"))
                approval_to_merge_seconds = max(0, round((merged - approved).total_seconds()))
            except (ValueError, TypeError):
                pass

        retest_count = self._count_e2e_retests(repo, pr["number"])

        # Merge-queue timing -- pure local lookup (see _lookup_queued_at),
        # so unlike first_approval_at this is always recomputed, never
        # cached: merge_group jobs for a just-merged PR can still be
        # trickling into the jobs table across polling cycles.
        queued_at = self._lookup_queued_at(repo, pr["number"])
        via_merge_queue = 1 if queued_at else 0
        queue_wait_seconds = self._seconds_between(queued_at, pr["merged_at"])
        approval_to_queue_seconds = self._seconds_between(first_approval_at, queued_at)

        with self._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pr_merges "
                "(id, repo, number, title, author, created_at, merged_at, merge_seconds, "
                "first_approval_at, approval_to_merge_seconds, retest_count, "
                "queued_at, queue_wait_seconds, approval_to_queue_seconds, via_merge_queue) "
                "VALUES (:id, :repo, :number, :title, :author, :created_at, :merged_at, :merge_seconds, "
                ":first_approval_at, :approval_to_merge_seconds, :retest_count, "
                ":queued_at, :queue_wait_seconds, :approval_to_queue_seconds, :via_merge_queue)",
                {
                    "id": pr["id"],
                    "repo": repo,
                    "number": pr.get("number"),
                    "title": pr.get("title", ""),
                    "author": pr.get("user", {}).get("login", ""),
                    "created_at": pr["created_at"],
                    "merged_at": pr["merged_at"],
                    "merge_seconds": merge_seconds,
                    "first_approval_at": first_approval_at,
                    "approval_to_merge_seconds": approval_to_merge_seconds,
                    "retest_count": retest_count,
                    "queued_at": queued_at,
                    "queue_wait_seconds": queue_wait_seconds,
                    "approval_to_queue_seconds": approval_to_queue_seconds,
                    "via_merge_queue": via_merge_queue,
                },
            )

    def _backfill_queue_data_if_needed(self):
        """One-time-per-process backfill of queue_wait_seconds/etc. for
        pr_merges rows recorded before those columns existed. Called once
        from collect() (behind _pr_backfill_done), the same lifecycle as
        _backfill_missing_pr_data -- not an _init_db migration.

        Unlike _backfill_pr_approval_data_if_needed, this makes no GitHub
        API calls (_lookup_queued_at is a pure local jobs-table query), so
        it isn't gated on TOKEN and processes every affected row in one
        pass rather than a rate-limit-driven cap. Targets rows where
        via_merge_queue IS NULL (never computed) -- 0 is a real, settled
        "confirmed not via the queue" answer and must not be re-checked
        on the next restart, same NULL-vs-falsy distinction used for
        first_approval_at elsewhere in this class.
        """
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, repo, number, merged_at, first_approval_at FROM pr_merges "
                "WHERE via_merge_queue IS NULL"
            ).fetchall()
        if not rows:
            return
        updated = 0
        # One write connection reused for every row, not one per iteration
        # -- _lookup_queued_at's own short-lived read connection per row
        # is unaffected (and fine alongside this one under WAL).
        with self._db() as conn:
            for row in rows:
                queued_at = self._lookup_queued_at(row["repo"], row["number"])
                via_merge_queue = 1 if queued_at else 0
                queue_wait_seconds = self._seconds_between(queued_at, row["merged_at"])
                approval_to_queue_seconds = self._seconds_between(row["first_approval_at"], queued_at)
                conn.execute(
                    "UPDATE pr_merges SET queued_at = ?, queue_wait_seconds = ?, "
                    "approval_to_queue_seconds = ?, via_merge_queue = ? WHERE id = ?",
                    (queued_at, queue_wait_seconds, approval_to_queue_seconds, via_merge_queue, row["id"]),
                )
                updated += 1
        logger.info("Backfilled merge-queue timing for %d pr_merges row(s)", updated)

    def _backfill_pr_approval_data_if_needed(self):
        """One-time-ish startup backfill for pr_merges rows recorded before
        approval-time tracking existed, or that have since fallen out of
        _refresh_pr_map's 30-most-recent-closed-PRs-per-repo window (and so
        would otherwise never get this data filled in by normal polling).

        Skipped entirely without a token (e.g. local dry-run testing
        against a copy of the DB) -- this makes real API calls, unlike
        every other _init_db migration, so it shouldn't turn constructing
        a WorkflowExporter() into a slow, network-dependent operation in
        contexts that never call collect()/initial_load() anyway.
        Bounded to 200 rows per startup as a rate-limit safety net.

        Only targets rows where first_approval_at is truly NULL (never
        checked) -- "" (confirmed no human approval) is intentionally
        excluded so this doesn't re-fetch settled answers every restart.
        Rows where the fetch itself fails are simply skipped (left NULL),
        not written with a failure result, so a transient network/API
        error can't get permanently misrecorded as "no approval" -- they
        naturally retry on the next startup or poll cycle instead.
        """
        if not TOKEN:
            return
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, repo, number, merged_at FROM pr_merges "
                "WHERE first_approval_at IS NULL "
                "LIMIT 200"
            ).fetchall()
        if not rows:
            return
        updated = 0
        for row in rows:
            first_approval_at = self._fetch_first_approval(row["repo"], row["number"])
            if first_approval_at is None:
                continue  # fetch failed -- leave NULL, retry later
            retest_count = self._count_e2e_retests(row["repo"], row["number"])
            approval_to_merge_seconds = None
            if first_approval_at:
                try:
                    merged = datetime.fromisoformat(row["merged_at"].replace("Z", "+00:00"))
                    approved = datetime.fromisoformat(first_approval_at.replace("Z", "+00:00"))
                    approval_to_merge_seconds = max(0, round((merged - approved).total_seconds()))
                except (ValueError, TypeError):
                    pass
            with self._db() as conn:
                conn.execute(
                    "UPDATE pr_merges SET first_approval_at = ?, "
                    "approval_to_merge_seconds = ?, retest_count = ? WHERE id = ?",
                    (first_approval_at, approval_to_merge_seconds, retest_count, row["id"]),
                )
            updated += 1
        logger.info("Backfilled approval/retest data for %d pr_merges row(s)", updated)

    def _backfill_missing_pr_data(self):
        """One-time catch-up pass: fill pr_url/pr_display for already-stored
        pull_request and merge_group jobs that predate PR tracking (or were
        collected while it was broken).

        _upsert_job only touches a row when a run's run_attempt increases,
        so an already-completed run is never revisited by normal polling
        -- without this, jobs stored before pr_url/pr_display existed
        would stay stuck with an empty PR column forever, even though
        _make_job_record now resolves it correctly for every newly
        collected run. pull_request rows only resolve branches still
        present in the live PR map (recently open/closed, same as
        _lookup_pr elsewhere); PRs old enough to have fallen out of that
        window stay unresolved. merge_group rows have no such limitation
        -- the PR number is embedded in the branch name itself
        (_extract_merge_queue_pr), so every historical merge_group row
        resolves in one pass. Called once per process lifetime since
        that's the only case that can ever improve for pull_request rows.
        """
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, repo, branch, event FROM jobs "
                "WHERE event IN ('pull_request', 'merge_group') "
                "AND (pr_url IS NULL OR pr_url = '')"
            ).fetchall()
            updated = 0
            for row in rows:
                if row["event"] == "merge_group":
                    pr_num = self._extract_merge_queue_pr(row["branch"])
                    pr_url = ""
                else:
                    pr_num, pr_url = self._lookup_pr(row["repo"], row["branch"])
                if not pr_num:
                    continue
                pr_url = pr_url or f"https://github.com/{ORG}/{row['repo']}/pull/{pr_num}"
                conn.execute(
                    "UPDATE jobs SET pr_url = :pr_url, pr_display = :pr_display WHERE id = :id",
                    {"pr_url": pr_url, "pr_display": f"#{pr_num}", "id": row["id"]},
                )
                updated += 1
        logger.info(
            "PR backfill: resolved %d/%d already-stored pull_request/merge_group jobs "
            "missing PR info (rest not in the current open/recently-closed PR window)",
            updated, len(rows),
        )

    # -- helpers for detailed job info ---------------------------------------

    # GitHub's merge queue runs checks against a temporary branch named
    # "gh-readonly-queue/<base>/pr-<N>-<sha>" -- this is the only place
    # the PR number is exposed for a merge_group-triggered run. `.+`
    # (not `[^/]+`) for <base>, anchored on the pr-<N>-<sha> suffix: a
    # base branch containing its own "/" (e.g. "release/4.20") would
    # otherwise shift <base>'s match short and miss the PR number
    # entirely. Neither repo currently queues against anything but
    # "main", but the anchored-suffix form costs nothing and doesn't
    # silently break if that changes.
    MERGE_QUEUE_BRANCH_RE = re.compile(r"^gh-readonly-queue/.+/pr-(\d+)-[0-9a-f]+$")

    @staticmethod
    def _extract_merge_queue_pr(branch):
        """PR number embedded in a merge-queue branch name, or None."""
        m = WorkflowExporter.MERGE_QUEUE_BRANCH_RE.match(branch or "")
        return int(m.group(1)) if m else None

    @staticmethod
    def _extract_trigger(run):
        """Derive a human-readable trigger label from the run."""
        event = run.get("event", "unknown")
        if event == "pull_request":
            prs = run.get("pull_requests") or []
            if prs:
                pr_num = prs[0].get("number", "?")
                return f"PR #{pr_num}"
            # head_branch may hint at the PR
            return f"PR ({run.get('head_branch', '?')})"
        if event == "merge_group":
            pr_num = WorkflowExporter._extract_merge_queue_pr(run.get("head_branch", ""))
            return f"merge queue (PR #{pr_num})" if pr_num else "merge queue"
        if event == "push":
            return f"push ({run.get('head_branch', '?')})"
        if event == "schedule":
            return "scheduled"
        if event == "workflow_dispatch":
            return "manual"
        return event

    def _make_job_record(self, run, repo):
        """Build a flat dict for the JSON API from a workflow run."""
        started = run.get("run_started_at") or run.get("created_at", "")
        ended = run.get("updated_at", "")
        duration_s = 0
        if started and ended:
            try:
                t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(ended.replace("Z", "+00:00"))
                duration_s = max(0, (t1 - t0).total_seconds())
            except (ValueError, TypeError):
                pass

        conclusion = run.get("conclusion") or ""
        status = run.get("status", "unknown")
        display_status = conclusion if conclusion else status

        # Resolve PR number/URL for pull_request-triggered runs. The run's
        # own pull_requests array is often empty (a GitHub API quirk for
        # forked-repo PRs in particular), so fall back to the branch->PR
        # map built from the pulls API.
        branch = run.get("head_branch", "")
        pr_url = ""
        pr_display = ""
        if run.get("event") == "pull_request":
            prs = run.get("pull_requests") or []
            pr_num = prs[0].get("number") if prs else None
            if not pr_num:
                pr_num, pr_url = self._lookup_pr(repo, branch)
            if pr_num:
                pr_url = pr_url or f"https://github.com/{ORG}/{repo}/pull/{pr_num}"
                pr_display = f"#{pr_num}"
        elif run.get("event") == "merge_group":
            # GitHub's merge queue runs checks against a temporary branch
            # named "gh-readonly-queue/<base>/pr-<N>-<sha>" -- the PR
            # number is embedded in the branch name itself, so this needs
            # no extra API call (unlike the pull_request fallback above).
            pr_num = WorkflowExporter._extract_merge_queue_pr(branch)
            if pr_num:
                pr_url = f"https://github.com/{ORG}/{repo}/pull/{pr_num}"
                pr_display = f"#{pr_num}"

        workflow_name = run.get("name", "unknown")
        return {
            "id": run.get("id"),
            "repo": repo,
            "workflow": workflow_name,
            "display_name": f"{repo} / {workflow_name}",
            "category": WorkflowExporter._categorize_workflow(workflow_name),
            "branch": branch,
            "pr_url": pr_url,
            "pr_display": pr_display,
            "status": display_status,
            "conclusion": conclusion,
            "event": run.get("event", "unknown"),
            "trigger": WorkflowExporter._extract_trigger(run),
            "duration_s": round(duration_s),
            "duration": WorkflowExporter._fmt_duration(duration_s),
            "actor": run.get("actor", {}).get("login", ""),
            "url": run.get("html_url", ""),
            "created_at": run.get("created_at", ""),
            "updated_at": ended,
            "run_number": run.get("run_number", 0),
            "run_attempt": run.get("run_attempt", 1),
            "runner_name": "",
        }

    @staticmethod
    def _fmt_duration(seconds):
        if seconds <= 0:
            return "-"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}h {m}m {s}s"
        if m > 0:
            return f"{m}m {s}s"
        return f"{s}s"

    # -- metric collection ---------------------------------------------------

    def _run_count(self, repo, status):
        """Get total_count of runs with given status (single API call)."""
        resp = self._get(
            f"{API_URL}/repos/{ORG}/{repo}/actions/runs?status={status}&per_page=1"
        )
        if not resp.ok:
            return 0
        return resp.json().get("total_count", 0)

    def _fetch_active_runs(self, repo):
        """Fetch current queued and in_progress runs for the active list.

        Also resolves which machine each run is on for in_progress runs --
        the whole point of the active list is "what's running right now",
        so runner_name matters most there. A queued run has no runner
        assigned yet, so _fetch_run_jobs on one would just burn an API
        call for a jobs list with nothing meaningful to extract -- skipped
        entirely for that status, which also keeps a large queued backlog
        (exactly the scenario this list needs to represent accurately)
        from multiplying into hundreds of near-useless extra calls per
        collect() cycle.

        Paginates fully rather than a single per_page page: a single busy
        repo can queue more runs than one page during a real backlog (a
        GitHub Actions incident, a capacity crunch), and this list backs
        every "queued now"/"in progress now" stat panel -- silently
        capping it at one page's worth understates the real number
        exactly when an accurate one matters most.

        Returns (runs, complete). complete is False if any page fetch
        failed partway through a repo's pagination -- runs is then a
        partial list that undercounts the real total, not "there
        genuinely are only this many". Callers deriving counts from this
        list (e.g. the by-category queued/in-progress gauges) should skip
        publishing on an incomplete fetch rather than publish an
        undercount that looks like a real drop.
        """
        runs = []
        complete = True
        for status in ("queued", "in_progress"):
            url = (
                f"{API_URL}/repos/{ORG}/{repo}/actions/runs"
                f"?status={status}&per_page=100"
            )
            while url:
                resp = self._get(url)
                if not resp.ok:
                    complete = False
                    break
                for run in resp.json().get("workflow_runs", []):
                    if run.get("event") in WorkflowExporter.IGNORED_EVENTS:
                        continue
                    record = self._make_job_record(run, repo)
                    if status == "in_progress":
                        jobs = self._fetch_run_jobs(repo, run["id"])
                        if jobs:
                            record["runner_name"] = self._extract_runner_names(jobs)
                    runs.append(record)
                url = resp.links.get("next", {}).get("url")
        return runs, complete

    def _recent_completed(self, repo):
        """Fetch recently completed runs to detect new completions.

        Fetches 50 per page and relies on _needs_upsert() to skip
        already-processed ones. Correctly, cheaply catches short-lived runs
        (created and completed within roughly one poll interval), which
        always sort near the top regardless of ordering.

        Does NOT reliably catch long-running runs on a busy repo: this
        endpoint's default sort is by created_at descending (confirmed
        live against the real API -- this docstring previously assumed
        updated_at descending, which is not what GitHub actually returns),
        so a run's position here is fixed by when it STARTED, not when it
        finished. A run that takes hours to complete sinks in this
        ordering for its entire runtime, and on a repo producing 50+ other
        completions in that window (confirmed live on osac-test-infra),
        it can be pushed past this single, unpaginated 50-item page before
        ever being looked at again -- silently and permanently, not just
        delayed, since nothing before it in created_at order will ever
        rank lower. See collect()'s dropped-from-active-list catch-up for
        the mechanism that actually covers long-running runs; this
        function alone is not sufficient for them.
        """
        resp = self._get(
            f"{API_URL}/repos/{ORG}/{repo}/actions/runs"
            f"?status=completed&per_page=50"
        )
        if not resp.ok:
            return []
        return resp.json().get("workflow_runs", [])

    def _fetch_recent_history(self, repo):
        """Fetch the most recent completed runs for initial history load.

        Fetches 100 most recent completed runs per repo to seed Prometheus
        counters and the JSON API with a meaningful baseline.
        """
        resp = self._get(
            f"{API_URL}/repos/{ORG}/{repo}/actions/runs"
            f"?status=completed&per_page=100"
        )
        if not resp.ok:
            return []
        return resp.json().get("workflow_runs", [])

    def _fetch_run_jobs(self, repo, run_id):
        """Fetch job-level details for a run.

        Returns the raw jobs list from the GitHub API, [] if the run
        genuinely has no jobs, or None if the fetch itself failed. Callers
        must treat None as "try again later" -- persisting a record built
        from a failed fetch would look like a complete, jobless run
        forever, since a run already stored looks "seen" to _needs_upsert.
        """
        resp = self._get(
            f"{API_URL}/repos/{ORG}/{repo}/actions/runs/{run_id}/jobs"
            f"?filter=latest&per_page=30"
        )
        if not resp.ok:
            return None
        return resp.json().get("jobs", [])

    def _extract_failed_steps(self, jobs):
        """Extract failed step info from a list of job objects.

        Returns: [{"display": "job → step", "step": "step_name"}, ...]
        """
        failed_steps = []
        for job in jobs:
            if self._is_gate_job(job.get("name")):
                continue
            if job.get("conclusion") != "failure":
                continue
            for step in job.get("steps", []):
                if step.get("conclusion") == "failure":
                    failed_steps.append({
                        "display": f"{job['name']} → {step['name']}",
                        "step": step["name"],
                    })
        return failed_steps

    def _extract_step_durations(self, jobs):
        """Extract step durations from a list of job objects.

        Returns: [{"name": "step_name", "duration_s": N}, ...]
        Only includes completed steps with valid timestamps.
        """
        steps = []
        for job in jobs:
            if self._is_gate_job(job.get("name")):
                continue
            if job.get("conclusion") not in ("success", "failure"):
                continue
            for step in job.get("steps", []):
                if step.get("status") != "completed":
                    continue
                started = step.get("started_at", "")
                completed = step.get("completed_at", "")
                if not started or not completed:
                    continue
                try:
                    t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    t1 = datetime.fromisoformat(completed.replace("Z", "+00:00"))
                    dur = max(0, (t1 - t0).total_seconds())
                    steps.append({"name": step["name"], "duration_s": round(dur)})
                except (ValueError, TypeError):
                    pass
        return steps

    @staticmethod
    def _extract_runner_names(jobs):
        """Extract the machine(s) a run's job(s) executed on.

        Usually one job per run, but matrix strategies can have several --
        returns a sorted, deduplicated, comma-joined string rather than a
        single value so that case is never silently collapsed to one name.

        Excludes GitHub-hosted runners ("GitHub Actions <id>", GitHub's
        default display name for its own hosted runners) -- those aren't
        our fleet, carry no useful "which machine" signal, and are
        ephemeral/unstable identifiers anyway.
        """
        names = sorted({
            j["runner_name"] for j in jobs
            if j.get("runner_name") and not j["runner_name"].startswith("GitHub Actions ")
        })
        return ", ".join(names)

    def _needs_upsert(self, run):
        """Whether `run` (a raw GitHub API run object) is worth fetching
        job-level details for and upserting -- true if it's not stored at
        all yet, or if the incoming run_attempt is newer than what's
        stored (see _upsert_job). False means the stored row is already
        at least as current as this run, so the caller can skip the extra
        _fetch_run_jobs API call entirely.
        """
        with self._db() as conn:
            row = conn.execute(
                "SELECT run_attempt FROM jobs WHERE id = ?", (run["id"],)
            ).fetchone()
        return row is None or run.get("run_attempt", 1) > row[0]

    # GitHub's workflow-runs list endpoint stops paginating around 1000
    # results regardless of total_count -- a documented REST API list
    # limit, not specific to this endpoint. A single since-only query for a
    # high-volume repo silently returns only its most recent ~1000 runs,
    # truncating the requested window without any error. Stay well under
    # that with a safety margin before bisecting the date range.
    BACKFILL_SAFE_RESULT_LIMIT = 900

    def _backfill_page(self, repo, url):
        """Paginate a single (already narrow enough) runs-list query,
        upserting every run. Returns (seen, new).
        """
        seen = new = 0
        while url:
            resp = self._get(url)

            if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
                reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset - time.time(), 0) + 5
                logger.warning("Rate limit exhausted, sleeping %.0fs until reset", wait)
                time.sleep(wait)
                continue  # retry the same url

            if not resp.ok:
                logger.error("Backfill fetch failed for %s: %s %s",
                             repo, resp.status_code, resp.text[:200])
                break

            for run in resp.json().get("workflow_runs", []):
                if run.get("event") in WorkflowExporter.IGNORED_EVENTS:
                    continue
                run_id = run["id"]
                seen += 1
                if not self._needs_upsert(run):
                    continue

                record = self._make_job_record(run, repo)
                conclusion = run.get("conclusion") or "unknown"
                jobs = self._fetch_run_jobs(repo, run_id)
                if jobs is None:
                    logger.warning(
                        "Skipping run %s (%s): job-details fetch failed, will retry next pass",
                        run_id, repo,
                    )
                    continue
                if jobs:
                    record["runner_name"] = self._extract_runner_names(jobs)
                    record["steps"] = self._extract_step_durations(jobs)
                self._apply_ai_status(record, jobs)
                if conclusion == "failure":
                    # jobs may be [] (fetch succeeded, zero job entries) --
                    # classify anyway rather than leaving failure_reason
                    # unset; _classify_failure_reason treats "no per-step
                    # detail at all" as infra, which is correct here too.
                    failed = self._extract_failed_steps(jobs or [])
                    record["failure_reason"] = self._classify_failure_reason(record.get("category", ""), failed, jobs)
                    if failed:
                        record["failed_step"] = "; ".join(
                            f["display"] for f in failed
                        )

                if self._upsert_job(record):
                    new += 1

            url = resp.links.get("next", {}).get("url")

        return seen, new

    def _backfill_range(self, repo, since_dt, until_dt):
        """Fetch completed runs for repo in [since_dt, until_dt), recursing
        by bisecting the date range whenever a query's total_count would
        need to paginate past GitHub's ~1000-result list cap. Returns
        (seen, new).
        """
        since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        until_str = until_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (f"{API_URL}/repos/{ORG}/{repo}/actions/runs"
               f"?status=completed&per_page=100&created={since_str}..{until_str}")

        probe = self._get(url)
        if probe.status_code == 403 and probe.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(probe.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = max(reset - time.time(), 0) + 5
            logger.warning("Rate limit exhausted, sleeping %.0fs until reset", wait)
            time.sleep(wait)
            return self._backfill_range(repo, since_dt, until_dt)  # retry whole range

        if not probe.ok:
            logger.error("Backfill probe failed for %s [%s..%s]: %s %s",
                         repo, since_str, until_str, probe.status_code, probe.text[:200])
            return 0, 0

        total_count = probe.json().get("total_count", 0)
        if total_count > self.BACKFILL_SAFE_RESULT_LIMIT and (until_dt - since_dt) > timedelta(hours=1):
            mid = since_dt + (until_dt - since_dt) / 2
            logger.info("%s [%s..%s]: %d runs, bisecting at %s",
                        repo, since_str, until_str, total_count, mid.isoformat())
            seen1, new1 = self._backfill_range(repo, since_dt, mid)
            seen2, new2 = self._backfill_range(repo, mid, until_dt)
            return seen1 + seen2, new1 + new2

        # Narrow enough range -- paginate normally, reusing the probe
        # response as the first page instead of refetching it.
        seen = new = 0
        for run in probe.json().get("workflow_runs", []):
            if run.get("event") in WorkflowExporter.IGNORED_EVENTS:
                continue
            run_id = run["id"]
            seen += 1
            if not self._needs_upsert(run):
                continue
            record = self._make_job_record(run, repo)
            conclusion = run.get("conclusion") or "unknown"
            jobs = self._fetch_run_jobs(repo, run_id)
            if jobs is None:
                logger.warning(
                    "Skipping run %s (%s): job-details fetch failed, will retry next pass",
                    run_id, repo,
                )
                continue
            if jobs:
                record["runner_name"] = self._extract_runner_names(jobs)
                record["steps"] = self._extract_step_durations(jobs)
            self._apply_ai_status(record, jobs)
            if conclusion == "failure":
                # jobs may be [] (fetch succeeded, zero job entries) --
                # classify anyway rather than leaving failure_reason unset;
                # _classify_failure_reason treats "no per-step detail at
                # all" as infra, which is correct here too.
                failed = self._extract_failed_steps(jobs or [])
                record["failure_reason"] = self._classify_failure_reason(record.get("category", ""), failed, jobs)
                if failed:
                    record["failed_step"] = "; ".join(f["display"] for f in failed)
            if self._upsert_job(record):
                new += 1

        next_url = probe.links.get("next", {}).get("url")
        if next_url:
            more_seen, more_new = self._backfill_page(repo, next_url)
            seen += more_seen
            new += more_new

        return seen, new

    def backfill(self, days):
        """One-off backfill: fetch completed runs from the last `days` days
        across all monitored repos and upsert them into the DB, bisecting
        each repo's date range as needed to stay under GitHub's ~1000-
        result list pagination cap.

        Safe to re-run any number of times -- _upsert_job's INSERT OR
        IGNORE against the id primary key means already-stored runs are
        silently skipped, never duplicated. Intended to be invoked directly
        (BACKFILL_DAYS env var, see main()), not during normal polling.
        """
        since_dt = datetime.now(timezone.utc) - timedelta(days=days)
        until_dt = datetime.now(timezone.utc)
        repos = REPOS_FILTER if REPOS_FILTER else self.get_active_repos()
        self._refresh_pr_map(repos)
        total_seen = total_new = 0

        for repo in repos:
            logger.info("Backfilling %s (created >= %s)...", repo, since_dt.isoformat())
            repo_seen, repo_new = self._backfill_range(repo, since_dt, until_dt)
            logger.info("%s: %d runs seen, %d newly inserted", repo, repo_seen, repo_new)
            total_seen += repo_seen
            total_new += repo_new

        logger.info("Backfill complete: %d runs seen total, %d newly inserted", total_seen, total_new)
        return total_seen, total_new

    def initial_load(self):
        """Seed history from the GitHub API on a genuinely fresh DB.

        The DB persists across restarts, and _migrate_json_cache_if_needed
        (run during __init__) already imports any legacy JSON cache -- so
        this only hits the GitHub API when there's truly no history yet
        (first-ever deploy).

        Does NOT increment Prometheus counters — those are only incremented
        for genuinely new completions detected during regular polling. This
        prevents increase() from showing inflated numbers after every
        restart.
        """
        with self._db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        if count > 0:
            logger.info("DB already has %d jobs, skipping initial API fetch", count)
            return

        repos = REPOS_FILTER if REPOS_FILTER else self.get_active_repos()
        self._refresh_pr_map(repos)
        loaded = 0
        for repo in repos:
            try:
                for run in self._fetch_recent_history(repo):
                    if run.get("event") in WorkflowExporter.IGNORED_EVENTS:
                        continue
                    run_id = run["id"]
                    record = self._make_job_record(run, repo)
                    conclusion = run.get("conclusion") or "unknown"

                    # Fetch job-level data for failed steps and step durations
                    jobs = self._fetch_run_jobs(repo, run_id)
                    if jobs is None:
                        logger.warning(
                            "Skipping run %s (%s): job-details fetch failed, will retry next pass",
                            run_id, repo,
                        )
                        continue
                    if jobs:
                        record["runner_name"] = self._extract_runner_names(jobs)
                        record["steps"] = self._extract_step_durations(jobs)
                    self._apply_ai_status(record, jobs)
                    if conclusion == "failure":
                        # jobs may be [] (fetch succeeded, zero job entries)
                        # -- classify anyway rather than leaving
                        # failure_reason unset; _classify_failure_reason
                        # treats "no per-step detail at all" as infra,
                        # which is correct here too.
                        failed = self._extract_failed_steps(jobs or [])
                        record["failure_reason"] = self._classify_failure_reason(record.get("category", ""), failed, jobs)
                        if failed:
                            record["failed_step"] = "; ".join(
                                f["display"] for f in failed
                            )

                    if self._upsert_job(record):
                        loaded += 1
            except Exception:
                logger.exception("Error loading history for %s", repo)

        logger.info("Initial load: %d jobs seeded", loaded)

    def _process_completed_run(self, repo, run):
        """Fetch job-level detail for one completed run and upsert it,
        incrementing the completed/duration/failed-step metrics on
        success. Returns True if the row was actually (newly) upserted.

        Shared by collect()'s two independent ways of finding a candidate
        run -- _recent_completed's polling loop, and the dropped-from-
        active-list catch-up below -- so both go through identical
        processing. Safe to call twice for the same run (e.g. if both
        paths happen to surface it in the same cycle): _needs_upsert/
        _upsert_job are the actual dedup point, keyed on run id + attempt.
        """
        if run.get("event") in WorkflowExporter.IGNORED_EVENTS:
            return False
        run_id = run["id"]
        if not self._needs_upsert(run):
            return False

        conclusion = run.get("conclusion") or "unknown"
        workflow_name = run.get("name", "unknown")

        record = self._make_job_record(run, repo)
        jobs = self._fetch_run_jobs(repo, run_id)
        if jobs is None:
            logger.warning(
                "Skipping run %s (%s): job-details fetch failed, will retry next pass",
                run_id, repo,
            )
            return False
        failed = []
        if jobs:
            record["runner_name"] = self._extract_runner_names(jobs)
            record["steps"] = self._extract_step_durations(jobs)
        self._apply_ai_status(record, jobs)
        if conclusion == "failure":
            # jobs may be [] (fetch succeeded, zero job entries)
            # -- classify anyway rather than leaving
            # failure_reason unset; _classify_failure_reason
            # treats "no per-step detail at all" as infra,
            # which is correct here too.
            failed = self._extract_failed_steps(jobs or [])
            record["failure_reason"] = self._classify_failure_reason(record.get("category", ""), failed, jobs)
            if failed:
                record["failed_step"] = "; ".join(
                    f["display"] for f in failed
                )

        if not self._upsert_job(record):
            return False  # stored row's run_attempt was already current

        completed_runs.labels(
            org=ORG, repo=repo, workflow=workflow_name, conclusion=conclusion
        ).inc()

        started = run.get("run_started_at")
        ended = run.get("updated_at")
        if started and ended:
            t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(ended.replace("Z", "+00:00"))
            dur = (t1 - t0).total_seconds()
            if dur > 0:
                run_duration.labels(
                    org=ORG, repo=repo, conclusion=conclusion
                ).observe(dur)

        for f in failed:
            failed_step_total.labels(
                org=ORG, workflow=workflow_name, step=f["step"]
            ).inc()

        return True

    def collect(self):
        self._prune_jobs()
        self._prune_pr_merges()
        repos = REPOS_FILTER if REPOS_FILTER else self.get_active_repos()
        self._refresh_pr_map(repos)
        if not self._pr_backfill_done:
            self._backfill_missing_pr_data()
            # Must run after the above: it depends on merge_group jobs'
            # pr_display already being resolved.
            self._backfill_queue_data_if_needed()
            self._pr_backfill_done = True
        tot_queued = 0
        tot_in_progress = 0
        current_active = []
        # False if any repo's active-runs fetch was partial/failed this
        # cycle -- gates the by-category gauge update below (see
        # _fetch_active_runs' docstring): publishing counts derived from
        # an incomplete current_active would look like a real drop in
        # queued/in-progress e2e work rather than "we couldn't fetch it".
        active_runs_complete = True

        # Snapshot before this cycle's active-runs fetch overwrites it --
        # see the dropped-from-active-list catch-up after the main loop
        # below for why. {run_id: repo}; a run id is globally unique
        # across repos so this can't collide.
        with self._lock:
            previous_active_by_id = {r["id"]: r["repo"] for r in self.active_runs}

        for repo in repos:
            try:
                q = self._run_count(repo, "queued")
                ip = self._run_count(repo, "in_progress")

                queued_runs.labels(org=ORG, repo=repo).set(q)
                in_progress_runs.labels(org=ORG, repo=repo).set(ip)
                tot_queued += q
                tot_in_progress += ip

                # Collect active (queued/in_progress) runs for the active list
                if q > 0 or ip > 0:
                    try:
                        active_runs, was_complete = self._fetch_active_runs(repo)
                        current_active.extend(active_runs)
                        if not was_complete:
                            active_runs_complete = False
                    except Exception:
                        logger.exception("Error fetching active runs for %s", repo)
                        active_runs_complete = False

                # Track newly completed runs. _process_completed_run checks
                # for an existing up-to-date row (cheaply) before spending
                # the extra _fetch_run_jobs API call, so already-recorded
                # (and not-newer-attempt) runs don't burn rate-limit budget.
                # This alone only reliably catches short-lived runs -- see
                # _recent_completed's docstring and the dropped-from-active
                # catch-up after this loop for long-running ones.
                for run in self._recent_completed(repo):
                    self._process_completed_run(repo, run)

            except Exception:
                logger.exception("Error collecting metrics for %s", repo)

        queued_total.labels(org=ORG).set(tot_queued)
        in_progress_total.labels(org=ORG).set(tot_in_progress)

        # By-category breakdown of the same org-wide totals above -- e2e
        # runs on self-hosted runners, a completely different resource
        # pool/queue from the GitHub-hosted runners lint/build/automation
        # workflows use, so the combined total can look calm while e2e's
        # own queue is backed up (or vice versa). Reuses current_active
        # (already fully fetched above for the active-run list) rather
        # than making extra API calls -- each record already carries its
        # category from _make_job_record.
        #
        # Skipped entirely when active_runs_complete is False: unlike
        # tot_queued/tot_in_progress above (each repo's own reliable
        # total_count from a single API call, unaffected by pagination
        # failures), these gauges are derived from current_active itself,
        # so a partial fetch would publish an undercount that looks like a
        # real drop in queued/in-progress work rather than "we couldn't
        # fetch it this cycle" -- leaving the previous values in place
        # (Gauges hold their last value until explicitly set) is more
        # honest than overwriting them with a known-wrong number.
        if active_runs_complete:
            category_queued = {}
            category_in_progress = {}
            for run in current_active:
                cat = run.get("category", "ci")
                if run.get("status") == "queued":
                    category_queued[cat] = category_queued.get(cat, 0) + 1
                elif run.get("status") == "in_progress":
                    category_in_progress[cat] = category_in_progress.get(cat, 0) + 1
            # Explicitly zero every known category every cycle -- a Gauge
            # holds its last value forever otherwise, so a category that
            # drops to zero active runs would otherwise show a stale
            # nonzero count rather than actually reaching zero.
            for cat in set(WorkflowExporter.WORKFLOW_CATEGORIES.keys()) | {"ci"}:
                queued_by_category.labels(org=ORG, category=cat).set(category_queued.get(cat, 0))
                in_progress_by_category.labels(org=ORG, category=cat).set(category_in_progress.get(cat, 0))
        else:
            logger.warning(
                "Active-runs fetch incomplete this cycle -- skipping "
                "by-category queued/in-progress gauge update, keeping "
                "previous values"
            )

        # Catch-up for runs that dropped out of the active list since last
        # cycle -- i.e. they must have finished (or been cancelled) in the
        # meantime. This is the actual fix for long-running runs, which
        # _recent_completed alone cannot reliably catch on a busy repo (see
        # its docstring): rather than hoping a completed run resurfaces in
        # that noisy, unpaginated top-50 feed before it's pushed out, fetch
        # each one directly by the id+repo we already know from tracking it
        # as active. Confirmed live: two ~2.5h E2E CaaS runs on
        # osac-test-infra were silently and permanently dropped this way --
        # _process_completed_run is idempotent, so any overlap with
        # _recent_completed above is harmless.
        #
        # Gated on active_runs_complete for the same reason the by-category
        # gauges are: if this cycle's active-runs fetch was itself partial
        # (an API error mid-pagination, not a genuine drop), a run missing
        # from current_active could just be a fetch gap, not a real
        # completion -- treating that as "finished" would risk recording a
        # wrong conclusion (or none) for a run that's actually still going.
        # Skipping the whole catch-up this cycle just retries next cycle,
        # same as the gauge update's own skip does.
        if active_runs_complete:
            still_active_ids = {r["id"] for r in current_active}
            for run_id, dropped_repo in previous_active_by_id.items():
                if run_id in still_active_ids:
                    continue
                try:
                    resp = self._get(
                        f"{API_URL}/repos/{ORG}/{dropped_repo}/actions/runs/{run_id}"
                    )
                    if not resp.ok:
                        continue
                    run_json = resp.json()
                    if run_json.get("status") != "completed":
                        # Dropping out of current_active doesn't always mean
                        # the run finished -- _run_count silently returns 0
                        # on a transient API failure (it doesn't flip
                        # active_runs_complete), which skips
                        # _fetch_active_runs for that repo entirely this
                        # cycle even though the run is still genuinely
                        # queued/in_progress. Calling _process_completed_run
                        # on a non-completed run would upsert a premature
                        # row with an empty conclusion under this run's
                        # current run_attempt -- and since _needs_upsert
                        # only compares run_attempt, the real completion
                        # later (same attempt number) would then never
                        # overwrite it, permanently freezing the row on
                        # "in_progress". Re-add it to this cycle's active
                        # list instead, so it's retained for next cycle's
                        # diff and this catch-up gets another chance once
                        # it's actually done.
                        current_active.append(
                            self._make_job_record(run_json, dropped_repo)
                        )
                        continue
                    self._process_completed_run(dropped_repo, run_json)
                except Exception:
                    logger.exception(
                        "Error catching up dropped-active run %s (%s)",
                        run_id, dropped_repo,
                    )

        with self._lock:
            self.active_runs = current_active

        with self._db() as conn:
            total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

        logger.info(
            "Collected: repos=%d queued=%d in_progress=%d history=%d",
            len(repos),
            tot_queued,
            tot_in_progress,
            total_jobs,
        )

    # Maps job_type filter values to GitHub Actions event names
    JOB_TYPE_EVENTS = {
        "periodic":    {"schedule"},
        "presubmit":   {"pull_request"},
        "manual":      {"workflow_dispatch"},
        "merge_queue": {"merge_group"},
    }

    def _parse_grafana_param(self, params, key):
        """Parse a Grafana template variable query param.

        Returns the cleaned value, or None if the value is empty, "All",
        contains unresolved template syntax like "${var}", or has
        trailing colons from Grafana variable quirks.
        """
        raw = params.get(key, [None])[0]
        if not raw:
            return None
        cleaned = raw.strip().rstrip(":").strip()
        if (cleaned.lower() == "all"
                or cleaned == ""
                or "${" in cleaned):
            return None
        return cleaned

    @staticmethod
    def _parse_limit(params, default=200):
        """Safely parse the `limit` query param.

        Falls back to `default` on anything non-integer (a malformed
        client request shouldn't produce a 500 from an uncaught
        ValueError), and clamps the result to [1, JOBS_HISTORY_MAX_COUNT]
        -- a negative value would otherwise reach SQLite's LIMIT clause,
        where a negative LIMIT means "no limit" rather than "zero rows".
        """
        raw = params.get("limit", [default])[0]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
        return max(1, min(value, JOBS_HISTORY_MAX_COUNT))

    @staticmethod
    def _normalize_iso(dt_str):
        """Parse an ISO-8601 timestamp (any offset/precision) and re-render
        it as "YYYY-MM-DDTHH:MM:SSZ" -- the exact format the GitHub API (and
        thus every stored created_at) always uses. Needed so the SQL range
        comparison below (plain TEXT comparison) orders the same way the
        previous datetime-object comparison did, regardless of the
        precision/format a caller's since/until param happens to use (e.g.
        Grafana's ${__from:date:iso} includes milliseconds).
        """
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _row_to_job(self, row):
        job = dict(row)
        steps_json = job.pop("steps_json", None)
        job["steps"] = json.loads(steps_json) if steps_json else []
        return job

    def get_jobs_json(self, params):
        """Return jobs list as JSON, with optional filters.

        Query params:
          status    - filter by conclusion (success, failure, cancelled)
          repo      - filter by repo name
          workflow  - comma-separated workflow name substrings (case-insensitive)
          limit     - max results (default 200)
          active    - include queued/in_progress runs (true/false)
          since     - ISO 8601 timestamp, only return jobs created at or after
          until     - ISO 8601 timestamp, only return jobs created before
          job_type  - periodic, presubmit, manual, or merge_queue
          failure_reason - infra, test, gate (category "e2e"), or ran,
                      skipped (category "ai") -- see
                      _classify_failure_reason. For e2e's values, implies
                      status=failure and takes precedence over the status
                      param if both are given -- NOT true for ai's ran/
                      skipped, since an ai row is essentially never
                      conclusion=="failure" regardless of whether the
                      Diagnose job's real work ran (see AI_DIAGNOSIS_STEP).
          runner    - filter by machine/runner name (matches a
                      "<runner>-runner-" prefix, case-insensitively;
                      runner_name can hold more than one comma-joined
                      name for matrix-strategy runs)
          search    - free-text substring match across workflow, repo,
                      trigger, branch, PR, display name, and actor
        """
        status_filter = self._parse_grafana_param(params, "status")
        repo_filter = self._parse_grafana_param(params, "repo")
        workflow_filter = params.get("workflow", [None])[0]
        workflow_name_filter = self._parse_grafana_param(params, "workflow_name")
        job_type_filter = self._parse_grafana_param(params, "job_type")
        category_filter = self._parse_grafana_param(params, "category")
        failure_reason_filter = self._parse_grafana_param(params, "failure_reason")
        runner_filter = self._parse_grafana_param(params, "runner")
        search_filter = self._parse_grafana_param(params, "search")
        search_lower = search_filter.lower() if search_filter else None
        limit = self._parse_limit(params)
        include_active = params.get("active", ["false"])[0].lower() == "true"

        # Parse time-range filters — kept as both datetime objects (for the
        # in-memory active_runs filter below) and normalized strings (for
        # the SQL query against the DB-backed history).
        since_str = params.get("since", [None])[0]
        until_str = params.get("until", [None])[0]
        since_dt = until_dt = since_norm = until_norm = None
        if since_str:
            try:
                since_dt = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
                since_norm = self._normalize_iso(since_str)
            except (ValueError, TypeError):
                pass
        if until_str:
            try:
                until_dt = datetime.fromisoformat(until_str.replace("Z", "+00:00"))
                until_norm = self._normalize_iso(until_str)
            except (ValueError, TypeError):
                pass

        # Parse workflow filter into list of lowercase substrings
        wf_filters = []
        if workflow_filter:
            wf_filters = [w.strip().lower() for w in workflow_filter.split(",") if w.strip()]

        # Resolve job_type to a set of allowed event names
        allowed_events = None
        if job_type_filter:
            allowed_events = self.JOB_TYPE_EVENTS.get(job_type_filter.lower())

        # -- DB-backed completed-job history -----------------------------
        where = []
        args = {"limit": limit}
        # Whole-workflow gates (e.g. "label-gate") add no CI-health signal
        # of their own -- excluded from every stats consumer of this
        # method unconditionally, not behind an opt-in filter, since
        # there's no legitimate reason to count them. Job-level gates
        # (e.g. "e2e-caas-gate", one job within a bigger e2e workflow) are
        # handled separately in _extract_failed_steps/_extract_step_durations,
        # since they're not their own row here.
        where.append("LOWER(workflow) NOT LIKE '%' || :gate_suffix")
        args["gate_suffix"] = WorkflowExporter.GATE_NAME_SUFFIX
        # Gate-only failures (failure_reason "gate", see
        # _is_gate_only_failure): a precondition like "e2e-readiness"
        # failed and the real e2e job never ran, so there's no e2e signal
        # here at all -- excluded unconditionally, same as the
        # whole-workflow gate filter above, no opt-in override.
        # NOT `LOWER(failure_reason) != 'gate'` -- failure_reason is NULL
        # for every non-failure row (success/cancelled/skipped never set
        # it, see the ingestion code), and SQL's three-valued NULL logic
        # makes any comparison against NULL evaluate to NULL, not TRUE --
        # a bare `!=` here silently excluded the entire table except the
        # ~7k rows that happen to have a real failure_reason, making every
        # dashboard look like "100% failures" (confirmed live in
        # production immediately after this shipped). COALESCE first so
        # NULL rows compare as '' (never equal to 'gate') and pass through.
        where.append("COALESCE(LOWER(failure_reason), '') != 'gate'")
        if failure_reason_filter:
            # Picking an e2e failure reason (infra/test/gate) implies
            # "only failures" -- the same effect as also picking
            # status=failure. Takes precedence over status_filter since
            # Grafana has no native way to make one dropdown's selection
            # change another dropdown's displayed value, so this is how
            # "selecting Failure Reason behaves as if Status were set to
            # Failed" actually gets enforced. Does NOT apply to ai's
            # ran/skipped values: an "ai" row is essentially never
            # conclusion=="failure" regardless of whether the Diagnose
            # job's real work happened (see AI_DIAGNOSIS_STEP) -- forcing
            # conclusion='failure' here would make selecting Ran/Skipped
            # return zero rows every time.
            if failure_reason_filter.lower() not in ("ran", "skipped"):
                where.append("conclusion = 'failure'")
            where.append("LOWER(failure_reason) = :failure_reason")
            args["failure_reason"] = failure_reason_filter.lower()
        elif status_filter:
            where.append("conclusion = :status")
            args["status"] = status_filter
        if repo_filter:
            where.append("repo = :repo")
            args["repo"] = repo_filter
        if category_filter:
            where.append("LOWER(category) = :category")
            args["category"] = category_filter.lower()
        if runner_filter:
            # Anchored to the "-runner-" boundary rather than a bare
            # substring -- runner_name is a single "box-runner-NN" or a
            # comma-joined list of those (matrix runs). An unanchored
            # substring would let a box filter like "osac-42" also match
            # "osac-421-runner-01"; requiring "-runner-" immediately after
            # the filter value (either at the start of the string, or
            # right after a ", " separator) rules that out while still
            # matching every real box name the Machine dropdown can send.
            where.append(
                "(LOWER(runner_name) LIKE :runner_start OR LOWER(runner_name) LIKE :runner_mid)"
            )
            runner_lower = runner_filter.lower()
            args["runner_start"] = f"{runner_lower}-runner-%"
            args["runner_mid"] = f"%, {runner_lower}-runner-%"
        if wf_filters:
            where.append("(" + " OR ".join(
                f"LOWER(workflow) LIKE :wf{i}" for i in range(len(wf_filters))
            ) + ")")
            for i, f in enumerate(wf_filters):
                args[f"wf{i}"] = f"%{f}%"
        if workflow_name_filter:
            where.append("LOWER(workflow) LIKE :workflow_name")
            args["workflow_name"] = f"%{workflow_name_filter.lower()}%"
        if allowed_events:
            events = sorted(allowed_events)
            where.append("event IN (" + ", ".join(f":ev{i}" for i in range(len(events))) + ")")
            for i, ev in enumerate(events):
                args[f"ev{i}"] = ev
        if since_norm:
            where.append("created_at >= :since")
            args["since"] = since_norm
        if until_norm:
            where.append("created_at < :until")
            args["until"] = until_norm
        if search_lower:
            search_cols = ("workflow", "repo", "trigger", "branch",
                           "pr_display", "display_name", "actor")
            where.append("(" + " OR ".join(
                f"LOWER({c}) LIKE :search" for c in search_cols
            ) + ")")
            args["search"] = f"%{search_lower}%"

        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT :limit"

        with self._db() as conn:
            rows = conn.execute(sql, args).fetchall()
        result = [self._row_to_job(r) for r in rows]

        # -- in-memory active (queued/in_progress) runs -------------------
        # Not persisted in the DB, so filtered in Python against the same
        # criteria. Prepended if requested, not counted against the limit
        # so they never displace history.
        if include_active:
            def matches_active(job):
                # Same unconditional whole-workflow-gate exclusion as the
                # SQL-backed branch above -- without this, a queued/
                # in-progress run of e.g. "label-gate" would appear here
                # (the ?active=true path) even though it's excluded from
                # every completed-history query.
                if WorkflowExporter._is_gate_name(job.get("workflow")):
                    return False
                if failure_reason_filter:
                    # Active (queued/in_progress) jobs are never
                    # conclusion == "failure" yet -- same precedence as
                    # the SQL branch above.
                    return False
                if status_filter and job.get("conclusion") != status_filter:
                    return False
                if repo_filter and job["repo"] != repo_filter:
                    return False
                if category_filter and job.get("category", "").lower() != category_filter.lower():
                    return False
                if wf_filters:
                    wf_name = job.get("workflow", "").lower()
                    if not any(f in wf_name for f in wf_filters):
                        return False
                if (workflow_name_filter
                        and workflow_name_filter.lower()
                        not in job.get("workflow", "").lower()):
                    return False
                if runner_filter:
                    # Same "-runner-" boundary anchoring as the SQL branch
                    # above, checked against each comma-separated token
                    # individually rather than the raw joined string.
                    tokens = [t.strip().lower() for t in job.get("runner_name", "").split(",")]
                    if not any(t.startswith(f"{runner_filter.lower()}-runner-") for t in tokens):
                        return False
                if allowed_events and job.get("event") not in allowed_events:
                    return False
                if search_lower:
                    haystack = " ".join([
                        job.get("workflow", ""),
                        job.get("repo", ""),
                        job.get("trigger", ""),
                        job.get("branch", ""),
                        job.get("pr_display", ""),
                        job.get("display_name", ""),
                        job.get("actor", ""),
                    ]).lower()
                    if search_lower not in haystack:
                        return False
                if since_dt or until_dt:
                    created = job.get("created_at", "")
                    if created:
                        try:
                            job_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                            if since_dt and job_dt < since_dt:
                                return False
                            if until_dt and job_dt >= until_dt:
                                return False
                        except (ValueError, TypeError):
                            pass
                return True

            active_matched = []
            with self._lock:
                for job in self.active_runs:
                    if matches_active(job):
                        active_matched.append(job)
            result = active_matched + result

        return result

    def get_counts_by_workflow_json(self, params):
        """Return per-workflow counts, with the same filters as get_jobs_json.

        Returns: [{"workflow": "...", "success": N, "failure": N,
                   "cancelled": N, "total": N, "success_rate": 0.xx}, ...]
        """
        jobs = self.get_jobs_json(params)
        merge = self._parse_grafana_param(params, "merge_similar")
        use_display = not (merge and merge.lower() in ("true", "yes", "1"))
        by_wf = {}
        for job in jobs:
            wf = (job.get("display_name") or job.get("workflow", "unknown")
                  ) if use_display else job.get("workflow", "unknown")
            c = job.get("conclusion") or job.get("status", "unknown")
            if wf not in by_wf:
                by_wf[wf] = {"workflow": wf, "success": 0, "failure": 0,
                             "cancelled": 0, "total": 0}
            by_wf[wf][c] = by_wf[wf].get(c, 0) + 1
            by_wf[wf]["total"] += 1

        result = []
        for wf_data in by_wf.values():
            decisive = wf_data["success"] + wf_data["failure"]
            wf_data["success_rate"] = (
                round(wf_data["success"] / decisive, 4) if decisive > 0 else 0
            )
            result.append(wf_data)
        return sorted(result, key=lambda x: x["workflow"])

    # Job-count-per-PR buckets for the histogram -- single values up to 5
    # (the bulk of PRs land here), then widening ranges for the long tail
    # of heavy-retry PRs, so a handful of 50-95-job outliers don't force
    # every other bucket down to an unreadable sliver.
    JOBS_PER_PR_BUCKETS = ["1", "2", "3", "4", "5", "6-10", "11-20", "21-50", "51+"]

    @staticmethod
    def _jobs_per_pr_bucket(count):
        if count <= 5:
            return str(count)
        if count <= 10:
            return "6-10"
        if count <= 20:
            return "11-20"
        if count <= 50:
            return "21-50"
        return "51+"

    def get_jobs_per_pr_json(self, params):
        """How many e2e job runs (including retries/re-runs) accumulate on
        each PR, with the same filters as get_jobs_json -- callers
        typically pass category=e2e, since this question ("how many jobs
        did this PR need") is specific to e2e, not lint/build noise.

        Groups by (repo, pr_display); jobs with no pr_display (not
        PR-triggered) are excluded entirely rather than counted as one
        enormous fake "PR".

        Returns: {"total_jobs": N, "distinct_prs": N, "avg_jobs_per_pr": X,
                  "median_jobs_per_pr": X, "max_jobs_per_pr": N,
                  "histogram": [{"bucket": "1", "prs": N, "success": N,
                                 "cancelled": N, "failure": N}, ...]
                  (success/cancelled/failure are job-level counts across
                  every PR in that bucket, not PR-level -- lets a caller
                  render each bucket's bar as a proportional 3-way stack
                  without changing what the bar's total height means),
                  "top_prs": [{"repo":.., "pr":.., "jobs": N}, ...] (top 10),
                  "outcomes": [{"outcome": "Success", "count": N}, ...]}
                  (fixed Success/Cancelled/Failure order, list rather than
                  an object so it plots the same way as histogram --
                  one field selector per row instead of one per column)
        """
        # Force active=false regardless of what the caller passed: an
        # in-progress job has no conclusion yet, so it would inflate
        # total_jobs/distinct_prs/each bucket's "prs" count (which count
        # every matching job) while being silently excluded from the
        # success/cancelled/failure breakdown (which only counts jobs with
        # a matching conclusion) -- breaking the documented invariant that
        # a bucket's outcome segments sum to its bar height.
        completed_params = dict(params)
        completed_params["active"] = ["false"]
        jobs = [j for j in self.get_jobs_json(completed_params) if j.get("pr_display")]
        by_pr = {}
        outcome_counts = {"success": 0, "cancelled": 0, "failure": 0}
        for job in jobs:
            key = (job["repo"], job["pr_display"])
            by_pr.setdefault(key, []).append(job.get("conclusion"))
            c = job.get("conclusion")
            if c in outcome_counts:
                outcome_counts[c] += 1

        outcomes = [
            {"outcome": "Success", "count": outcome_counts["success"]},
            {"outcome": "Cancelled", "count": outcome_counts["cancelled"]},
            {"outcome": "Failure", "count": outcome_counts["failure"]},
        ]

        if not by_pr:
            return {
                "total_jobs": 0, "distinct_prs": 0, "avg_jobs_per_pr": 0,
                "median_jobs_per_pr": 0, "max_jobs_per_pr": 0,
                "histogram": [
                    {"bucket": b, "prs": 0, "success": 0, "cancelled": 0, "failure": 0}
                    for b in self.JOBS_PER_PR_BUCKETS
                ],
                "top_prs": [], "outcomes": outcomes,
            }

        counts = sorted(len(v) for v in by_pr.values())
        n = len(counts)
        median = counts[n // 2] if n % 2 else (counts[n // 2 - 1] + counts[n // 2]) / 2

        bucket_counts = {}
        bucket_outcomes = {b: {"success": 0, "cancelled": 0, "failure": 0} for b in self.JOBS_PER_PR_BUCKETS}
        for conclusions in by_pr.values():
            b = self._jobs_per_pr_bucket(len(conclusions))
            bucket_counts[b] = bucket_counts.get(b, 0) + 1
            for concl in conclusions:
                if concl in bucket_outcomes[b]:
                    bucket_outcomes[b][concl] += 1
        histogram = [
            {
                "bucket": b,
                "prs": bucket_counts.get(b, 0),
                "success": bucket_outcomes[b]["success"],
                "cancelled": bucket_outcomes[b]["cancelled"],
                "failure": bucket_outcomes[b]["failure"],
            }
            for b in self.JOBS_PER_PR_BUCKETS
        ]

        top = sorted(by_pr.items(), key=lambda kv: -len(kv[1]))[:10]
        top_prs = [{"repo": k[0], "pr": k[1], "jobs": len(v)} for k, v in top]

        return {
            "total_jobs": len(jobs),
            "distinct_prs": n,
            "avg_jobs_per_pr": round(sum(counts) / n, 2),
            "median_jobs_per_pr": median,
            "max_jobs_per_pr": max(counts),
            "histogram": histogram,
            "top_prs": top_prs,
            "outcomes": outcomes,
        }

    def get_flake_rate_json(self, params):
        """Retry-to-green flake rate per workflow, with the same filters as
        get_jobs_json (OSAC-2064).

        A stored job row's run_attempt/conclusion always reflect the LATEST
        attempt (see _upsert_job's run_attempt-guarded upsert) -- so
        run_attempt > 1 together with conclusion == "success" means the run
        failed at least once on this exact commit, then passed on a re-run
        with no code change: a flake, not a real fix. A run that never
        succeeded (still failing after N attempts) is a real failure, not a
        flake, and is correctly excluded by only considering
        conclusion == "success" rows here.

        Returns: [{"workflow": "...", "flaky_passes": N,
                   "total_successes": N, "flake_rate": 0.xx}, ...]
        sorted by flake_rate descending (worst offenders first).
        """
        jobs = self.get_jobs_json(params)
        merge = self._parse_grafana_param(params, "merge_similar")
        use_display = not (merge and merge.lower() in ("true", "yes", "1"))
        by_wf = {}
        for job in jobs:
            if job.get("conclusion") != "success":
                continue
            wf = (job.get("display_name") or job.get("workflow", "unknown")
                  ) if use_display else job.get("workflow", "unknown")
            entry = by_wf.setdefault(
                wf, {"workflow": wf, "flaky_passes": 0, "total_successes": 0}
            )
            entry["total_successes"] += 1
            if (job.get("run_attempt") or 1) > 1:
                entry["flaky_passes"] += 1

        result = []
        for entry in by_wf.values():
            entry["flake_rate"] = round(
                entry["flaky_passes"] / entry["total_successes"], 4
            )
            result.append(entry)
        return sorted(result, key=lambda x: x["flake_rate"], reverse=True)

    def get_mttr_json(self, params):
        """Per-workflow + overall MTTR, with the same filters as
        get_jobs_json (OSAC-2064): mean time from a failing run to the next
        run of that same workflow that succeeds.

        Runs sorted chronologically per workflow; a "failure" opens a
        recovery window (if one isn't already open), the next "success"
        closes it and records the elapsed time. Any other conclusion
        (cancelled, etc.) is skipped over -- it neither starts nor ends a
        recovery window, since it's neither a real failure nor a fix.

        Returns: {"by_workflow": [{"workflow": "...", "mttr_seconds": N,
                   "mttr_display": "1h 2m", "num_recoveries": N}, ...]
                   (sorted by mttr_seconds descending, worst first),
                   "overall": {...same shape, no "workflow" key...} | None}
        """
        jobs = self.get_jobs_json(params)
        merge = self._parse_grafana_param(params, "merge_similar")
        use_display = not (merge and merge.lower() in ("true", "yes", "1"))
        by_wf = {}
        for job in jobs:
            wf = (job.get("display_name") or job.get("workflow", "unknown")
                  ) if use_display else job.get("workflow", "unknown")
            by_wf.setdefault(wf, []).append(job)

        result = []
        all_recoveries = []
        for wf, wf_jobs in by_wf.items():
            wf_jobs.sort(key=lambda j: j.get("created_at", ""))
            recoveries = []
            failed_at = None
            for job in wf_jobs:
                c = job.get("conclusion")
                if c == "failure":
                    if failed_at is None:
                        failed_at = job.get("created_at")
                elif c == "success":
                    if failed_at is not None:
                        t0 = datetime.fromisoformat(failed_at.replace("Z", "+00:00"))
                        t1 = datetime.fromisoformat(
                            job["created_at"].replace("Z", "+00:00")
                        )
                        recoveries.append((t1 - t0).total_seconds())
                        failed_at = None
            if recoveries:
                avg = sum(recoveries) / len(recoveries)
                result.append({
                    "workflow": wf,
                    "mttr_seconds": round(avg),
                    "mttr_display": WorkflowExporter._fmt_duration(avg),
                    "num_recoveries": len(recoveries),
                })
                all_recoveries.extend(recoveries)

        overall = None
        if all_recoveries:
            avg = sum(all_recoveries) / len(all_recoveries)
            overall = {
                "mttr_seconds": round(avg),
                "mttr_display": WorkflowExporter._fmt_duration(avg),
                "num_recoveries": len(all_recoveries),
            }
        return {
            "by_workflow": sorted(
                result, key=lambda x: x["mttr_seconds"], reverse=True
            ),
            "overall": overall,
        }

    def get_workflows_json(self, params):
        """Return distinct workflow names from the job history.

        Accepts the same filters (workflow substring, since, until) so the
        dropdown only shows workflows relevant to the current view.

        Returns: [{"workflow": "...", "__text": "...", "__value": "..."}, ...]
        The __text/__value keys are for Grafana variable data source compatibility.
        """
        jobs = self.get_jobs_json(params)
        names = sorted(set(j.get("workflow", "unknown") for j in jobs))
        return [{"workflow": n, "__text": n, "__value": n} for n in names]

    # Below this fraction of the total, a "Most Failing Steps" entry is
    # folded into a single "Other" slice instead of its own -- with dozens
    # of distinct "job -> step" combinations across e2e flavors, a
    # piechart/legend listing every single-digit-count step is unreadable.
    FAILED_STEPS_OTHER_THRESHOLD = 0.10

    def get_failed_steps_json(self, params):
        """Return failure counts by step name for jobs matching the filters.

        Parses the 'failed_step' field (semicolon-separated "job -> step" entries)
        from each matching failed job. Entries below FAILED_STEPS_OTHER_THRESHOLD
        of the total are combined into a single "Other" entry.

        Returns: [{"step": "step_name", "count": N}, ...] sorted by count desc.
        """
        jobs = self.get_jobs_json(params)
        step_counts = {}
        for job in jobs:
            fs = job.get("failed_step", "")
            if not fs:
                continue
            for entry in fs.split("; "):
                entry = entry.strip()
                if entry:
                    step_counts[entry] = step_counts.get(entry, 0) + 1

        total = sum(step_counts.values())
        main = []
        other_count = 0
        for step, count in step_counts.items():
            if total and count / total >= self.FAILED_STEPS_OTHER_THRESHOLD:
                main.append({"step": step, "count": count})
            else:
                other_count += count
        if other_count:
            main.append({"step": "Other", "count": other_count})
        return sorted(main, key=lambda x: -x["count"])

    # Maps event names back to human-readable job type labels
    EVENT_TYPE_LABELS = {
        "schedule": "Periodic",
        "pull_request": "Presubmit",
        "workflow_dispatch": "Manual",
        "merge_group": "Merge Queue",
    }

    def get_avg_duration_by_type_json(self, params):
        """Return average run duration grouped by job type.

        Returns: [{"job_type": "Periodic", "avg_duration_s": N,
                   "avg_duration": "Xm Ys", "count": N}, ...]
        """
        jobs = self.get_jobs_json(params)
        by_type = {}
        for job in jobs:
            event = job.get("event", "unknown")
            label = self.EVENT_TYPE_LABELS.get(event, event)
            dur = job.get("duration_s", 0)
            if dur <= 0:
                continue
            if label not in by_type:
                by_type[label] = {"total_s": 0, "count": 0}
            by_type[label]["total_s"] += dur
            by_type[label]["count"] += 1

        result = []
        for label, data in sorted(by_type.items()):
            avg = round(data["total_s"] / data["count"]) if data["count"] else 0
            result.append({
                "job_type": label,
                "avg_duration_s": avg,
                "avg_duration": self._fmt_duration(avg),
                "count": data["count"],
            })
        return result

    def get_avg_step_duration_json(self, params):
        """Return average duration per step name across matching jobs.

        Returns: [{"step": "step_name", "avg_duration_s": N,
                   "avg_duration": "Xm Ys", "count": N}, ...]
        sorted by typical execution order (average position in the step list).
        """
        jobs = self.get_jobs_json(params)
        by_step = {}
        step_order = {}  # track average position for ordering
        for job in jobs:
            steps = job.get("steps", [])
            for idx, step in enumerate(steps):
                name = step.get("name", "")
                dur = step.get("duration_s", 0)
                if not name or dur <= 0:
                    continue
                if name not in by_step:
                    by_step[name] = {"total_s": 0, "count": 0}
                    step_order[name] = {"total_idx": 0, "count": 0}
                by_step[name]["total_s"] += dur
                by_step[name]["count"] += 1
                step_order[name]["total_idx"] += idx
                step_order[name]["count"] += 1

        result = []
        for name, data in by_step.items():
            avg = round(data["total_s"] / data["count"]) if data["count"] else 0
            if avg < 5:
                continue  # skip trivial steps (< 5s avg)
            avg_idx = (step_order[name]["total_idx"] /
                       step_order[name]["count"]) if step_order[name]["count"] else 0
            result.append({
                "step": name,
                "avg_duration_s": avg,
                "avg_duration": self._fmt_duration(avg),
                "count": data["count"],
                "_order": avg_idx,
            })
        # Sort by execution order, then remove the internal field
        result.sort(key=lambda x: x["_order"])
        for r in result:
            del r["_order"]
        return result

    def get_counts_json(self, params):
        """Return job counts by conclusion, with the same filters as get_jobs_json.

        Returns: {"success": N, "failure": N, "cancelled": N,
                  "queued": N, "in_progress": N, "total": N,
                  "failure_rate": 0.xx, "success_rate": 0.xx,
                  "infra_failure_count": N, "infra_failure_pct": 0.xx,
                  "cache_oldest_at": "2026-..." | None}
        """
        # Reuse the same filtering logic — just count instead of return
        jobs = self.get_jobs_json(params)
        counts = {}
        infra_failure_count = 0
        for job in jobs:
            c = job.get("conclusion") or job.get("status", "unknown")
            counts[c] = counts.get(c, 0) + 1
            # get_jobs_json already excludes failure_reason "gate" rows
            # (see _is_gate_only_failure) -- a gate-only failure has no
            # e2e signal at all, so it's correctly absent from both this
            # count and the failure_count denominator below, not silently
            # folded into "infra".
            if job.get("failure_reason") == "infra":
                infra_failure_count += 1
        total = len(jobs)
        success_count = counts.get("success", 0)
        failure_count = counts.get("failure", 0)
        decisive = success_count + failure_count  # exclude cancelled
        failure_rate = round(failure_count / decisive, 4) if decisive > 0 else 0
        return {
            "success": success_count,
            "failure": failure_count,
            "cancelled": counts.get("cancelled", 0),
            "queued": counts.get("queued", 0),
            "in_progress": counts.get("in_progress", 0),
            "total": total,
            "failure_rate": failure_rate,
            # For a standup-facing "pass rate" stat panel (OSAC-2064) --
            # 1 - failure_rate rather than success_count/decisive so the
            # two rates always sum to exactly 1 (avoids float rounding
            # drift between two separately-rounded fractions).
            "success_rate": round(1 - failure_rate, 4) if decisive > 0 else 0,
            "infra_failure_count": infra_failure_count,
            # Of all failures (not of all runs) -- "what fraction of the
            # failures we did have were CI's fault rather than the
            # product's", distinct from failure_rate itself (which is
            # fraction of all decisive runs that failed at all).
            "infra_failure_pct": round(infra_failure_count / failure_count, 4) if failure_count > 0 else 0,
            # How far back the exporter's in-memory data actually goes,
            # regardless of the query's own filters -- lets the dashboard
            # show "data since: X" instead of implying full coverage of
            # whatever time range is selected (see OSAC-2211).
            "cache_oldest_at": self.get_cache_coverage(),
        }

    def get_presubmit_infra_failures_json(self, params):
        """Break down failed jobs by failure_reason, with the same filters
        as get_jobs_json -- callers typically pass
        job_type=presubmit&category=e2e&since=... to ask "of presubmit e2e
        failures in this window, how many were CI's fault (infra) vs the
        product's (test)?", but any filter combination works.

        infra failures are further broken out by which specific step
        failed (failure_reason alone only says infra/test/unknown, not
        which step) by re-parsing each job's stored `failed_step` text.

        Returns: {"infra_by_step": [{"step": "...", "count": N}, ...]
                  (sorted by count descending), "infra_total": N,
                  "test_total": N, "unattributed_total": N,
                  "total_failures": N}
        """
        params = dict(params)
        params["status"] = ["failure"]
        jobs = self.get_jobs_json(params)

        infra_by_step = {}
        infra_total = test_total = unattributed_total = 0
        for job in jobs:
            reason = job.get("failure_reason") or "unknown"
            if reason == "infra":
                infra_total += 1
                for entry in (job.get("failed_step") or "").split("; "):
                    if not entry:
                        continue
                    parts = entry.split(" → ")
                    step = parts[-1]
                    # Rows stored before the ingestion-time gate-job filter
                    # existed (see _extract_failed_steps) still carry the
                    # gate job's own relay step ("e2e-*-gate → Run echo
                    # ...") alongside the real failing job's entry in the
                    # same failed_step text -- confirmed live, e.g. run
                    # #32186127810's stored text has both "e2e-caas-full-install
                    # / e2e → Build and load component images" AND
                    # "e2e-caas-gate → Run echo ...". failure_reason for
                    # that row is correctly "infra" either way (the real
                    # entry alone decides it), but this per-step breakdown
                    # would otherwise also count the noise entry as if it
                    # were its own distinct infra cause.
                    if len(parts) > 1 and self._is_gate_job(parts[0]):
                        continue
                    if step not in self.TEST_STEPS:
                        infra_by_step[step] = infra_by_step.get(step, 0) + 1
            elif reason == "test":
                test_total += 1
            else:
                unattributed_total += 1

        return {
            "infra_by_step": sorted(
                (
                    {"step": step, "count": count}
                    for step, count in infra_by_step.items()
                ),
                key=lambda x: x["count"],
                reverse=True,
            ),
            "infra_total": infra_total,
            "test_total": test_total,
            "unattributed_total": unattributed_total,
            "total_failures": len(jobs),
        }

    def get_pr_merge_time_json(self, params):
        """PR merge timing/retest stats, filtered by when the PR was
        *merged* (not opened) -- "in the past week" means the merge event
        fell in that window, regardless of how old the PR itself was.

        Query params: since, until (ISO 8601, compared against merged_at),
        repo (optional, exact match).

        Reports two distinct timing metrics, since they answer different
        questions:
        - open-to-merge: full PR lifecycle (create to merge). Skewed by
          PRs that sat stale for a long time before anyone reviewed them.
        - approval-to-merge: first APPROVED review to merge -- "how long
          did it take to land once approved," not muddied by staleness.
          None/excluded for PRs merged without a formal approval (e.g. an
          admin override).

        Also reports avg_retest_count (e2e runs beyond the first, per PR
        -- see _count_e2e_retests).

        Both timing metrics report mean AND median -- a handful of PRs
        approved and then simply never merged for days/weeks (no new
        commits, no blocker, just forgotten) skew the mean heavily; median
        shows the typical case instead.

        Also reports queue-wait stats (entered merge queue -> merged) and
        approval-to-queue stats (first approval -> entered merge queue),
        split out from approval-to-merge for exactly the reason described
        in _upsert_pr_merge/_lookup_queued_at: since the merge queue
        rolled out, approval-to-merge silently includes label-gate and
        batch-wait time that has nothing to do with review speed.
        approval-to-queue is the part that's still a genuine human/process
        signal; queue-wait is the new structural cost. Both are None/"n/a"
        for a window with no merge-queue PRs at all (e.g. before rollout).

        Returns: {"avg_open_to_merge_seconds": N, "avg_open_to_merge_display": "Xh Ym",
                  "median_open_to_merge_seconds": N, "median_open_to_merge_display": "Xh Ym",
                  "avg_approval_to_merge_seconds": N|None, "avg_approval_to_merge_display": "Xh Ym"|"n/a",
                  "median_approval_to_merge_seconds": N|None, "median_approval_to_merge_display": "Xh Ym"|"n/a",
                  "avg_approval_to_queue_seconds": N|None, "avg_approval_to_queue_display": "Xh Ym"|"n/a",
                  "median_approval_to_queue_seconds": N|None, "median_approval_to_queue_display": "Xh Ym"|"n/a",
                  "avg_queue_wait_seconds": N|None, "avg_queue_wait_display": "Xh Ym"|"n/a",
                  "median_queue_wait_seconds": N|None, "median_queue_wait_display": "Xh Ym"|"n/a",
                  "via_merge_queue_count": N,
                  "approved_count": N, "avg_retest_count": N, "count": N,
                  "by_repo": [{"repo":.., ...same fields..}, ...]}
        """
        repo_filter = self._parse_grafana_param(params, "repo")
        since_str = params.get("since", [None])[0]
        until_str = params.get("until", [None])[0]

        where = []
        args = {}
        if repo_filter:
            where.append("repo = :repo")
            args["repo"] = repo_filter
        # Same try/except-around-_normalize_iso convention as get_jobs_json --
        # a malformed since/until shouldn't 500 the endpoint, just be ignored.
        if since_str:
            try:
                args["since"] = self._normalize_iso(since_str)
                where.append("merged_at >= :since")
            except (ValueError, TypeError):
                pass
        if until_str:
            try:
                args["until"] = self._normalize_iso(until_str)
                where.append("merged_at < :until")
            except (ValueError, TypeError):
                pass

        sql = ("SELECT repo, number, title, merge_seconds, approval_to_merge_seconds, retest_count, "
               "queue_wait_seconds, approval_to_queue_seconds, via_merge_queue FROM pr_merges")
        if where:
            sql += " WHERE " + " AND ".join(where)

        with self._db() as conn:
            rows = conn.execute(sql, args).fetchall()

        def avg(values):
            return round(sum(values) / len(values)) if values else None

        def median(values):
            # A handful of PRs approved and then simply forgotten for days
            # or weeks (no new commits, no blocker -- just never merged)
            # skew the mean heavily; median reports the typical case
            # instead of letting a few outliers dominate the headline
            # number. Both are reported since the mean-vs-median gap
            # itself is a useful signal that outliers exist at all.
            return round(statistics.median(values)) if values else None

        def stats(rs):
            open_vals = [r["merge_seconds"] for r in rs]
            approval_vals = [
                r["approval_to_merge_seconds"] for r in rs
                if r["approval_to_merge_seconds"] is not None
            ]
            queue_wait_vals = [
                r["queue_wait_seconds"] for r in rs if r["queue_wait_seconds"] is not None
            ]
            approval_to_queue_vals = [
                r["approval_to_queue_seconds"] for r in rs
                if r["approval_to_queue_seconds"] is not None
            ]
            via_queue_count = sum(1 for r in rs if r["via_merge_queue"])
            retest_vals = [r["retest_count"] or 0 for r in rs]
            avg_open = avg(open_vals) or 0
            avg_approval = avg(approval_vals)
            avg_queue_wait = avg(queue_wait_vals)
            avg_approval_to_queue = avg(approval_to_queue_vals)
            median_open = median(open_vals) or 0
            median_approval = median(approval_vals)
            median_queue_wait = median(queue_wait_vals)
            median_approval_to_queue = median(approval_to_queue_vals)
            return {
                "avg_open_to_merge_seconds": avg_open,
                "avg_open_to_merge_display": self._fmt_duration(avg_open),
                "median_open_to_merge_seconds": median_open,
                "median_open_to_merge_display": self._fmt_duration(median_open),
                "avg_approval_to_merge_seconds": avg_approval,
                "avg_approval_to_merge_display": (
                    self._fmt_duration(avg_approval) if avg_approval is not None else "n/a"
                ),
                "median_approval_to_merge_seconds": median_approval,
                "median_approval_to_merge_display": (
                    self._fmt_duration(median_approval) if median_approval is not None else "n/a"
                ),
                "avg_approval_to_queue_seconds": avg_approval_to_queue,
                "avg_approval_to_queue_display": (
                    self._fmt_duration(avg_approval_to_queue) if avg_approval_to_queue is not None else "n/a"
                ),
                "median_approval_to_queue_seconds": median_approval_to_queue,
                "median_approval_to_queue_display": (
                    self._fmt_duration(median_approval_to_queue) if median_approval_to_queue is not None else "n/a"
                ),
                "avg_queue_wait_seconds": avg_queue_wait,
                "avg_queue_wait_display": (
                    self._fmt_duration(avg_queue_wait) if avg_queue_wait is not None else "n/a"
                ),
                "median_queue_wait_seconds": median_queue_wait,
                "median_queue_wait_display": (
                    self._fmt_duration(median_queue_wait) if median_queue_wait is not None else "n/a"
                ),
                "via_merge_queue_count": via_queue_count,
                "approved_count": len(approval_vals),
                "avg_retest_count": round(sum(retest_vals) / len(retest_vals), 1) if retest_vals else 0,
                "count": len(rs),
            }

        by_repo_rows = {}
        for r in rows:
            by_repo_rows.setdefault(r["repo"], []).append(r)

        result = stats(rows)
        result["by_repo"] = sorted(
            ({"repo": repo, **stats(rs)} for repo, rs in by_repo_rows.items()),
            key=lambda x: x["repo"],
        )

        # Individual PRs approved-then-forgotten for days/weeks are exactly
        # what the median (above) is deliberately insensitive to -- but
        # that doesn't mean they should be invisible. Surfaced separately
        # so a real problem PR doesn't just vanish from the reported
        # numbers. Threshold is relative (3x the window's own median) with
        # an absolute floor so a tight cluster of small values (e.g. all
        # under 10 minutes) doesn't get "outliers" 3x'd into noise.
        approved_rows = [r for r in rows if r["approval_to_merge_seconds"] is not None]
        overall_median_approval = median([r["approval_to_merge_seconds"] for r in approved_rows])
        outliers = []
        # Not `if overall_median_approval:` -- a median of exactly 0
        # (plausible: "mostly-instant approvals, one straggler") is falsy
        # and would silently skip outlier detection for precisely the
        # "mostly fine, one real problem" case this feature targets.
        if overall_median_approval is not None:
            threshold = max(overall_median_approval * 3, 3600)
            outliers = sorted(
                (r for r in approved_rows if r["approval_to_merge_seconds"] > threshold),
                key=lambda r: r["approval_to_merge_seconds"],
                reverse=True,
            )[:5]
        result["approval_outliers"] = [
            {
                "repo": r["repo"],
                "number": r["number"],
                "title": r["title"],
                "approval_to_merge_seconds": r["approval_to_merge_seconds"],
                "approval_to_merge_display": self._fmt_duration(r["approval_to_merge_seconds"]),
            }
            for r in outliers
        ]
        return result


# ---------------------------------------------------------------------------
# Custom HTTP handler: /metrics + /api/jobs
# ---------------------------------------------------------------------------
class ExporterHandler(BaseHTTPRequestHandler):
    exporter = None  # set after instantiation

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/metrics":
            output = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(output)

        elif parsed.path == "/api/jobs":
            params = parse_qs(parsed.query)
            jobs = self.exporter.get_jobs_json(params)
            payload = json.dumps(jobs, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/counts":
            params = parse_qs(parsed.query)
            # Override limit to max so we count all matching jobs
            params["limit"] = [str(JOBS_HISTORY_MAX_COUNT)]
            # Always include active runs in counts so in-progress/queued
            # periodic jobs are reflected in the totals
            params["active"] = ["true"]
            counts = self.exporter.get_counts_json(params)
            payload = json.dumps(counts, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/presubmit-infra-failures":
            params = parse_qs(parsed.query)
            params["limit"] = [str(JOBS_HISTORY_MAX_COUNT)]
            breakdown = self.exporter.get_presubmit_infra_failures_json(params)
            payload = json.dumps(breakdown, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/pr-merge-time":
            params = parse_qs(parsed.query)
            merge_time = self.exporter.get_pr_merge_time_json(params)
            payload = json.dumps(merge_time, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/workflows":
            params = parse_qs(parsed.query)
            params["limit"] = [str(JOBS_HISTORY_MAX_COUNT)]
            workflows = self.exporter.get_workflows_json(params)
            payload = json.dumps(workflows, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/failed-steps":
            params = parse_qs(parsed.query)
            params["limit"] = [str(JOBS_HISTORY_MAX_COUNT)]
            steps = self.exporter.get_failed_steps_json(params)
            payload = json.dumps(steps, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/counts-by-workflow":
            params = parse_qs(parsed.query)
            params["limit"] = [str(JOBS_HISTORY_MAX_COUNT)]
            counts = self.exporter.get_counts_by_workflow_json(params)
            payload = json.dumps(counts, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/jobs-per-pr":
            params = parse_qs(parsed.query)
            params["limit"] = [str(JOBS_HISTORY_MAX_COUNT)]
            data = self.exporter.get_jobs_per_pr_json(params)
            payload = json.dumps(data, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/flake-rate":
            params = parse_qs(parsed.query)
            params["limit"] = [str(JOBS_HISTORY_MAX_COUNT)]
            data = self.exporter.get_flake_rate_json(params)
            payload = json.dumps(data, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/mttr":
            params = parse_qs(parsed.query)
            params["limit"] = [str(JOBS_HISTORY_MAX_COUNT)]
            data = self.exporter.get_mttr_json(params)
            payload = json.dumps(data, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/avg-duration-by-type":
            params = parse_qs(parsed.query)
            params["limit"] = [str(JOBS_HISTORY_MAX_COUNT)]
            data = self.exporter.get_avg_duration_by_type_json(params)
            payload = json.dumps(data, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/avg-step-duration":
            params = parse_qs(parsed.query)
            params["limit"] = [str(JOBS_HISTORY_MAX_COUNT)]
            data = self.exporter.get_avg_step_duration_json(params)
            payload = json.dumps(data, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/active":
            with self.exporter._lock:
                active = list(self.exporter.active_runs)
            payload = json.dumps(active, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/categories":
            categories = list(WorkflowExporter.WORKFLOW_CATEGORIES.keys())
            payload = json.dumps(categories, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        logger.debug("HTTP %s", self.path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not TOKEN:
        logger.error("PRIVATE_GITHUB_TOKEN environment variable is required")
        sys.exit(1)

    exporter = WorkflowExporter()

    # One-off backfill mode: run to completion and exit, instead of serving
    # HTTP and polling forever. Intended for manual invocation (e.g. a
    # throwaway `podman run` against the same DB volume) with a dedicated
    # token, not as part of the long-running service.
    backfill_days = os.getenv("BACKFILL_DAYS")
    if backfill_days:
        exporter.backfill(int(backfill_days))
        return

    ExporterHandler.exporter = exporter

    logger.info("Starting workflow exporter on port %d (poll every %ds)", PORT, POLL_INTERVAL)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), ExporterHandler)
    # Otherwise a client request still in flight when the process is asked
    # to exit would keep it alive -- this is a long-running daemon, not a
    # service with a graceful-drain shutdown path.
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # Load recent job history so the table isn't empty on startup.
    # HTTP server is already running so /metrics and /api/jobs are available
    # (they'll return empty data until this finishes).
    logger.info("Loading recent job history...")
    exporter.initial_load()

    while True:
        try:
            exporter.collect()
        except Exception:
            logger.exception("Collection error")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
