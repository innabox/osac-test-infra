#!/usr/bin/env bash
#
# Refresh OSAC on the snapshot cluster with agentless-net values.
# Clones the osac mono-repo, deep-merges the agentless-net overlay
# into the cloned caas-ci infra.yaml and instance.yaml in-place,
# then installs OSAC via the osac-installer Makefile.
# Corresponds to setup-lab.sh steps 7-10.
# Idempotent — safe to re-run.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/.."

# shellcheck source=/dev/null
source "${INFRA_DIR}/.mgmt-network"

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
OSAC_PROFILE="${OSAC_PROFILE:-caas-ci}"
OSAC_REPO="${OSAC_REPO:-https://github.com/osac-project/osac.git}"
OSAC_BRANCH="${OSAC_BRANCH:-main}"
OSAC_DIR="/opt/osac"
INSTALLER_DIR="${OSAC_DIR}/osac-installer"
VALUES_OVERLAY="${INFRA_DIR}/values-overlay.yaml"
PULL_SECRET="${OSAC_PULL_SECRET_PATH:-/root/pull-secret}"
DNS_CREDS="${DNS_CREDENTIALS_PATH:-${INFRA_DIR}/config}"
SSH_PRIVATE_KEY="${SSH_PRIVATE_KEY_PATH:-${HOME}/.ssh/id_rsa}"

export KUBECONFIG

info() { echo "==> $*"; }

# ---------- clone osac mono-repo ----------

if [ -d "$OSAC_DIR" ]; then
    info "osac mono-repo already cloned at ${OSAC_DIR}"
    git -C "$OSAC_DIR" fetch origin
    git -C "$OSAC_DIR" checkout "$OSAC_BRANCH"
    git -C "$OSAC_DIR" pull --ff-only || true
else
    info "Cloning osac mono-repo (${OSAC_REPO} @ ${OSAC_BRANCH})..."
    git clone --branch "$OSAC_BRANCH" "$OSAC_REPO" "$OSAC_DIR"
fi

# ---------- merge values overlay ----------

PROFILE_DIR="${INSTALLER_DIR}/values/${OSAC_PROFILE}"

info "Deep-merging values overlay into cloned ${OSAC_PROFILE} profile..."
python3 -c "
import yaml, sys, copy

def deep_merge(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result

with open(sys.argv[1]) as f:
    overlay = yaml.safe_load(f)

for values_file in ('infra.yaml', 'instance.yaml'):
    path = sys.argv[2] + '/' + values_file
    with open(path) as f:
        base = yaml.safe_load(f)
    merged = deep_merge(base, overlay)
    with open(path, 'w') as f:
        yaml.dump(merged, f, default_flow_style=False, sort_keys=False)
    print(f'  Merged overlay into {path}')
" "$VALUES_OVERLAY" "$PROFILE_DIR"

# Copy pull-secret and license into the profile values directory
AAP_LICENSE="${AAP_LICENSE_PATH:-/root/aap-license.zip}"
cp "$PULL_SECRET" "${PROFILE_DIR}/pull-secret.json"
cp "$AAP_LICENSE" "${PROFILE_DIR}/license.zip"

# ---------- step 8: install OSAC ----------

if KUBECONFIG="$KUBECONFIG" oc get deploy/fulfillment-grpc-server -n "$OSAC_NAMESPACE" 2>/dev/null | grep -q "1/1"; then
    info "OSAC already running — skipping install"
else
    info "Installing OSAC via Helm (this may take several minutes)..."
    (cd "$INSTALLER_DIR" && \
        KUBECONFIG="$KUBECONFIG" \
        make install \
            PLATFORM=openshift \
            PROFILE="$OSAC_PROFILE" \
            NS="$OSAC_NAMESPACE")
fi

# ---------- step 9: patch DNS credentials ----------

if [ -f "$DNS_CREDS" ]; then
    info "Patching cluster-fulfillment-ig with DNS credentials..."
    # shellcheck source=/dev/null
    source "$DNS_CREDS"
    KUBECONFIG="$KUBECONFIG" oc patch secret cluster-fulfillment-ig -n "$OSAC_NAMESPACE" --type merge \
        -p "{\"data\":{\"AWS_ACCESS_KEY_ID\":\"$(echo -n "$AWS_ACCESS_KEY_ID" | base64)\",\"AWS_SECRET_ACCESS_KEY\":\"$(echo -n "$AWS_SECRET_ACCESS_KEY" | base64)\"}}"
else
    echo "  WARN: ${DNS_CREDS} not found — DNS (Route 53) will not work."
fi

# ---------- step 9b: patch SSH key ----------

if [ -f "$SSH_PRIVATE_KEY" ]; then
    info "Patching cluster-fulfillment-ig with SSH key..."
    KUBECONFIG="$KUBECONFIG" oc patch secret cluster-fulfillment-ig -n "$OSAC_NAMESPACE" --type merge \
        -p "{\"data\":{\"SERVER_SSH_KEY\":\"$(base64 -w0 < "$SSH_PRIVATE_KEY")\"}}"
else
    echo "  WARN: ${SSH_PRIVATE_KEY} not found — NMState live apply will not work."
fi

# ---------- step 10: validate OSAC ----------

info "Validating OSAC..."
KUBECONFIG="$KUBECONFIG" oc wait deploy/fulfillment-grpc-server -n "$OSAC_NAMESPACE" \
    --for=condition=Available --timeout=300s
KUBECONFIG="$KUBECONFIG" oc wait deploy/osac-operator -n "$OSAC_NAMESPACE" \
    --for=condition=Available --timeout=300s
info "OSAC is running:"
KUBECONFIG="$KUBECONFIG" oc get pods -n "$OSAC_NAMESPACE" --no-headers | \
    awk '{print $3}' | sort | uniq -c | sort -rn

# ---------- step 10b: patch MetalLB to use fabric subnet ----------

info "Patching MetalLB to use fabric subnet..."
KUBECONFIG="$KUBECONFIG" oc patch ipaddresspool caas-address-pool -n metallb-system \
    --type=merge -p '{"spec":{"addresses":["10.0.0.240-10.0.0.250"]}}'
echo "  Pool: 10.0.0.240-10.0.0.250"

info "deploy-osac complete."
