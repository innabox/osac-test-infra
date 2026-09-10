#!/usr/bin/env bash
# rotate-slack-oncall.sh -- Rotate a Slack usergroup's single member weekly
# and post an announcement describing the CI Duty responsibility (content
# taken from the CI Duty slide deck, see SLIDES_URL below).
#
# The rotation index is whole weeks elapsed since a fixed reference
# Wednesday, modulo the rotation list length, so no index/state needs to
# persist between runs -- a missed or manually re-run week self-corrects
# instead of drifting. This (rather than the ISO week number itself) is
# what avoids two consecutive weeks picking the same person across an
# ISO-year boundary, where the week number resets from 52/53 back to 1.
#
# The duty window is Wednesday through the following Tuesday. The rotation
# anchors to the most recently started Wednesday on or before "today"
# (rather than "today" itself), so a manual run on any day of the week
# still reports the correct window and picks the same person as a run on
# the actual Wednesday would.
#
# Required env vars:
#   SLACK_BOT_TOKEN             Bot token with usergroups:write, users:read,
#                               users:read.email, chat:write scopes
#   SLACK_ONCALL_USERGROUP_ID   Usergroup ID to update (e.g. S0123ABCDEF)
#   SLACK_ONCALL_CHANNEL_ID     Channel ID to post the announcement to
#   SLACK_ONCALL_ROTATION_LIST  Comma-separated list of email addresses
set -euo pipefail

: "${SLACK_BOT_TOKEN:?SLACK_BOT_TOKEN is required}"
: "${SLACK_ONCALL_USERGROUP_ID:?SLACK_ONCALL_USERGROUP_ID is required}"
: "${SLACK_ONCALL_CHANNEL_ID:?SLACK_ONCALL_CHANNEL_ID is required}"
: "${SLACK_ONCALL_ROTATION_LIST:?SLACK_ONCALL_ROTATION_LIST is required}"

SLIDES_URL="https://docs.google.com/presentation/d/1AzoMigaqLuKGNu35yoE5GDGtetlhQqxKDkYdHRwOx-8"
GRAFANA_URL="https://osac-ci.redhat.com:3000/"

trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

slack_get() {
    local method="$1"
    shift
    curl -sf --connect-timeout 10 --max-time 30 -G \
        -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
        "$@" "https://slack.com/api/${method}"
}

slack_post() {
    local method="$1"
    shift
    curl -sf --connect-timeout 10 --max-time 30 \
        -X POST -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
        -H 'Content-type: application/json; charset=utf-8' \
        "$@" "https://slack.com/api/${method}"
}

check_ok() {
    local response="$1" context="$2"
    if [ "$(echo "$response" | jq -r '.ok')" != "true" ]; then
        echo "::error::${context} failed: $(echo "$response" | jq -r '.error // "unknown error"')" >&2
        exit 1
    fi
}

IFS=',' read -ra RAW_PEOPLE <<< "$SLACK_ONCALL_ROTATION_LIST"
COUNT=${#RAW_PEOPLE[@]}
[ "$COUNT" -gt 0 ] || { echo "::error::SLACK_ONCALL_ROTATION_LIST is empty" >&2; exit 1; }

PEOPLE=()
for i in "${!RAW_PEOPLE[@]}"; do
    entry="$(trim "${RAW_PEOPLE[$i]}")"
    [ -n "$entry" ] || { echo "::error::SLACK_ONCALL_ROTATION_LIST has an empty entry at position $((i + 1)) (check for consecutive or trailing commas)" >&2; exit 1; }
    PEOPLE+=("$entry")
done

WEEKDAY=$(date -u +%u) # 1=Monday .. 7=Sunday
DAYS_SINCE_WEDNESDAY=$(( (WEEKDAY - 3 + 7) % 7 ))
ANCHOR_DATE=$(date -u -d "-${DAYS_SINCE_WEDNESDAY} days" +%Y-%m-%d)

# Arbitrary fixed Wednesday used only as a stable epoch for a continuously
# incrementing week counter -- never change this once in use, or every
# rotation index shifts. Both dates being Wednesdays guarantees the day
# count below is always an exact multiple of 7.
REFERENCE_WEDNESDAY="2024-01-03"
DAYS_SINCE_REFERENCE=$(( ( $(date -u -d "$ANCHOR_DATE" +%s) - $(date -u -d "$REFERENCE_WEDNESDAY" +%s) ) / 86400 ))
WEEKS_ELAPSED=$(( DAYS_SINCE_REFERENCE / 7 ))
INDEX=$(( ((WEEKS_ELAPSED % COUNT) + COUNT) % COUNT ))
CURRENT_EMAIL="${PEOPLE[$INDEX]}"

DUTY_START=$(date -u -d "$ANCHOR_DATE" +"%b %-d, %Y")
DUTY_END=$(date -u -d "$ANCHOR_DATE +6 days" +"%b %-d, %Y")

echo "Week ${WEEKS_ELAPSED} since ${REFERENCE_WEDNESDAY}: rotating on-call (index ${INDEX} of ${COUNT}), duty window ${DUTY_START} - ${DUTY_END}"

LOOKUP=$(slack_get "users.lookupByEmail" --data-urlencode "email=${CURRENT_EMAIL}")
check_ok "$LOOKUP" "users.lookupByEmail(index ${INDEX})"
USER_ID=$(echo "$LOOKUP" | jq -r '.user.id')
USER_DISPLAY_NAME=$(echo "$LOOKUP" | jq -r '.user.real_name // .user.name // .user.id')

UPDATE_PAYLOAD=$(jq -n --arg usergroup "$SLACK_ONCALL_USERGROUP_ID" --arg users "$USER_ID" \
    '{usergroup: $usergroup, users: $users}')
UPDATE=$(slack_post "usergroups.users.update" --data "$UPDATE_PAYLOAD")
check_ok "$UPDATE" "usergroups.users.update(${USER_ID})"

DUTY_TEXT=$(cat <<'EOF'
*CI duty* involves monitoring system health, triaging infrastructure failures, and keeping the merge queue moving. The workgroup lead owns CI duty and decides how their working group covers it (24/7) -- one person for the week or several trading off, with the lead answering for coverage either way.

*Every day*
• Read the morning CI health digest: success rate, infra vs. test breakdown, flake rate, time to merge.
• Check #osac-ci for runner/disk/service alerts and #wg-osac-infra for reported/ongoing incidents.
• Look at the merge queue -- a queue that is not draining is the highest-value thing to fix.

*Ongoing*
• Triage failing periodic runs before they become background noise -- submit fixes if possible (review/approve the automatic chai-bot fixes).
• Unblock stuck pull requests: absent check names, expired authorizations, open reviews.
• Monitor the CI Grafana dashboard for trends, runner/machine status, free disk space, etc.

*Escalate these*
• A green pull request that fails in the queue is either a conflict or a flake.
• A required check that no longer exists needs a configuration change.
EOF
)

BLOCKS=$(jq -n \
    --arg mention "<@${USER_ID}>" \
    --arg duty_window "${DUTY_START} - ${DUTY_END}" \
    --arg duty_text "$DUTY_TEXT" \
    --arg grafana_url "$GRAFANA_URL" \
    --arg slides_url "$SLIDES_URL" \
    '[
        {"type": "header", "text": {"type": "plain_text", "text": "CI Duty Rotation", "emoji": true}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (":rotating_light: *On duty:* " + $mention + "\n*Duty window:* " + $duty_window)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": $duty_text}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Grafana dashboard"}, "url": $grafana_url},
            {"type": "button", "text": {"type": "plain_text", "text": "CI Duty slides"}, "url": $slides_url}
        ]}
    ]')

MESSAGE_PAYLOAD=$(jq -n \
    --arg channel "$SLACK_ONCALL_CHANNEL_ID" \
    --arg fallback "CI duty for ${DUTY_START} - ${DUTY_END} is ${USER_DISPLAY_NAME}" \
    --argjson blocks "$BLOCKS" \
    '{channel: $channel, text: $fallback, blocks: $blocks}')
POST=$(slack_post "chat.postMessage" --data "$MESSAGE_PAYLOAD")
check_ok "$POST" "chat.postMessage"

echo "Rotated on-call to index ${INDEX} of ${COUNT} (${USER_ID}) for ${DUTY_START} - ${DUTY_END} and posted the announcement."
