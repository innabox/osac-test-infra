#!/usr/bin/env bash
#
# Deploy OSAC on the agentless-net cluster.
# Clones the osac mono-repo, applies the agentless-net overrides on top of
# the caas-ci profile values (via yq), and runs `make install`.
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
AAP_LICENSE="${AAP_LICENSE_PATH:-/root/aap-license.zip}"
DNS_CREDS="${DNS_CREDENTIALS_PATH:-${INFRA_DIR}/config}"
SSH_PRIVATE_KEY="${SSH_PRIVATE_KEY_PATH:-${HOME}/.ssh/id_rsa}"

export KUBECONFIG VALUES_OVERLAY

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

# ---------- apply agentless-net overrides to the split caas-ci values ----------
#
# OSAC-3752 split values/caas-ci/values.yaml into infra.yaml (osac-deps +
# osac-infra charts) and instance.yaml (osac chart). yq-merge the overlay
# subtrees into the matching file — kafka belongs to the infra charts,
# metering + aap to the osac chart — mirroring how e2e-caas-full-install.yml
# patches the profile values in place.

INFRA_VALUES="${INSTALLER_DIR}/values/${OSAC_PROFILE}/infra.yaml"
INSTANCE_VALUES="${INSTALLER_DIR}/values/${OSAC_PROFILE}/instance.yaml"

info "Applying agentless-net overrides to ${OSAC_PROFILE} values..."
yq -i '. *= (load(env(VALUES_OVERLAY)) | pick(["kafka"]))' "$INFRA_VALUES"
yq -i '. *= (load(env(VALUES_OVERLAY)) | pick(["metering", "aap"]))' "$INSTANCE_VALUES"

# ---------- inject DNS + SSH secrets into the osac instance values ----------
#
# These populate the config-as-code-ig secret at install time. The old
# post-install `oc patch secret cluster-fulfillment-ig` target was removed
# by OSAC-3752.

if [ -f "$DNS_CREDS" ]; then
    info "Injecting Route 53 DNS credentials..."
    # shellcheck source=/dev/null
    source "$DNS_CREDS"
    export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
    yq -i '.aap.instanceGroups.clusterFulfillment.secret.AWS_ACCESS_KEY_ID = strenv(AWS_ACCESS_KEY_ID) |
           .aap.instanceGroups.clusterFulfillment.secret.AWS_SECRET_ACCESS_KEY = strenv(AWS_SECRET_ACCESS_KEY)' \
        "$INSTANCE_VALUES"
else
    echo "  WARN: ${DNS_CREDS} not found — DNS (Route 53) will not work."
fi

if [ -f "$SSH_PRIVATE_KEY" ]; then
    info "Injecting SSH private key..."
    SERVER_SSH_KEY="$(cat "$SSH_PRIVATE_KEY")" \
    yq -i '.aap.instanceGroups.clusterFulfillment.secret.SERVER_SSH_KEY = strenv(SERVER_SSH_KEY)' \
        "$INSTANCE_VALUES"
else
    echo "  WARN: ${SSH_PRIVATE_KEY} not found — NMState live apply will not work."
fi

# ---------- install OSAC ----------

if KUBECONFIG="$KUBECONFIG" oc get deploy/fulfillment-grpc-server -n "$OSAC_NAMESPACE" 2>/dev/null | grep -q "1/1"; then
    info "OSAC already running — skipping install"
else
    info "Installing OSAC via Helm (this may take several minutes)..."
    (cd "$INSTALLER_DIR" && \
        KUBECONFIG="$KUBECONFIG" \
        make install \
            PLATFORM=openshift \
            PROFILE="$OSAC_PROFILE" \
            NS="$OSAC_NAMESPACE" \
            AAP_LICENSE_FILE="$AAP_LICENSE")
fi

# ---------- validate OSAC ----------

info "Validating OSAC..."
KUBECONFIG="$KUBECONFIG" oc wait deploy/fulfillment-grpc-server -n "$OSAC_NAMESPACE" \
    --for=condition=Available --timeout=300s
KUBECONFIG="$KUBECONFIG" oc wait deploy/osac-operator -n "$OSAC_NAMESPACE" \
    --for=condition=Available --timeout=300s
info "OSAC is running:"
KUBECONFIG="$KUBECONFIG" oc get pods -n "$OSAC_NAMESPACE" --no-headers | \
    awk '{print $3}' | sort | uniq -c | sort -rn

# ---------- patch MetalLB to use fabric subnet ----------

info "Patching MetalLB to use fabric subnet..."
KUBECONFIG="$KUBECONFIG" oc patch ipaddresspool caas-address-pool -n metallb-system \
    --type=merge -p '{"spec":{"addresses":["10.0.0.240-10.0.0.250"]}}'
echo "  Pool: 10.0.0.240-10.0.0.250"

info "deploy-osac complete."
