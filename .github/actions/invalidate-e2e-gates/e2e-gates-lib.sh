#!/usr/bin/env bash
# Shared helpers for merge-required e2e-*-gate checks on a commit SHA.

set -euo pipefail

readonly MERGE_E2E_GATE_NAMES=(e2e-vmaas-gate e2e-bmaas-gate e2e-caas-gate)

# Load all check runs for HEAD_SHA into CHECK_RUNS_JSON (array).
load_check_runs_for_sha() {
  local page=1 resp count batch
  CHECK_RUNS_JSON='[]'
  while true; do
    resp=$(gh api "repos/${REPO}/commits/${HEAD_SHA}/check-runs?per_page=100&page=${page}")
    batch=$(jq -c '.check_runs' <<<"${resp}")
    CHECK_RUNS_JSON=$(jq -n --argjson acc "${CHECK_RUNS_JSON}" --argjson page "${batch}" '$acc + $page')
    count=$(jq '.check_runs | length' <<<"${resp}")
    if [[ "${count}" -lt 100 ]]; then
      break
    fi
    page=$((page + 1))
  done
}

# Print latest conclusion for a gate name (success, failure, missing, ...).
latest_gate_conclusion() {
  local gate="$1"
  jq -r --arg g "${gate}" '
    [.[] | select(.name == $g)]
    | sort_by(.started_at)
    | last
    | .conclusion // "missing"
  ' <<<"${CHECK_RUNS_JSON}"
}

# Exit 0 when every merge-required gate is success on HEAD_SHA.
all_merge_e2e_gates_green() {
  local gate conclusion
  load_check_runs_for_sha
  for gate in "${MERGE_E2E_GATE_NAMES[@]}"; do
    conclusion=$(latest_gate_conclusion "${gate}")
    if [[ "${conclusion}" != "success" ]]; then
      echo "Gate ${gate} on ${HEAD_SHA:0:7}: ${conclusion}"
      return 1
    fi
  done
  echo "All merge-required e2e gates success on ${HEAD_SHA:0:7}"
  return 0
}
