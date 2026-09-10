#!/usr/bin/env python3
"""post-ci-digest.py -- Daily Slack digest of OSAC CI health.

Pulls from the workflow-exporter's JSON API (must run where that's
reachable -- the monitoring-central host, since the exporter binds
127.0.0.1:9103), renders a single designed dashboard-style PNG (matplotlib)
summarizing everything, and posts it to Slack as the entire message (a
short caption plus the image, no separate text digest).

Needs a Slack bot token (chat:write + files:write), not just an incoming
webhook -- webhooks can only post text/Block Kit JSON, they can't upload
files. Uses secret/osac/monitoring/slack-bot (separate app/token from
slack-webhook-url, which Alertmanager and the e2e failure notification use
and which can't upload files either).

Env vars:
  EXPORTER_URL       - base URL of the workflow-exporter (default http://127.0.0.1:9103)
  SLACK_BOT_TOKEN    - Slack bot token (xoxb-...), required unless DRY_RUN
  SLACK_CHANNEL_ID   - Slack channel ID to post to, required unless DRY_RUN
  DRY_RUN            - if "true", save the image to ./ci-digest-preview.png
                        instead of posting to Slack
"""

from __future__ import annotations

import io
import json
import os
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Circle, FancyBboxPatch, Wedge  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402

EXPORTER_URL = os.getenv("EXPORTER_URL", "http://127.0.0.1:9103")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("true", "1", "yes")

# Palette matching the reference dashboard mockup: light cyan-to-blue
# gradient background, white "headline" cards, dark navy "detail" cards
# with a cyan border, red/gray gauges.
COLOR_NAVY = "#0b2545"
COLOR_CARD_DARK = "#0f2f52"
COLOR_CARD_LIGHT = "#f2f9fc"
COLOR_BORDER_CYAN = "#4fc3f7"
COLOR_GREEN = "#2ecc71"
COLOR_YELLOW = "#f1c40f"
COLOR_RED = "#e74c3c"
COLOR_GRAY = "#95a5a6"
COLOR_TEXT_LIGHT = "#eaf6ff"
COLOR_TEXT_MUTED = "#9fb8cc"


def _get(path, **params):
    resp = requests.get(f"{EXPORTER_URL}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def pct(rate):
    return f"{rate * 100:.1f}%" if rate is not None else "n/a"


def status_color(rate):
    """Same thresholds as ci-health.json's Overall Pass Rate panel."""
    if rate is None:
        return COLOR_GRAY
    if rate < 0.5:
        return COLOR_RED
    if rate < 0.8:
        return COLOR_YELLOW
    return COLOR_GREEN


def decisive_rate(counts):
    """/api/counts always returns success_rate=0 (not None) when there are
    zero success+failure jobs in the window (see get_counts_json) -- return
    None in that case so callers can render "no runs" instead of a
    misleading 0.0%."""
    if counts["success"] + counts["failure"] == 0:
        return None
    return counts["success_rate"]


def fetch_periodic_success_rate(since):
    return _get("/api/counts", job_type="periodic", category="e2e", since=_iso(since))


def fetch_infra_failures(since, job_type):
    """The exporter's endpoint isn't actually presubmit-specific -- it
    buckets whatever jobs match the given filters into infra/test/
    unattributed, so this works identically for job_type=periodic."""
    return _get(
        "/api/presubmit-infra-failures",
        job_type=job_type, category="e2e", since=_iso(since),
    )


def fetch_pr_merge_time(since):
    return _get("/api/pr-merge-time", since=_iso(since))


def fetch_overall_flake_rate(since):
    """/api/flake-rate returns per-workflow entries -- aggregate across all
    e2e workflows for a single overall figure, same "sum then divide"
    approach as the exporter's own overall pass-rate calc (avoids drift
    from averaging already-rounded per-workflow rates)."""
    rows = _get("/api/flake-rate", category="e2e", since=_iso(since))
    flaky = sum(r["flaky_passes"] for r in rows)
    total = sum(r["total_successes"] for r in rows)
    return round(flaky / total, 4) if total else None


def fetch_overall_mttr(since):
    data = _get("/api/mttr", category="e2e", since=_iso(since))
    return data.get("overall")


def fetch_top_failing_workflow(since):
    rows = _get("/api/counts-by-workflow", merge_similar="Yes", category="e2e", since=_iso(since))
    failing = [r for r in rows if r.get("failure", 0) > 0]
    failing.sort(key=lambda r: r["failure"], reverse=True)
    return failing[0] if failing else None


def fetch_jobs_per_pr(since):
    return _get("/api/jobs-per-pr", category="e2e", since=_iso(since))


def gather_digest_data(now):
    """Single data-gathering pass, shared by both build_digest_image (PNG
    summary) and build_digest_html (full HTML report) -- each API endpoint
    is fetched exactly once per digest run rather than once per output
    format."""
    h24 = now - timedelta(hours=24)
    h72 = now - timedelta(hours=72)
    d7 = now - timedelta(days=7)

    return {
        "periodic_24h": fetch_periodic_success_rate(h24),
        "periodic_72h": fetch_periodic_success_rate(h72),
        "infra_24h": fetch_infra_failures(h24, "presubmit"),
        "infra_72h": fetch_infra_failures(h72, "presubmit"),
        "periodic_infra_24h": fetch_infra_failures(h24, "periodic"),
        "periodic_infra_72h": fetch_infra_failures(h72, "periodic"),
        "merge_time": fetch_pr_merge_time(d7),
        "merge_time_24h": fetch_pr_merge_time(h24),
        "flake_rate": fetch_overall_flake_rate(d7),
        "mttr": fetch_overall_mttr(d7),
        "top_failing": fetch_top_failing_workflow(h24),
        "jobs_per_pr": fetch_jobs_per_pr(d7),
        "now": now.strftime("%Y-%m-%d %H:%M UTC"),
    }


# -- drawing helpers ----------------------------------------------------


def _gradient_background(fig):
    """Light-cyan-to-navy diagonal gradient spanning the whole figure,
    matching the reference mockup's background."""
    ax = fig.add_axes((0, 0, 1, 1), zorder=-10)
    ax.axis("off")
    top_left = np.array([0xe9, 0xf6, 0xfc])
    bottom_right = np.array([0x14, 0x3d, 0x66])
    n = 256
    grad = np.zeros((n, n, 3))
    for i in range(n):
        for j in range(n):
            t = (i + j) / (2 * n)
            grad[i, j] = top_left * (1 - t) + bottom_right * t
    ax.imshow(grad / 255.0, extent=(0, 1, 0, 1), aspect="auto", origin="upper")
    return ax


def _card(fig, rect, dark):
    """rect = (x, y, w, h) in figure-fraction coordinates."""
    facecolor = COLOR_CARD_DARK if dark else COLOR_CARD_LIGHT
    edgecolor = COLOR_BORDER_CYAN if dark else "#c7e6f5"
    patch = FancyBboxPatch(
        (rect[0], rect[1]), rect[2], rect[3],
        boxstyle="round,pad=0,rounding_size=0.015",
        linewidth=1.6, edgecolor=edgecolor, facecolor=facecolor,
        transform=fig.transFigure, zorder=1,
    )
    fig.add_artist(patch)


def _small_axes(fig, cx, cy, r):
    """A square-in-*pixels* axes centered at (cx, cy) [figure-fraction],
    with data coordinates -1..1 on both axes. fig.transFigure is normalized
    0-1 on both x and y regardless of the figure's actual aspect ratio, so
    a Circle/Wedge drawn directly on it (as the first version of this
    script did) renders as an ellipse on any non-square figure. Isolating
    each icon in its own equal-aspect axes sidesteps that entirely.
    """
    fig_w, fig_h = fig.get_size_inches()
    aspect = fig_h / fig_w
    w = 2 * r
    h = w / aspect
    ax = fig.add_axes((cx - w / 2, cy - h / 2, w, h), zorder=6)
    ax.set_xlim(-1, 1)
    ax.set_ylim(-1, 1)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.patch.set_alpha(0)
    return ax


def _checkmark(fig, x, y, r, color):
    ax = _small_axes(fig, x, y, r)
    ax.add_patch(Circle((0, 0), 0.85, color=color))
    ax.text(0, -0.02, "✓", ha="center", va="center", fontsize=r * 1000,
            color="white", fontweight="bold")


def _gauge(fig, cx, cy, r, infra_frac, has_data):
    """Half-donut gauge (dome shape, opens downward) split into test
    (green, right side) / infra (red, left side) portions by angular
    fraction. Sweeps counterclockwise from the right point through the top
    to the left point -- infra failures are CI's own fault, so they get
    the "alarming" red; test failures are the expected/normal category
    here and get green. Rendered as a single neutral gray dome when
    has_data is False (zero failures in the window) -- otherwise
    infra_frac=0 would render as a "fully green" dome that reads as "100%
    test failures" rather than "no failures at all".
    """
    ax = _small_axes(fig, cx, cy, r)
    if not has_data:
        ax.add_patch(Wedge((0, 0), 0.95, 0, 180, width=0.38,
                            facecolor=COLOR_GRAY, edgecolor="none", linewidth=0))
        return
    test_frac = 1 - infra_frac
    test_deg = 180 * test_frac
    ax.add_patch(Wedge((0, 0), 0.95, 0, test_deg, width=0.38,
                        facecolor=COLOR_GREEN, edgecolor="none", linewidth=0))
    ax.add_patch(Wedge((0, 0), 0.95, test_deg, 180, width=0.38,
                        facecolor=COLOR_RED, edgecolor="none", linewidth=0))


def _text(fig, x, y, s, fontsize, color, weight="normal", ha="left", va="center", style="normal"):
    fig.text(x, y, s, fontsize=fontsize, color=color, fontweight=weight,
              ha=ha, va=va, fontstyle=style, zorder=5)


def _truncate(s, max_len=26):
    """Bounds a label's rendered width regardless of source-data length --
    repo names/durations are drawn manually at a small fixed offset from a
    reference point (not via a mechanism that scales its own reserved
    space to the string, like axes tick labels do), so an unexpectedly
    long string would otherwise run past its card."""
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


# -- image assembly -------------------------------------------------------


def build_digest_image(data):
    periodic_24h = data["periodic_24h"]
    periodic_72h = data["periodic_72h"]
    infra_24h = data["infra_24h"]
    infra_72h = data["infra_72h"]
    periodic_infra_24h = data["periodic_infra_24h"]
    periodic_infra_72h = data["periodic_infra_72h"]
    merge_time = data["merge_time"]
    merge_time_24h = data["merge_time_24h"]
    flake_rate = data["flake_rate"]
    mttr = data["mttr"]
    top_failing = data["top_failing"]
    jobs_per_pr = data["jobs_per_pr"]

    fig = plt.figure(figsize=(14, 8.5), dpi=180)
    _gradient_background(fig)

    _text(fig, 0.03, 0.945, "OSAC CI Daily Digest", 30, COLOR_NAVY, weight="bold")
    _text(fig, 0.03, 0.895, data["now"], 15, "#3a5a78")

    # -- Card A: Periodic E2E Success Rate + Infra vs. Test (top-left, light)
    # Grown taller (and Card C below it shrunk to match) to fit the
    # periodic infra-vs-test breakdown without losing the success-rate
    # percentages -- both are anchored to the top of the card so the
    # extra room lands below the percentage boxes, not squeezed between
    # them and the title.
    a_rect = (0.02, 0.475, 0.47, 0.385)
    _card(fig, a_rect, dark=False)
    _text(fig, a_rect[0] + 0.02, a_rect[1] + a_rect[3] - 0.035,
          "Periodic E2E Success Rate", 17, COLOR_NAVY, weight="bold")

    ch = 0.175
    sub_top = a_rect[1] + a_rect[3] - 0.075
    cy = sub_top - ch
    for i, (label, counts) in enumerate((("Last 24h", periodic_24h), ("Last 72h", periodic_72h))):
        cx = a_rect[0] + 0.025 + i * 0.225
        cw = 0.20
        _card(fig, (cx, cy, cw, ch), dark=False)
        rate = decisive_rate(counts)
        total = counts["success"] + counts["failure"]
        _text(fig, cx + cw / 2, cy + ch - 0.045, label, 13, "#3a5a78", ha="center")
        _text(fig, cx + cw / 2 - 0.02, cy + ch / 2 - 0.01, pct(rate), 24, COLOR_NAVY,
              weight="bold", ha="center")
        _checkmark(fig, cx + cw - 0.035, cy + ch / 2, 0.018, status_color(rate))
        _text(fig, cx + cw / 2, cy + 0.035, f"({counts['success']}/{total})", 12, "#5a7a98", ha="center")

    _text(fig, a_rect[0] + 0.02, cy - 0.028,
          "Periodic Failures — Infra vs. Test", 11.5, COLOR_NAVY, weight="bold")
    for i, (label, window) in enumerate((("24h", periodic_infra_24h), ("72h", periodic_infra_72h))):
        ly = cy - 0.058 - i * 0.032
        infra_n, test_n, tot = window["infra_total"], window["test_total"], window["total_failures"]
        _text(fig, a_rect[0] + 0.03, ly, f"{label}:", 10, "#3a5a78", weight="bold")
        _text(fig, a_rect[0] + 0.10, ly, f"{infra_n} infra", 10,
              COLOR_RED if infra_n else "#8ba3b8", weight="bold")
        _text(fig, a_rect[0] + 0.21, ly, f"{test_n} test", 10,
              COLOR_GREEN if test_n else "#8ba3b8", weight="bold")
        _text(fig, a_rect[0] + 0.31, ly, f"({tot} total)", 9.5, "#5a7a98")

    # -- Card B: Presubmit E2E Failures -- Infra vs. Test (top-right, dark)
    b_rect = (0.51, 0.35, 0.47, 0.51)
    _card(fig, b_rect, dark=True)
    _text(fig, b_rect[0] + 0.02, b_rect[1] + b_rect[3] - 0.04,
          "Presubmit E2E Failures — Infra vs. Test", 16, COLOR_TEXT_LIGHT, weight="bold")
    _text(fig, b_rect[0] + 0.02, b_rect[1] + b_rect[3] - 0.075,
          "Infra = CI's fault (setup/teardown), not the product's", 10.5, COLOR_TEXT_MUTED)

    for i, (label, window) in enumerate((("Last 24h", infra_24h), ("Last 72h", infra_72h))):
        gx = b_rect[0] + 0.14 + i * 0.22
        gy = b_rect[1] + 0.20
        infra_n, test_n, tot = window["infra_total"], window["test_total"], window["total_failures"]
        frac = infra_n / tot if tot else 0
        # Dome opens downward -- title sits above the dome's peak, counts
        # sit below its flat base.
        _text(fig, gx, gy + 0.19, label, 13, COLOR_TEXT_LIGHT, weight="bold", ha="center")
        _gauge(fig, gx, gy, 0.08, frac, has_data=tot > 0)
        _text(fig, gx - 0.085, gy - 0.05, f"{infra_n}\ninfra", 11, COLOR_RED, ha="center", weight="bold")
        _text(fig, gx + 0.085, gy - 0.05, f"{test_n}\ntest", 11, COLOR_GREEN, ha="center", weight="bold")
        _text(fig, gx, gy - 0.14, f"({tot} total)", 10, COLOR_TEXT_MUTED, ha="center")

    # Capped to the top 3 steps (already sorted by count desc) rather than
    # every distinct step: TEST_STEPS classifies infra as "anything that
    # isn't the actual test/install step" (see workflow-exporter.py), so
    # unlike the old fixed INFRA_STEPS allowlist, the number of distinct
    # infra step names here is unbounded -- a wide window can list a dozen
    # different steps, which textwrap alone won't keep from running past
    # this card's remaining height and into the card below it.
    def _step_summary(steps, limit=3):
        shown = ", ".join(f"{s['step']} ({s['count']})" for s in steps[:limit])
        extra = len(steps) - limit
        return shown + (f", +{extra} more" if extra > 0 else "")

    step_lines = []
    s24 = infra_24h["infra_by_step"]
    s72 = infra_72h["infra_by_step"]
    if s24:
        step_lines.append("24h: " + _step_summary(s24))
    if s72:
        step_lines.append("72h: " + _step_summary(s72))
    if step_lines:
        wrapped = "\n".join(
            "\n".join(textwrap.wrap(line, width=78)) for line in step_lines
        )
        # va="top", anchored just below the gauge captions above (gy is the
        # last loop's value, but both iterations share the same gy) --
        # va="center" (the _text default) vertically centers a multi-line
        # block around its anchor, so a tall block grows upward into the
        # gauge captions *and* downward past the card's bottom edge at the
        # same time. Anchoring the top and growing downward only is the
        # predictable direction given how little room is left below.
        _text(fig, b_rect[0] + 0.02, gy - 0.14 - 0.03, wrapped, 9, COLOR_TEXT_MUTED, va="top")

    # -- Card C: Time to Merge -- First Approval (bottom-left, dark) -------
    # Height shrunk to match Card A's growth above (0.02 gap preserved
    # between the two stacked left-column cards).
    c_rect = (0.02, 0.02, 0.47, 0.435)
    _card(fig, c_rect, dark=True)
    _text(fig, c_rect[0] + 0.02, c_rect[1] + c_rect[3] - 0.04,
          "Time to Merge — First Approval (7d)", 14.5, COLOR_TEXT_LIGHT, weight="bold")
    _text(fig, c_rect[0] + 0.02, c_rect[1] + c_rect[3] - 0.075,
          (
              f"Median {merge_time['median_approval_to_merge_display']} · "
              f"{merge_time['approved_count']} of {merge_time['count']} merged PRs "
              "had a human approval"
          ),
          10.5, COLOR_TEXT_LIGHT)
    # 24h vs 7d comparison, in text only (not a second bar chart -- this
    # card's bar chart already fills the available height; the full
    # by-repo 24h/7d breakdown lives in the HTML report instead). Flags a
    # rollout-day shift (e.g. the merge queue adding structural wait) at a
    # glance without needing to open the full report.
    _text(fig, c_rect[0] + 0.02, c_rect[1] + c_rect[3] - 0.105,
          (
              f"24h: {merge_time_24h['median_approval_to_merge_display']} "
              f"({merge_time_24h['approved_count']}/{merge_time_24h['count']}) · "
              f"7d: {merge_time['median_approval_to_merge_display']} "
              f"({merge_time['approved_count']}/{merge_time['count']})"
          ),
          9.5, COLOR_TEXT_MUTED)

    # Median, not mean -- a handful of PRs approved and then simply never
    # merged for days/weeks would otherwise dominate a mean-based bar
    # chart, squashing every normal repo down to an invisible sliver next
    # to the outlier's bar. Those outliers aren't hidden, just moved to
    # their own callout below (see approval_outliers) instead of
    # distorting this comparison.
    approved_repos = [r for r in merge_time["by_repo"] if r["median_approval_to_merge_seconds"] is not None]
    # Reserve extra room at the bottom of the card for the outliers
    # callout + retest count, beyond what the bar chart itself needs.
    reserved_bottom = 0.12
    if approved_repos:
        # Repo names are drawn manually (not via barh's automatic y-tick
        # labels) so their width is fully under control -- relying on
        # tick-label auto-placement let long names like
        # "fulfillment-service" overflow past the card's left edge.
        label_margin = 0.16
        # The axes' pixel height must scale with how many bars there
        # actually are -- a fixed-height axes box with just 1-2 items
        # (common here, since few merges have a human approval on record)
        # gives each bar a huge vertical slot and renders as a big block
        # instead of a thin bar, however small `height=` is set below.
        # Anchored to the same top edge regardless of count, so it doesn't
        # visually float when there are fewer bars.
        max_bars = 5
        # Top reserve only needs to clear the title+subtitle+24h-vs-7d text
        # (which occupy roughly the top 0.135 of the card, up from 0.10
        # before the 24h-vs-7d line was added) -- the original 0.20 left a
        # large dead gap above the bars that this space was never actually
        # spent on.
        full_height = c_rect[3] - 0.135 - reserved_bottom
        n = len(approved_repos)
        bar_area_height = full_height * min(n, max_bars) / max_bars
        chart_ax = fig.add_axes((
            c_rect[0] + label_margin,
            c_rect[1] + reserved_bottom + (full_height - bar_area_height),
            c_rect[2] - label_margin - 0.04,
            bar_area_height,
        ), zorder=2)
        chart_ax.patch.set_alpha(0)
        sorted_repos = sorted(approved_repos, key=lambda r: r["median_approval_to_merge_seconds"])
        hours = [r["median_approval_to_merge_seconds"] / 3600 for r in sorted_repos]
        # A scale of 0 (single repo, or every repo at 0s) would collapse
        # both label offsets below to x=0, drawing the repo name and the
        # value label on top of each other as garbled overlapping text --
        # fall back to a fixed unit scale in that case.
        x_scale = max(hours) if max(hours) > 0 else 1.0
        bar_colors = plt.cm.Blues(np.linspace(0.4, 0.9, len(hours)))
        y_pos = range(len(sorted_repos))
        bars = chart_ax.barh(list(y_pos), hours, height=0.6, color=bar_colors)
        for y, bar, r in zip(y_pos, bars, sorted_repos):
            chart_ax.text(
                bar.get_width() + x_scale * 0.03, y,
                _truncate(f"{r['median_approval_to_merge_display']} ({r['approved_count']}/{r['count']})"),
                va="center", fontsize=9.5, color=COLOR_TEXT_LIGHT,
            )
            # Repo names are drawn in the reserved label_margin, which sits
            # outside this axes' own data range by design -- clip_on=True
            # would clip against the *axes'* bounding box, not the card,
            # hiding these entirely rather than just trimming long ones.
            # Truncating the string instead bounds the render width without
            # relying on matplotlib's clip box lining up with the margin.
            chart_ax.text(
                -x_scale * 0.04, y, _truncate(r["repo"]),
                va="center", ha="right", fontsize=10.5, color=COLOR_TEXT_LIGHT,
            )
        chart_ax.set_xlim(0, x_scale * 1.5)
        chart_ax.set_ylim(-0.6, len(sorted_repos) - 0.4)
        chart_ax.axis("off")
    else:
        _text(fig, c_rect[0] + 0.02, c_rect[1] + c_rect[3] / 2,
              "No merged PRs had a human approval in this window", 11, COLOR_TEXT_MUTED)

    outliers = merge_time.get("approval_outliers", [])
    if outliers:
        shown = ", ".join(f"{o['repo']} #{o['number']} ({o['approval_to_merge_display']})" for o in outliers[:3])
        extra = len(outliers) - 3
        outlier_text = "Outliers (approved, then sat): " + shown + (f", +{extra} more" if extra > 0 else "")
        wrapped = "\n".join(textwrap.wrap(outlier_text, width=72))
        _text(fig, c_rect[0] + 0.02, c_rect[1] + 0.065, wrapped, 9, COLOR_YELLOW)

    _text(fig, c_rect[0] + 0.02, c_rect[1] + 0.03,
          f"Avg retests per PR: {merge_time['avg_retest_count']}", 10.5, COLOR_TEXT_MUTED)

    # -- Card D: Additional Stability Signals (bottom-right, dark) ---------
    d_rect = (0.51, 0.02, 0.47, 0.31)
    _card(fig, d_rect, dark=True)
    _text(fig, d_rect[0] + 0.02, d_rect[1] + d_rect[3] - 0.045,
          "Additional Stability Signals (7d)", 15, COLOR_TEXT_LIGHT, weight="bold")

    flake_str = pct(flake_rate) if flake_rate is not None else "no successes yet"
    mttr_str = mttr["mttr_display"] if mttr else "no recoveries yet"
    # No emoji here -- this is baked into a rasterized PNG (matplotlib's
    # Agg backend uses DejaVu Sans, which has no color-emoji glyphs), not
    # sent as Slack markdown, so it would render as a missing-glyph box.
    top_str = (
        f"{top_failing['workflow']} ({top_failing['failure']})" if top_failing else "None"
    )
    jobs_per_pr_str = (
        f"{jobs_per_pr['avg_jobs_per_pr']} avg / {jobs_per_pr['median_jobs_per_pr']} median"
        if jobs_per_pr["distinct_prs"] else "no e2e PR activity"
    )
    outcome_total = sum(o["count"] for o in jobs_per_pr["outcomes"])
    if outcome_total:
        outcomes_str = " / ".join(
            f"{round(o['count'] / outcome_total * 100)}% {o['outcome'].lower()}"
            for o in jobs_per_pr["outcomes"]
        )
    else:
        outcomes_str = "no e2e job runs"
    # .get(), not direct indexing: the digest and exporter are separate
    # processes/deploys, and these keys only exist on an exporter that's
    # already been upgraded -- a digest run against a not-yet-redeployed
    # exporter should degrade this one line, not crash the whole PNG build.
    queue_wait_str = (
        f"{merge_time.get('median_queue_wait_display', 'n/a')} ({merge_time.get('via_merge_queue_count', 0)} PRs)"
        if merge_time.get("via_merge_queue_count") else "no merge-queue PRs yet"
    )
    lines = [
        ("Flake rate", flake_str),
        ("MTTR", mttr_str),
        ("Top failing workflow (24h)", top_str),
        ("E2E jobs per PR (7d)", jobs_per_pr_str),
        ("E2E outcomes (7d)", outcomes_str),
        ("Median time in merge queue (7d)", queue_wait_str),
    ]
    # Spacing shrunk again (0.048 -> 0.039) and font trimmed (10.5 -> 10)
    # to fit a 6th line (merge-queue wait) in the same card height that
    # used to hold 5 -- see the full HTML report (linked in the caption)
    # for the un-compressed charts.
    for i, (label, value) in enumerate(lines):
        ly = d_rect[1] + d_rect[3] - 0.095 - i * 0.039
        _text(fig, d_rect[0] + 0.03, ly, f"•  {label}:", 10, COLOR_TEXT_MUTED)
        _text(fig, d_rect[0] + d_rect[2] - 0.03, ly, value, 10, COLOR_TEXT_LIGHT,
              weight="bold", ha="right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


# -- HTML full report -------------------------------------------------------

HTML_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "ci-digest-report-template.html")


def build_digest_html(data):
    """Render the full interactive HTML report -- a superset of the PNG
    summary (same underlying data, already gathered once by
    gather_digest_data) with the charts the static PNG can't show
    un-compressed: the full infra-vs-test breakdown, per-repo merge times,
    and the e2e-jobs-per-PR distribution/outcomes. The template is plain
    HTML/CSS/JS with a single JSON placeholder -- no server-side templating
    needed, it just embeds `data` as `const D = {...}` for the page's own
    JS to render client-side, identical to how the design was prototyped
    and approved before being wired in here.
    """
    with open(HTML_TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    # GitHub-derived strings (PR titles, step names, ...) end up in this
    # payload verbatim. The template embeds it unquoted (`const D =
    # __DIGEST_DATA_JSON__;`), so every "<" in the output is necessarily
    # inside a quoted JSON string value -- JSON's own syntax never uses "<"
    # structurally. Escaping every "<" as a JS unicode escape is therefore
    # always safe and, unlike matching "</script" specifically, also covers
    # "<!--", "<script", "<style", and any other tag-like sequence in one
    # pass, regardless of case. U+2028/U+2029 are valid JSON but illegal
    # inside pre-ES2019 JS string/line contexts.
    payload = (
        json.dumps(data, default=str)
        .replace("<", "\\u003c")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )
    return template.replace("__DIGEST_DATA_JSON__", payload).encode("utf-8")


# -- Slack delivery ---------------------------------------------------------


def upload_files(files, caption):
    """Uploads one or more files as a single new channel message (no
    separate text message) via Slack's current (2024+) external-upload
    flow -- the older files.upload endpoint is deprecated. Each file gets
    its own getUploadURLExternal call and binary upload, but all of them
    are attached together in one completeUploadExternal call so they land
    as one message with multiple attachments, not one message per file.
    initial_comment becomes the message text; posting without thread_ts
    shares it to the channel as a top-level message.

    files: [(filename, content_bytes, mimetype, title), ...]
    """
    file_ids = []
    for filename, content, mimetype, title in files:
        resp = requests.post(
            "https://slack.com/api/files.getUploadURLExternal",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            data={"filename": filename, "length": len(content)},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            print(f"Slack API error (files.getUploadURLExternal, {filename}): {data.get('error')}", file=sys.stderr)
            sys.exit(1)
        upload_url, file_id = data["upload_url"], data["file_id"]

        up = requests.post(upload_url, files={"file": (filename, content, mimetype)}, timeout=60)
        up.raise_for_status()
        file_ids.append({"id": file_id, "title": title})

    resp = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={
            "files": file_ids,
            "channel_id": SLACK_CHANNEL_ID,
            "initial_comment": caption,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        print(f"Slack API error (files.completeUploadExternal): {data.get('error')}", file=sys.stderr)
        sys.exit(1)


def main():
    now = datetime.now(timezone.utc)
    data = gather_digest_data(now)
    png_bytes = build_digest_image(data)
    html_bytes = build_digest_html(data)

    # Same payload embedded in the HTML report (see build_digest_html),
    # written out separately so CI can publish it as a workflow artifact --
    # a stable, structured export for consumers (e.g. org-pulse) that don't
    # want to scrape the JSON back out of the HTML.
    json_path = "ci-digest-data.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, default=str, indent=2)
    print(f"Saved digest data: {json_path}", file=sys.stderr)

    if DRY_RUN:
        png_path = "ci-digest-preview.png"
        html_path = "ci-digest-preview.html"
        with open(png_path, "wb") as f:
            f.write(png_bytes)
        with open(html_path, "wb") as f:
            f.write(html_bytes)
        print(f"Saved preview image: {png_path}", file=sys.stderr)
        print(f"Saved preview report: {html_path}", file=sys.stderr)
        return

    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        print("SLACK_BOT_TOKEN and SLACK_CHANNEL_ID are required unless DRY_RUN=true", file=sys.stderr)
        sys.exit(1)

    caption = f"📊 OSAC CI Daily Digest — {now.strftime('%Y-%m-%d %H:%M UTC')} (see the attached report for the full breakdown)"
    upload_files(
        [
            ("ci-digest.png", png_bytes, "image/png", "OSAC CI Daily Digest"),
            ("ci-digest-report.html", html_bytes, "text/html", "OSAC CI Daily Digest — Full Report"),
        ],
        caption,
    )


if __name__ == "__main__":
    main()
