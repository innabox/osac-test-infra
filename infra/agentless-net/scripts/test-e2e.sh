#!/usr/bin/env bash
#
# E2E test: create hosted clusters, wait for READY, validate
# self-connectivity and cross-cluster isolation.
# Corresponds to test-e2e.sh.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/.."

# shellcheck source=/dev/null
source "${INFRA_DIR}/.mgmt-network"

export KUBECONFIG

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"
PULL_SECRET="${OSAC_PULL_SECRET_PATH:-/root/pull-secret}"

CLUSTER_TEMPLATE="osac.templates.ocp_ci_small"
CLUSTER_NAMES=("cluster-1" "cluster-2")

HOST_VMS=("host-1" "host-2")

info() { echo "==> $*"; }

cluster_exists() {
    local name="$1"
    local result
    result=$(osac get cluster "$name" -o json 2>/dev/null)
    [ "$result" != "[]" ] && [ -n "$result" ]
}

cluster_id_by_name() {
    local name="$1"
    osac get clusters -o json | python3 -c "
import sys,json
for c in json.load(sys.stdin):
    if c.get('name') == '${name}':
        print(c['id']); break
"
}

# Print agent + clusterorder progression so the long PROGRESSING waits are
# actionable. Selects only benign agent debugInfo fields (state/stateInfo),
# never the token-bearing eventsURL/logsURL.
print_progress() {
    echo "    agents:"
    KUBECONFIG="$KUBECONFIG" oc get agents -A --no-headers \
        -o 'custom-columns=HOST:.metadata.annotations.osac\.openshift\.io/host_uuid,APPROVED:.spec.approved,ROLE:.status.role,STATE:.status.debugInfo.state,INFO:.status.debugInfo.stateInfo' \
        2>/dev/null | sed 's/^/      /' || true
    echo "    clusterorders:"
    KUBECONFIG="$KUBECONFIG" oc get clusterorder -n "$OSAC_NAMESPACE" --no-headers \
        -o 'custom-columns=NAME:.metadata.name,PHASE:.status.phase' \
        2>/dev/null | sed 's/^/      /' || true
}

# ---------- preflight ----------

info "Running preflight checks..."

if ! oc get deploy/fulfillment-grpc-server -n "$OSAC_NAMESPACE" 2>/dev/null | grep -q "1/1"; then
    echo "ERROR: OSAC is not running."
    exit 1
fi

AGENT_COUNT=$(oc get agent -n hardware-inventory --no-headers 2>/dev/null | wc -l)
if [ "$AGENT_COUNT" -lt "${#CLUSTER_NAMES[@]}" ]; then
    echo "ERROR: Need at least ${#CLUSTER_NAMES[@]} agents, found ${AGENT_COUNT}"
    exit 1
fi

info "OSAC running, ${AGENT_COUNT} agents available"

# ---------- osac login ----------
#
# The osac CLI needs a persisted login (~/.config/osac) before the get/create
# calls below. Mirror tests/conftest.py's private_cli: internal API address,
# admin SA token, --private/--insecure. setup-caas.sh talks to the same
# internal API via curl; here we use the CLI.

info "Logging in to OSAC..."
INTERNAL_API="https://$(oc get route fulfillment-internal-api -n "$OSAC_NAMESPACE" -o jsonpath='{.status.ingress[0].host}')"
osac login --address "$INTERNAL_API" --insecure --private \
    --token-script "oc create token -n $OSAC_NAMESPACE admin"

# ---------- create clusters ----------

info "Creating clusters..."
CLUSTER_IDS=()
for name in "${CLUSTER_NAMES[@]}"; do
    if cluster_exists "$name"; then
        echo "  $name already exists — skipping creation"
        CLUSTER_ID=$(cluster_id_by_name "$name")
    else
        if ! CREATE_OUT=$(osac create cluster \
                --template "$CLUSTER_TEMPLATE" \
                -f pull_secret="$PULL_SECRET" \
                --name "$name" 2>&1); then
            echo "ERROR: osac create cluster failed for $name:"
            echo "$CREATE_OUT"
            exit 1
        fi
        CLUSTER_ID=$(echo "$CREATE_OUT" | grep -oP '[0-9a-f-]{36}' | head -1)
        echo "  Created $name (ID: $CLUSTER_ID)"
    fi
    CLUSTER_IDS+=("$CLUSTER_ID")
done

# ---------- wait for clusters ----------

patch_coredns() {
    local order_name="$1"
    local hc_ns="${OSAC_NAMESPACE}-${order_name}"
    local kc_secret="${order_name}-admin-kubeconfig"
    local guest_kc
    guest_kc=$(mktemp)

    oc get secret "$kc_secret" -n "$hc_ns" -o jsonpath='{.data.kubeconfig}' 2>/dev/null | base64 -d > "$guest_kc" 2>/dev/null
    if [ ! -s "$guest_kc" ]; then
        rm -f "$guest_kc"
        return 1
    fi

    if ! KUBECONFIG="$guest_kc" oc get ds dns-default -n openshift-dns &>/dev/null 2>&1; then
        rm -f "$guest_kc"
        return 1
    fi

    KUBECONFIG="$guest_kc" oc patch daemonset dns-default -n openshift-dns \
        --type=strategic -p '{"spec":{"template":{"spec":{"hostNetwork":true}}}}' 2>/dev/null
    echo "  Patched CoreDNS hostNetwork on $order_name"
    rm -f "$guest_kc"
    return 0
}

info "Waiting for clusters to reach READY state (this may take 30-60 minutes)..."
COREDNS_PATCHED=""
for i in "${!CLUSTER_NAMES[@]}"; do
    name="${CLUSTER_NAMES[$i]}"
    id="${CLUSTER_IDS[$i]}"

    elapsed=0
    while true; do
        state=$(osac get cluster "$id" -o json | \
            python3 -c "import sys,json; print(json.load(sys.stdin).get('status',{}).get('state',''))" 2>/dev/null) || true

        if [ "$state" = "CLUSTER_STATE_READY" ]; then
            info "$name is READY"
            break
        fi

        if [ "$state" = "CLUSTER_STATE_FAILED" ]; then
            echo "ERROR: $name FAILED"
            # Drop template_parameters -- it embeds the pull secret, which
            # would otherwise leak into the CI log.
            osac get cluster "$id" -o yaml | yq 'del(.spec.template_parameters)'
            exit 1
        fi

        # Restart VMs powered off by the assisted-installer (no BMC in lab)
        for vm in "${HOST_VMS[@]}"; do
            if virsh domstate "$vm" 2>/dev/null | grep -q "shut off"; then
                virsh start "$vm" 2>/dev/null && echo "  Restarted $vm (powered off by installer)"
            fi
        done

        # Patch CoreDNS to hostNetwork once worker joins
        for ORDER_NAME in $(oc get clusterorder -n "$OSAC_NAMESPACE" --no-headers -o custom-columns='NAME:.metadata.name' 2>/dev/null); do
            if ! echo "$COREDNS_PATCHED" | grep -qw "$ORDER_NAME"; then
                if patch_coredns "$ORDER_NAME"; then
                    COREDNS_PATCHED="$COREDNS_PATCHED $ORDER_NAME"
                fi
            fi
        done

        sleep 60; elapsed=$((elapsed + 60))
        echo "  ${elapsed}s — $name state: $state"
        print_progress

        if [ "$elapsed" -ge 3600 ]; then
            echo "ERROR: $name not ready after ${elapsed}s (state: $state)"
            exit 1
        fi
    done
done

info "All clusters ready"

# ---------- retrieve kubeconfigs ----------

info "Retrieving kubeconfigs..."
KUBECONFIG_DIR="/tmp/agentless-net-lab"
mkdir -p "$KUBECONFIG_DIR"
for i in "${!CLUSTER_NAMES[@]}"; do
    name="${CLUSTER_NAMES[$i]}"
    id="${CLUSTER_IDS[$i]}"
    osac get kubeconfig "$id" > "$KUBECONFIG_DIR/${name}.kubeconfig"
    echo "  $name: $KUBECONFIG_DIR/${name}.kubeconfig"
done

# ---------- validate clusters ----------

info "Validating clusters..."
for i in "${!CLUSTER_NAMES[@]}"; do
    name="${CLUSTER_NAMES[$i]}"
    KC="$KUBECONFIG_DIR/${name}.kubeconfig"
    NODE_COUNT=$(KUBECONFIG="$KC" oc get nodes --no-headers 2>/dev/null | wc -l)
    echo "  $name: $NODE_COUNT nodes"
    KUBECONFIG="$KC" oc get nodes -o wide 2>/dev/null
done

# ---------- test network isolation ----------

TEST_IMAGE="quay.io/openshift/origin-cli:4.17"

info "Deploying test pods..."
for i in "${!CLUSTER_NAMES[@]}"; do
    name="${CLUSTER_NAMES[$i]}"
    KC="$KUBECONFIG_DIR/${name}.kubeconfig"
    KUBECONFIG="$KC" oc run test-net --image="$TEST_IMAGE" \
        --restart=Never --command -- sleep 3600 2>/dev/null || true
done

info "Waiting for test pods to be ready..."
for i in "${!CLUSTER_NAMES[@]}"; do
    name="${CLUSTER_NAMES[$i]}"
    KC="$KUBECONFIG_DIR/${name}.kubeconfig"
    KUBECONFIG="$KC" oc wait pod/test-net --for=condition=Ready --timeout=120s
done

POD_IPS=()
for i in "${!CLUSTER_NAMES[@]}"; do
    name="${CLUSTER_NAMES[$i]}"
    KC="$KUBECONFIG_DIR/${name}.kubeconfig"
    ip=$(KUBECONFIG="$KC" oc get pod test-net -o jsonpath='{.status.podIP}')
    POD_IPS+=("$ip")
    echo "  $name test pod: $ip"
done

info "Testing self-connectivity (should pass)..."
for i in "${!CLUSTER_NAMES[@]}"; do
    name="${CLUSTER_NAMES[$i]}"
    KC="$KUBECONFIG_DIR/${name}.kubeconfig"
    if KUBECONFIG="$KC" oc exec test-net -- \
        curl -sk --connect-timeout 3 https://kubernetes.default.svc:443/healthz 2>&1 | grep -q "ok"; then
        echo "  $name: self-connectivity OK"
    else
        echo "ERROR: $name cannot reach its own API"
        exit 1
    fi
done

info "Testing cross-cluster isolation (should fail)..."
ISOLATED=true
for i in "${!CLUSTER_NAMES[@]}"; do
    name="${CLUSTER_NAMES[$i]}"
    KC="$KUBECONFIG_DIR/${name}.kubeconfig"
    for j in "${!CLUSTER_NAMES[@]}"; do
        [ "$i" = "$j" ] && continue
        other="${CLUSTER_NAMES[$j]}"
        other_ip="${POD_IPS[$j]}"
        if KUBECONFIG="$KC" oc exec test-net -- \
            curl -s --connect-timeout 3 "http://${other_ip}:8080" &>/dev/null; then
            echo "  ERROR: $name can reach $other ($other_ip) — isolation BROKEN"
            ISOLATED=false
        else
            echo "  $name cannot reach $other ($other_ip) — isolated OK"
        fi
    done
done

info "Cleaning up test pods..."
for i in "${!CLUSTER_NAMES[@]}"; do
    KC="$KUBECONFIG_DIR/${CLUSTER_NAMES[$i]}.kubeconfig"
    KUBECONFIG="$KC" oc delete pod test-net --ignore-not-found 2>/dev/null
done

if [ "$ISOLATED" = "false" ]; then
    echo "ERROR: Cross-cluster isolation test failed"
    exit 1
fi

info "E2E test complete."
