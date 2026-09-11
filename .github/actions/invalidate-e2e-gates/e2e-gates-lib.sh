#!/usr/bin/env bash
# Shared helpers for merge-required e2e-*-gate checks on a commit SHA.

set -euo pipefail

readonly MERGE_E2E_GATE_NAMES=(e2e-vmaas-gate e2e-bmaas-gate e2e-caas-gate)

# Load all check runs for HEAD_SHA into CHECK_RUNS_JSON (array).
load_check_runs_for_sha() {
  local page=1 resp count tmpdir
  # Commits with enough check-runs (this repo's PRs routinely have 100+)
  # produce a multi-hundred-KB JSON blob. Passing that as a --argjson
  # command-line argument on every page (as this used to do, re-embedding
  # the whole growing accumulator each time) hits Linux's ~128KB
  # single-argument limit (MAX_ARG_STRLEN) and fails with "Argument list
  # too long". Writing each page to a file and slurping the files instead
  # keeps the JSON off the command line entirely.
  tmpdir=$(mktemp -d)
  local pages=()
  while true; do
    resp=$(gh api "repos/${REPO}/commits/${HEAD_SHA}/check-runs?per_page=100&page=${page}")
    jq -c '.check_runs' <<<"${resp}" > "${tmpdir}/page-${page}.json"
    pages+=("${tmpdir}/page-${page}.json")
    count=$(jq '.check_runs | length' <<<"${resp}")
    if [[ "${count}" -lt 100 ]]; then
      break
    fi
    page=$((page + 1))
  done
  CHECK_RUNS_JSON=$(jq -s 'add' "${pages[@]}")
  rm -rf "${tmpdir}"
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
