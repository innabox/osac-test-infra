#!/usr/bin/env python3
"""Insert or replace a named, HTML-comment-delimited section within a
sticky PR comment body.

Lets ai-diagnostic-e2e.yml maintain one persistent comment per PR (one
section per E2E suite) instead of posting a new comment on every failure.
Pure text transform -- the calling workflow step handles all GitHub API
calls (finding the existing comment, POSTing/PATCHing the result).

SECTION_KEY is the triggering workflow's own name and is nominally
re-nameable by a PR that edits its own caller workflow's `name:` field --
but it's regex-escaped before use here and only ever affects that PR's own
single sticky comment, so at worst a renamed section label is cosmetically
confusing within that one PR, never a cross-PR or injection issue.
"""
import os
import re
import sys

COMMENT_MARKER = "<!-- osac-ai-diagnostic-comment -->"
# Matches any of our own marker shapes (section delimiters AND the total-
# cost marker) so they can be neutralized if they ever show up *inside*
# section content (see upsert_section below). Diagnosis content can quote
# attacker-influenced log/diff text verbatim (the prompt explicitly
# requires verbatim, not paraphrased, quotes in its Evidence section), so
# a crafted log line shaped like a real "<!-- osac-ai-total-cost:... -->"
# marker is a realistic injection, not just a theoretical one: strip_total_
# block's TOTAL_MARKER_RE uses re.search, which takes the LEFTMOST match --
# an unescaped fake marker earlier in the body would be found instead of
# the real trailing one, both truncating away everything after it
# (including the rest of that section and any later sections) and feeding
# attacker-chosen numbers into the "existing total" the next diagnosis
# builds on.
_MARKER_RE = re.compile(r"<!--\s*(?:/?section:|osac-ai-total-cost:).*?-->", re.DOTALL)

# The running-total block is always the last thing in the body (never
# section-scoped, since it tracks spend across every suite AND every
# repeated failure over the PR's whole lifetime, not just one section) --
# matching from the marker to the end of the string, rather than needing
# its own closing delimiter, is what lets it always be "whatever's last"
# regardless of how many sections come before it.
TOTAL_MARKER_RE = re.compile(
    r"\n*<!-- osac-ai-total-cost:(?P<cost>[0-9.eE+-]+):(?P<input>\d+):(?P<output>\d+):(?P<count>\d+) -->.*\Z",
    re.DOTALL,
)


def strip_total_block(body):
    """Remove the trailing running-total block (if present), returning
    (body_without_it, totals_dict_or_None). Called before upsert_section
    so a brand new section is appended where the total used to be, and the
    (possibly updated) total is re-appended after that -- it always ends
    up last regardless of which section changed.
    """
    match = TOTAL_MARKER_RE.search(body)
    if not match:
        return body, None
    totals = {
        "cost": float(match.group("cost")),
        "input_tokens": int(match.group("input")),
        "output_tokens": int(match.group("output")),
        "count": int(match.group("count")),
    }
    return body[: match.start()], totals


def render_total_block(cost, input_tokens, output_tokens, count):
    # repr(cost), not f"{cost:.4f}", in the machine-readable marker --
    # this value gets read back and added to on every future diagnosis, so
    # rounding it to 4 decimals here would compound a small error on every
    # addition. The display line below still rounds to 4dp for reading.
    plural = "is" if count == 1 else "es"
    # <sub> (smaller) + italic, all as one single-line paragraph -- never
    # split across a blank line into this, since <sub> only shrinks a
    # single line safely (see ai-diagnose-failure.py's blockquote/
    # build_diagnosis_body for the multi-line overlap bug this avoids).
    return (
        f"\n\n<!-- osac-ai-total-cost:{cost!r}:{input_tokens}:{output_tokens}:{count} -->\n"
        f"<sub>*Total AI diagnostic cost for this PR:* ${cost:.4f} "
        f"({input_tokens} input + {output_tokens} output tokens across {count} diagnos{plural})</sub>\n"
    )


def upsert_section(body, key, content):
    start = f"<!-- section:{key} -->"
    end = f"<!-- /section:{key} -->"
    # content (e.g. an LLM diagnosis) can echo attacker-influenced log/diff
    # text verbatim. If it happens to contain a literal copy of `end` (or
    # any other section marker), a later non-greedy regex search below
    # would terminate at that embedded copy instead of the real delimiter,
    # truncating the match and leaving stale content behind on the next
    # update. Neutralize any embedded marker-shaped text before it's ever
    # written into the body -- rendered as inert literal text, not parsed.
    safe_content = _MARKER_RE.sub(
        lambda m: m.group(0).replace("<!--", "&lt;!--"), content.strip()
    )
    block = f"{start}\n{safe_content}\n{end}"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(body):
        # Replacement via a function, not a literal string, so backslash
        # sequences in the diagnosis text (e.g. "\1" inside a stack trace)
        # are never interpreted as regex backreferences.
        return pattern.sub(lambda _match: block, body, count=1)
    sep = "\n\n" if body.strip() else "\n"
    return body.rstrip() + sep + block + "\n"


def main():
    current_body_path = os.environ["CURRENT_BODY_FILE"]
    section_key = os.environ["SECTION_KEY"]
    section_content_path = os.environ["SECTION_CONTENT_FILE"]
    output_path = os.environ["OUTPUT_BODY_FILE"]
    # This run's own cost contribution to the running total -- unset/empty
    # (not "0") when the diagnosis had no usable cost data (an exception
    # before Gemini responded, or GEMINI_MODEL missing from the pricing
    # table), and always empty for the "now passing" edit path, which never
    # runs a diagnosis at all. Either way, an empty RUN_COST_USD means
    # "don't add anything", not "add zero" -- see the totals-carry-forward
    # branch below.
    run_cost_raw = os.environ.get("RUN_COST_USD", "").strip()
    run_input_tokens_raw = os.environ.get("RUN_INPUT_TOKENS", "").strip()
    run_output_tokens_raw = os.environ.get("RUN_OUTPUT_TOKENS", "").strip()

    current_body = ""
    if os.path.isfile(current_body_path):
        with open(current_body_path, "r") as f:
            current_body = f.read()
    if not current_body.strip():
        current_body = COMMENT_MARKER + "\n"
    elif COMMENT_MARKER not in current_body:
        # Shouldn't happen (we only ever PATCH comments found via the
        # marker), but stay safe if the comment was ever hand-edited.
        current_body = COMMENT_MARKER + "\n" + current_body

    current_body, totals = strip_total_block(current_body)

    with open(section_content_path, "r") as f:
        section_content = f.read()

    new_body = upsert_section(current_body, section_key, section_content)

    if run_cost_raw:
        run_cost = float(run_cost_raw)
        run_input_tokens = int(run_input_tokens_raw)
        run_output_tokens = int(run_output_tokens_raw)
        if totals:
            total_cost = totals["cost"] + run_cost
            total_input_tokens = totals["input_tokens"] + run_input_tokens
            total_output_tokens = totals["output_tokens"] + run_output_tokens
            total_count = totals["count"] + 1
        else:
            total_cost = run_cost
            total_input_tokens = run_input_tokens
            total_output_tokens = run_output_tokens
            total_count = 1
        new_body = new_body.rstrip() + render_total_block(
            total_cost, total_input_tokens, total_output_tokens, total_count
        )
    elif totals:
        # Nothing new to add this call, but a total already existed --
        # carry it forward unchanged rather than silently dropping it (the
        # strip above already removed it from current_body).
        new_body = new_body.rstrip() + render_total_block(
            totals["cost"], totals["input_tokens"], totals["output_tokens"], totals["count"]
        )

    with open(output_path, "w") as f:
        f.write(new_body)


if __name__ == "__main__":
    sys.exit(main())
