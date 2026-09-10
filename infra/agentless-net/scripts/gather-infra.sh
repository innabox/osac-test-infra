#!/usr/bin/env bash
#
# Gather infrastructure diagnostics for debugging.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/.."
LOG_DIR="${INFRA_DIR}/logs"

mkdir -p "$LOG_DIR"

info() { echo "==> $*"; }

# ---------- containerlab state ----------

info "Gathering containerlab state..."
sudo containerlab inspect -n agentless-net-lab 2>&1 | tee "$LOG_DIR/containerlab-inspect.txt" >/dev/null || true

# ---------- switch state ----------

for sw in clab-agentless-net-lab-leaf-1 clab-agentless-net-lab-leaf-2; do
    docker exec "$sw" nv show bridge domain br_default vlan > "$LOG_DIR/${sw}-vlans.txt" 2>&1 || true
    docker exec "$sw" nv show interface > "$LOG_DIR/${sw}-interfaces.txt" 2>&1 || true
done

# ---------- net-node state ----------

docker exec clab-agentless-net-lab-net-node ip addr > "$LOG_DIR/net-node-ip-addr.txt" 2>&1 || true
docker exec clab-agentless-net-lab-net-node ip route > "$LOG_DIR/net-node-ip-route.txt" 2>&1 || true
docker exec clab-agentless-net-lab-net-node ip netns list > "$LOG_DIR/net-node-namespaces.txt" 2>&1 || true
docker exec clab-agentless-net-lab-net-node iptables -t nat -L -n > "$LOG_DIR/net-node-nat.txt" 2>&1 || true

# ---------- VM state ----------

virsh list --all > "$LOG_DIR/virsh-list.txt" 2>&1 || true
for vm in host-1 host-2; do
    virsh domifaddr "$vm" > "$LOG_DIR/${vm}-ifaddr.txt" 2>&1 || true
done

# ---------- management cluster ----------

if [ -f "${INFRA_DIR}/.mgmt-network" ]; then
    # shellcheck source=/dev/null
    source "${INFRA_DIR}/.mgmt-network"
    export KUBECONFIG
    oc get pods -n "${OSAC_NAMESPACE:-osac-e2e-ci}" > "$LOG_DIR/osac-pods.txt" 2>&1 || true
    oc get agents -n hardware-inventory -o wide > "$LOG_DIR/agents.txt" 2>&1 || true
    oc get clusterorders -n "${OSAC_NAMESPACE:-osac-e2e-ci}" > "$LOG_DIR/clusterorders.txt" 2>&1 || true

    # Full YAML of the hardware-inventory CRs whose .status carries the agent
    # discovery state. They reference the pull secret by NAME (not exposed
    # here); strip the assisted-installer signed URLs (Agent debugInfo
    # eventsURL/logsURL, InfraEnv ISO) whose tokens would otherwise land in
    # this raw-uploaded dir. (Hosted-cluster CRs live in gather-e2e.sh.)
    oc get agents -A -o yaml 2>/dev/null \
        | yq 'del(.items[].status.debugInfo)' > "$LOG_DIR/agents.yaml" 2>/dev/null || true
    oc get infraenvs -A -o yaml 2>/dev/null \
        | yq 'del(.items[].status.bootArtifacts) | del(.items[].status.isoDownloadURL)' > "$LOG_DIR/infraenvs.yaml" 2>/dev/null || true
fi

info "Diagnostics saved to $LOG_DIR"
