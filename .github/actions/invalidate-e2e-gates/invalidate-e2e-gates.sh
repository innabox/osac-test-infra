#!/usr/bin/env bash
# Mark merge-required e2e-*-gate checks in_progress on HEAD_SHA unless all
# three already passed on this SHA (manual workflow_dispatch and PR runs).

set -euo pipefail

lib_dir="$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=e2e-gates-lib.sh
source "${lib_dir}/e2e-gates-lib.sh"

readonly SKIP_IF_ALL_GREEN="${SKIP_IF_ALL_GREEN:-true}"

REASON="${REASON:-E2E unlock - waiting for fresh full-install run}"
DETAILS_URL="${DETAILS_URL:-${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}}"

if [[ -z "${HEAD_SHA:-}" ]]; then
  echo "HEAD_SHA is required" >&2
  exit 1
fi

if [[ "${SKIP_IF_ALL_GREEN}" == "true" ]] && all_merge_e2e_gates_green; then
  echo "Skipping invalidation: all merge-required e2e gates already success on HEAD."
  exit 0
fi

summary=$(printf '%s\n\n%s\n\n%s' "${REASON}" \
  "Partial or missing gate success on this SHA; waiting for a fresh full-install run." \
  "See .github/e2e-readiness.md")

failed=0
for gate in "${MERGE_E2E_GATE_NAMES[@]}"; do
  payload=$(jq -n \
    --arg name "${gate}" \
    --arg sha "${HEAD_SHA}" \
    --arg started "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg title "${REASON}" \
    --arg details "${DETAILS_URL}" \
    --arg summary "${summary}" \
    '{
      name: $name,
      head_sha: $sha,
      status: "in_progress",
      details_url: $details,
      started_at: $started,
      output: {
        title: $title,
        summary: $summary
      }
    }')
  if gh api "repos/${REPO}/check-runs" --input - <<<"${payload}"; then
    echo "Marked ${gate} in_progress on ${HEAD_SHA:0:7}"
  else
    echo "Could not post in_progress ${gate} (fork PRs may lack checks:write)." >&2
    failed=1
  fi
done

if [[ ${failed} -ne 0 ]]; then
  echo "Some e2e gates could not be invalidated; merge may still see stale greens." >&2
  exit 1
fi
