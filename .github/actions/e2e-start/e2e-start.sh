#!/usr/bin/env bash
# OSAC-3370: replay pull_request e2e runs after an unlock (label or CodeRabbit).
# Called from .github/actions/e2e-start (and e2e-on-label.yml).
#
# Env:
#   GH_TOKEN, REPO, PR_NUMBER
#   EVENT_HEAD_SHA     optional; skip if PR head moved
#   TRIGGER_LABEL      default e2e-ready
#   WORKFLOWS          comma-separated workflow filenames
#   SKIP_LABEL_CHECK   true skips label/actor checks (CodeRabbit start);
#                      still revalidates CR APPROVED on HEAD and no human CR

set -euo pipefail
TRIGGER_LABEL="${TRIGGER_LABEL:-e2e-ready}"
SKIP_LABEL_CHECK="${SKIP_LABEL_CHECK:-false}"
WORKFLOWS="${WORKFLOWS:-e2e-vmaas-full-install-caller.yml,e2e-bmaas-full-install-caller.yml,e2e-caas-full-install-caller.yml}"

# Returns 0 if the fetched PR JSON is still open.
pr_is_open() {
  [[ "$(jq -r '.state' <<<"${pr_json}")" == "open" ]]
}

pr_json=$(gh api "repos/${REPO}/pulls/${PR_NUMBER}")
if ! pr_is_open; then
  echo "PR #${PR_NUMBER} is $(jq -r '.state' <<<"${pr_json}") (not open); skipping."
  exit 0
fi
export HEAD_SHA
HEAD_SHA=$(jq -r '.head.sha' <<<"${pr_json}")
HEAD_REPO=$(jq -r '.head.repo.full_name // empty' <<<"${pr_json}")
HEAD_REF=$(jq -r '.head.ref // empty' <<<"${pr_json}")
if [[ -n "${EVENT_HEAD_SHA}" && "${HEAD_SHA}" != "${EVENT_HEAD_SHA}" ]]; then
  echo "PR head moved (${EVENT_HEAD_SHA:0:7} → ${HEAD_SHA:0:7}); skipping start."
  exit 0
fi
if [[ "${SKIP_LABEL_CHECK}" != "true" ]]; then
  labels_json=$(gh api --paginate --slurp "repos/${REPO}/issues/${PR_NUMBER}/labels" | jq 'add')
  if ! jq -e --arg n "${TRIGGER_LABEL}" '[.[].name] | index($n) != null' <<<"${labels_json}" >/dev/null; then
    echo "${TRIGGER_LABEL} label no longer present; skipping start."
    exit 0
  fi
fi

# Match gate: e2e-ready counts only when applied by github-actions[bot].
e2e_ready_applied_by_bot() {
  local events_json
  if ! events_json=$(gh api --paginate --slurp \
    "repos/${REPO}/issues/${PR_NUMBER}/events?per_page=100" | jq 'add'); then
    return 2
  fi
  jq -e '
    [.[]
      | select(.event == "labeled")
      | select(.label.name == "e2e-ready")
    ] | last
    | . != null and .actor.login == "github-actions[bot]"
  ' <<<"${events_json}" >/dev/null 2>&1
}

# Returns 0 if TRIGGER_LABEL is on the PR, 2 on fetch error.
pr_has_trigger_label() {
  local labels_json
  if ! labels_json=$(gh api --paginate --slurp \
    "repos/${REPO}/issues/${PR_NUMBER}/labels" | jq 'add'); then
    return 2
  fi
  jq -e --arg n "${TRIGGER_LABEL}" '[.[].name] | index($n) != null' \
    <<<"${labels_json}" >/dev/null
}

# Returns 0 if coderabbitai[bot] still APPROVED on HEAD_SHA and no human
# CHANGES_REQUESTED. 1 = dismissed/stale/blocked, 2 = fetch error.
approval_still_valid() {
  local reviews_json
  if ! reviews_json=$(gh api --paginate --slurp \
    "repos/${REPO}/pulls/${PR_NUMBER}/reviews" | jq 'add'); then
    return 2
  fi
  if jq -e '
    [.[]
      | select(.user != null)
      | select(((.user.login // "") | endswith("[bot]")) | not)
      | select((.user.type // "User") != "Bot")
      | select(.state == "APPROVED" or .state == "CHANGES_REQUESTED" or .state == "DISMISSED")
    ]
    | group_by(.user.login)
    | map(max_by([(.submitted_at // ""), (.id // 0)]))
    | map(select(.state == "CHANGES_REQUESTED"))
    | length > 0
  ' <<<"${reviews_json}" >/dev/null 2>&1; then
    return 1
  fi
  jq -e --arg sha "${HEAD_SHA}" --arg who "coderabbitai[bot]" '
    ([.[]
      | select(.user.login == $who)
      | select(.state == "APPROVED" or .state == "CHANGES_REQUESTED" or .state == "DISMISSED")
    ] | max_by([(.submitted_at // ""), (.id // 0)]) // empty) as $latest
    | ($latest != null)
      and ($latest.state == "APPROVED")
      and ($latest.commit_id != null)
      and ($latest.commit_id == $sha)
  ' <<<"${reviews_json}" >/dev/null 2>&1
}

if [[ "${SKIP_LABEL_CHECK}" != "true" && "${TRIGGER_LABEL}" == "e2e-ready" ]]; then
  e2e_rc=0
  e2e_ready_applied_by_bot || e2e_rc=$?
  if [[ ${e2e_rc} -eq 2 ]]; then
    echo "Failed to fetch issue events for PR #${PR_NUMBER}; skipping start."
    exit 1
  fi
  if [[ ${e2e_rc} -ne 0 ]]; then
    echo "e2e-ready was not applied by github-actions[bot]; skipping start."
    {
      echo "### E2E on \`e2e-ready\` skipped"
      echo ""
      echo "Label \`e2e-ready\` must be applied by \`/e2e-ready\` (\`github-actions[bot]\`)."
      echo "Manual UI labels do not unlock expensive e2e."
    } > /tmp/e2e-on-label-untrusted.md
    gh pr comment "${PR_NUMBER}" -R "${REPO}" --body "$(cat /tmp/e2e-on-label-untrusted.md)"
    exit 0
  fi
fi

if [[ "${SKIP_LABEL_CHECK}" == "true" ]]; then
  av_rc=0
  approval_still_valid || av_rc=$?
  if [[ ${av_rc} -eq 2 ]]; then
    echo "Failed to fetch reviews for PR #${PR_NUMBER}; skipping start."
    exit 1
  fi
  if [[ ${av_rc} -ne 0 ]]; then
    echo "CodeRabbit approval no longer valid on ${HEAD_SHA:0:7}; skipping start."
    exit 0
  fi
fi

lib_dir="$(dirname "${BASH_SOURCE[0]}")/../invalidate-e2e-gates"
# shellcheck source=../invalidate-e2e-gates/e2e-gates-lib.sh
source "${lib_dir}/e2e-gates-lib.sh"

if all_merge_e2e_gates_green; then
  {
    if [[ "${SKIP_LABEL_CHECK}" == "true" ]]; then
      echo "### E2E on CodeRabbit approval"
      echo ""
      echo "All merge-required e2e gates already success on HEAD — skipping replay."
    else
      echo "### E2E on \`${TRIGGER_LABEL}\`"
      echo ""
      echo "All merge-required e2e gates already success on HEAD — skipping replay."
    fi
    echo ""
    echo "Accepted: \`e2e-vmaas-gate\`, \`e2e-bmaas-gate\`, \`e2e-caas-gate\` on \`${HEAD_SHA:0:7}\`."
  } > /tmp/e2e-on-label-skip.md
  gh pr comment "${PR_NUMBER}" -R "${REPO}" --body "$(cat /tmp/e2e-on-label-skip.md)"
  exit 0
fi

if [[ "${SKIP_LABEL_CHECK}" == "true" ]]; then
  export REASON="CodeRabbit approval - waiting for fresh full-install run"
else
  export REASON="${TRIGGER_LABEL} unlock - waiting for fresh full-install run"
fi
bash "${lib_dir}/invalidate-e2e-gates.sh"

E2E_WORKFLOWS=()
IFS=',' read -ra _wfs <<< "${WORKFLOWS}"
for w in "${_wfs[@]}"; do
  w="${w#"${w%%[![:space:]]*}"}"
  w="${w%"${w##*[![:space:]]}"}"
  if [[ -n "${w}" ]]; then
    E2E_WORKFLOWS+=("${w}")
  fi
done
if [[ ${#E2E_WORKFLOWS[@]} -eq 0 ]]; then
  echo "No workflow filenames to replay."
  exit 1
fi

STARTED=0
ERRORS=0
SKIPPED=0
PENDING=0
STALE=0
TIMEOUTS=0

# Print the matching pull_request run JSON for workflow $1 at HEAD_SHA, or empty.
find_pr_run() {
  local wf="$1"
  local runs
  # Filter by head_sha. This repo exceeds 100 pull_request e2e runs
  # per day, so an unfiltered first page is not the current head
  # (osac-project/osac#550: /lgtm missed a 22h-old fork run).
  runs=$(gh api \
    "repos/${REPO}/actions/workflows/${wf}/runs?event=pull_request&head_sha=${HEAD_SHA}&per_page=100" \
    --jq '[.workflow_runs[]]')
  # Prefer PR-number match; fork runs often have empty pull_requests —
  # fall back only when head repo+branch uniquely match this PR.
  jq -c --arg sha "${HEAD_SHA}" --argjson pr "${PR_NUMBER}" \
    --arg repo "${HEAD_REPO}" --arg ref "${HEAD_REF}" '
    ([.[]
      | select(.head_sha == $sha)
      | select(any(.pull_requests[]?; .number == $pr))
    ][0])
    // (if ($repo | length) > 0 and ($ref | length) > 0 then
        ([.[]
          | select(.head_sha == $sha)
          | select((.pull_requests // []) | length == 0)
          | select((.head_repository.full_name // "") == $repo)
          | select(.head_branch == $ref)
        ] | if length == 1 then .[0] else empty end)
      else empty end)
    // empty
  ' <<<"${runs}"
}

# Returns: 0 = ok to rerun (completed without expensive e2e, or failed),
#          2 = already going / already green (skip),
#          3 = readiness has not started (skip; in-flight run will pick up unlock),
#          1 = error / timeout.
wait_until_rerun_or_skip() {
  local run_id="$1"
  local view status conclusion busy expensive_ran changes_done readiness
  for _ in $(seq 1 90); do
    view=$(gh run view "${run_id}" -R "${REPO}" --json status,conclusion,jobs)
    status=$(jq -r '.status' <<<"${view}")
    conclusion=$(jq -r '.conclusion // empty' <<<"${view}")
    # Queued for a runner (osac-project/osac#538): this same run will
    # evaluate current unlock labels when it starts. Waiting 3min then
    # failing is wrong — #538 sat 48min before changes even started.
    if [[ "${status}" == "queued" || "${status}" == "waiting" || "${status}" == "pending" ]]; then
      echo "Run #${run_id} is ${status}; skip rerun (will evaluate current labels)."
      return 2
    fi
    # Reusable calls prefix every child with the caller job name
    # (e2e-*-full-install / ...). Do not trust the first match.
    # "waiting" = blocked on needs / environment / concurrency.
    busy=$(jq -r '
      [.jobs[]
        | select(.name | test("full-install"; "i"))
        | select(.name | test("readiness"; "i") | not)
        | .status
      ]
      | if length == 0 then "unknown"
        else (map(select(. == "in_progress" or . == "queued" or . == "waiting")) | length > 0 | tostring)
        end
    ' <<<"${view}")

    changes_done=$(jq -r '
      [.jobs[] | select(.name == "changes") | .conclusion // ""]
      | any(. != "")
    ' <<<"${view}")
    # Cheap jobs have not started: same as queued. Keep waiting only
    # once readiness is in flight (it may have sampled labels before
    # this unlock).
    if [[ "${busy}" == "unknown" && "${status}" != "completed" && "${changes_done}" != "true" ]]; then
      echo "Run #${run_id} has not finished changes; skip rerun."
      return 2
    fi

    # No expensive job yet. If readiness has not started, the in-flight
    # PR run will see the unlock when it does: skip instead of waiting
    # out GitHub's runner queue. If readiness is already running or
    # done, keep waiting (may need rerun if the gate raced the unlock).
    if [[ "${busy}" == "unknown" && "${status}" != "completed" ]]; then
      readiness=$(jq -r '
        [.jobs[]
          | select(.name | test("readiness"; "i"))
          | select(.name | test("full-install"; "i") | not)
          | .status
        ]
        | if length == 0 then "missing" else .[0] end
      ' <<<"${view}")
      if [[ "${readiness}" != "missing" ]]; then
        sleep 2
        continue
      fi
      echo "Readiness has not started on run #${run_id}; skip rerun."
      return 3
    fi

    if [[ "${busy}" == "true" ]]; then
      echo "Expensive job already active on run #${run_id}; skip rerun."
      return 2
    fi

    if [[ "${status}" == "completed" ]]; then
      if [[ "${conclusion}" == "success" ]]; then
        # Unreadiness no longer fails the run (ready=false, expensive
        # skipped). That looks green; still rerun after unlock.
        expensive_ran=$(jq -r '
          [.jobs[]
            | select(.name | test("full-install"; "i"))
            | select(.name | test("readiness"; "i") | not)
            | select(.name | test("gate"; "i") | not)
            | select((.conclusion // "") != "skipped" and (.conclusion // "") != "")
          ] | length > 0
        ' <<<"${view}")
        if [[ "${expensive_ran}" == "true" ]]; then
          return 2
        fi
        echo "Run #${run_id} succeeded without expensive e2e; will rerun."
        return 0
      fi
      echo "Run #${run_id} completed (conclusion=${conclusion:-none}); will rerun."
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting on run #${run_id}; expensive job never started."
  return 1
}

# Replay workflow $1 from the matching PR run, or skip/count error.
start_via_pr_run() {
  local wf="$1"
  local candidates run_id rc cur_sha lbl_rc e2e_rc av_rc

  # Re-validate head/label before each workflow touch.
  if ! pr_json=$(gh api "repos/${REPO}/pulls/${PR_NUMBER}"); then
    echo "Failed to fetch PR #${PR_NUMBER} while processing ${wf}."
    ERRORS=$((ERRORS + 1))
    return
  fi
  if ! pr_is_open; then
    echo "PR #${PR_NUMBER} is no longer open; aborting ${wf}."
    STALE=$((STALE + 1))
    return
  fi
  cur_sha=$(jq -r '.head.sha' <<<"${pr_json}")
  if [[ "${cur_sha}" != "${HEAD_SHA}" ]]; then
    echo "PR head moved before starting ${wf} (${HEAD_SHA:0:7} → ${cur_sha:0:7}); aborting."
    STALE=$((STALE + 1))
    return
  fi
  if [[ "${SKIP_LABEL_CHECK}" != "true" ]]; then
    pr_has_trigger_label
    lbl_rc=$?
    if [[ ${lbl_rc} -eq 2 ]]; then
      echo "Failed to fetch labels for PR #${PR_NUMBER} while processing ${wf}."
      ERRORS=$((ERRORS + 1))
      return
    fi
    if [[ ${lbl_rc} -ne 0 ]]; then
      echo "${TRIGGER_LABEL} label removed before starting ${wf}; aborting."
      STALE=$((STALE + 1))
      return
    fi
    if [[ "${TRIGGER_LABEL}" == "e2e-ready" ]]; then
      e2e_ready_applied_by_bot
      e2e_rc=$?
      if [[ ${e2e_rc} -eq 2 ]]; then
        echo "Failed to fetch issue events for PR #${PR_NUMBER} while processing ${wf}."
        ERRORS=$((ERRORS + 1))
        return
      fi
      if [[ ${e2e_rc} -ne 0 ]]; then
        echo "e2e-ready no longer trusted before starting ${wf}; aborting."
        STALE=$((STALE + 1))
        return
      fi
    fi
  else
    av_rc=0
    approval_still_valid || av_rc=$?
    if [[ ${av_rc} -eq 2 ]]; then
      echo "Failed to fetch reviews for PR #${PR_NUMBER} while processing ${wf}."
      ERRORS=$((ERRORS + 1))
      return
    fi
    if [[ ${av_rc} -ne 0 ]]; then
      echo "CodeRabbit approval no longer valid before starting ${wf}; aborting."
      STALE=$((STALE + 1))
      return
    fi
  fi

  candidates=""
  for _ in $(seq 1 30); do
    candidates=$(find_pr_run "${wf}")
    if [[ -n "${candidates}" && "${candidates}" != "null" ]]; then
      break
    fi
    echo "Waiting for pull_request run for ${wf} at ${HEAD_SHA:0:7}..."
    sleep 2
  done
  if [[ -z "${candidates}" || "${candidates}" == "null" ]]; then
    echo "No pull_request run for ${wf} at ${HEAD_SHA:0:7} on PR #${PR_NUMBER}."
    ERRORS=$((ERRORS + 1))
    return
  fi

  run_id=$(jq -r '.id' <<<"${candidates}")
  echo "Considering ${wf} PR run #${run_id}..."

  wait_until_rerun_or_skip "${run_id}"
  rc=$?
  if [[ ${rc} -eq 2 ]]; then
    SKIPPED=$((SKIPPED + 1))
    return
  fi
  if [[ ${rc} -eq 3 ]]; then
    PENDING=$((PENDING + 1))
    return
  fi
  if [[ ${rc} -ne 0 ]]; then
    TIMEOUTS=$((TIMEOUTS + 1))
    return
  fi

  # Re-check head + label (and e2e-ready actor) immediately before rerun.
  if ! pr_json=$(gh api "repos/${REPO}/pulls/${PR_NUMBER}"); then
    echo "Failed to fetch PR #${PR_NUMBER} before rerun of ${wf}."
    ERRORS=$((ERRORS + 1))
    return
  fi
  if ! pr_is_open; then
    echo "PR #${PR_NUMBER} is no longer open; skipping rerun of ${wf}."
    STALE=$((STALE + 1))
    return
  fi
  cur_sha=$(jq -r '.head.sha' <<<"${pr_json}")
  if [[ "${cur_sha}" != "${HEAD_SHA}" ]]; then
    echo "PR head moved while waiting for ${wf}; skipping rerun."
    STALE=$((STALE + 1))
    return
  fi
  if [[ "${SKIP_LABEL_CHECK}" != "true" ]]; then
    pr_has_trigger_label
    lbl_rc=$?
    if [[ ${lbl_rc} -eq 2 ]]; then
      echo "Failed to fetch labels for PR #${PR_NUMBER} before rerun of ${wf}."
      ERRORS=$((ERRORS + 1))
      return
    fi
    if [[ ${lbl_rc} -ne 0 ]]; then
      echo "${TRIGGER_LABEL} label gone before rerun of ${wf}; skipping."
      STALE=$((STALE + 1))
      return
    fi
    if [[ "${TRIGGER_LABEL}" == "e2e-ready" ]]; then
      e2e_ready_applied_by_bot
      e2e_rc=$?
      if [[ ${e2e_rc} -eq 2 ]]; then
        echo "Failed to fetch issue events for PR #${PR_NUMBER} before rerun of ${wf}."
        ERRORS=$((ERRORS + 1))
        return
      fi
      if [[ ${e2e_rc} -ne 0 ]]; then
        echo "e2e-ready no longer trusted before rerun of ${wf}; skipping."
        STALE=$((STALE + 1))
        return
      fi
    fi
  else
    av_rc=0
    approval_still_valid || av_rc=$?
    if [[ ${av_rc} -eq 2 ]]; then
      echo "Failed to fetch reviews for PR #${PR_NUMBER} before rerun of ${wf}."
      ERRORS=$((ERRORS + 1))
      return
    fi
    if [[ ${av_rc} -ne 0 ]]; then
      echo "CodeRabbit approval no longer valid before rerun of ${wf}; skipping."
      STALE=$((STALE + 1))
      return
    fi
  fi

  if gh run rerun "${run_id}" -R "${REPO}"; then
    STARTED=$((STARTED + 1))
  else
    echo "Failed to start ${wf} from run #${run_id}"
    ERRORS=$((ERRORS + 1))
  fi
}

echo "${TRIGGER_LABEL} label on PR #${PR_NUMBER} (${HEAD_SHA:0:7}) — starting e2e via pull_request rerun."
if [[ "${SKIP_LABEL_CHECK}" == "true" ]]; then
  echo "skip_label_check=true (CodeRabbit approval start)."
fi

for wf in "${E2E_WORKFLOWS[@]}"; do
  set +e
  start_via_pr_run "${wf}"
  wf_rc=$?
  set -e
  if [[ ${wf_rc} -ne 0 ]]; then
    echo "Unexpected failure while processing ${wf} (rc=${wf_rc})."
    ERRORS=$((ERRORS + 1))
  fi
done

if [[ "${SKIP_LABEL_CHECK}" == "true" ]]; then
  {
    echo "### E2E on CodeRabbit approval"
    echo ""
    echo "CodeRabbit APPROVED — starting expensive e2e (PR run replay)."
  } > /tmp/e2e-on-label.md
else
  {
    echo "### E2E on \`${TRIGGER_LABEL}\`"
    echo ""
    echo "Label \`${TRIGGER_LABEL}\` applied — starting expensive e2e (PR run replay)."
  } > /tmp/e2e-on-label.md
fi
{
  echo "- Started: ${STARTED}/${#E2E_WORKFLOWS[@]}"
  if [[ ${SKIPPED} -gt 0 ]]; then
    echo "- Already active/green (skipped rerun): ${SKIPPED}"
  fi
  if [[ ${PENDING} -gt 0 ]]; then
    echo "- Readiness not started (in-flight run will pick up unlock): ${PENDING}"
  fi
  if [[ ${STALE} -gt 0 ]]; then
    echo "- Head moved or label withdrawn (no rerun): ${STALE}"
  fi
  if [[ ${TIMEOUTS} -gt 0 ]]; then
    echo "- Timed out waiting for the expensive job to reach a terminal state: ${TIMEOUTS}"
  fi
  if [[ ${ERRORS} -gt 0 ]]; then
    echo "- Errors: ${ERRORS}"
    echo ""
    echo "Needs a prior \`pull_request\` e2e run at this head for PR #${PR_NUMBER}."
  fi
} >> /tmp/e2e-on-label.md

gh pr comment "${PR_NUMBER}" -R "${REPO}" --body "$(cat /tmp/e2e-on-label.md)"

if [[ ${ERRORS} -gt 0 || ${TIMEOUTS} -gt 0 ]]; then
  exit 1
fi
