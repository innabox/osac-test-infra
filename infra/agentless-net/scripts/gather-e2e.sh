#!/usr/bin/env bash
#
# Gather E2E test diagnostics from hosted clusters.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/.."
LOG_DIR="${INFRA_DIR}/logs"

mkdir -p "$LOG_DIR"

info() { echo "==> $*"; }

if [ ! -f "${INFRA_DIR}/.mgmt-network" ]; then
    info "No .mgmt-network — skipping"
    exit 0
fi

# shellcheck source=/dev/null
source "${INFRA_DIR}/.mgmt-network"
export KUBECONFIG

OSAC_NAMESPACE="${OSAC_NAMESPACE:-osac-e2e-ci}"

# ---------- cluster state ----------

info "Gathering hosted cluster diagnostics..."

# Mgmt-side hosted-cluster CRs whose .status.conditions carry the provisioning
# failure reason. They reference the pull secret by NAME (not exposed here);
# strip AgentClusterInstall's signed debugInfo URLs (eventsURL/logsURL tokens)
# that would otherwise land in this raw-uploaded dir. (Hardware-inventory CRs
# -- agents, infraenvs -- live in gather-infra.sh.)
oc get hostedclusters -A > "$LOG_DIR/hostedclusters.txt" 2>&1 || true
oc get hostedclusters -A -o yaml > "$LOG_DIR/hostedclusters.yaml" 2>&1 || true
oc get nodepools -A -o yaml > "$LOG_DIR/nodepools.yaml" 2>&1 || true
oc get agentclusterinstalls -A -o yaml 2>/dev/null \
    | yq 'del(.items[].status.debugInfo)' > "$LOG_DIR/agentclusterinstalls.yaml" 2>/dev/null || true

for order in $(oc get clusterorders -n "$OSAC_NAMESPACE" --no-headers -o custom-columns='NAME:.metadata.name' 2>/dev/null); do
    hc_ns="${OSAC_NAMESPACE}-${order}"
    kc_secret="${order}-admin-kubeconfig"
    kc_file=$(mktemp)
    oc get secret "$kc_secret" -n "$hc_ns" -o jsonpath='{.data.kubeconfig}' 2>/dev/null | base64 -d > "$kc_file"
    if [ -s "$kc_file" ]; then
        KUBECONFIG="$kc_file" oc get nodes -o wide > "$LOG_DIR/${order}-nodes.txt" 2>&1 || true
        KUBECONFIG="$kc_file" oc get co > "$LOG_DIR/${order}-clusteroperators.txt" 2>&1 || true
        KUBECONFIG="$kc_file" oc get pods -A --no-headers | grep -v Running | grep -v Completed > "$LOG_DIR/${order}-unhealthy-pods.txt" 2>&1 || true
    fi
    rm -f "$kc_file"
done

# ---------- SSH-to-agent repro (nmstate_config "Disable rp_filter" root cause) ----------
#
# osac.service.nmstate_config SSHes into each agent as
#   ssh -i <key> core@<agent-mgmt-ip> 'sudo sysctl ... rp_filter=0'
# under no_log:true, hiding the real error. Reproduce it to capture the exact
# stderr and tell auth/sudo/sysctl (box path, over the libvirt bridge) apart
# from pod-network reachability (in-cluster path, as AAP's EE actually runs it).
info "Reproducing AAP SSH-to-agent to surface the hidden nmstate error..."
ssh_opts="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=30 -o BatchMode=yes"
mgmt_iface="${AGENTLESS_NET_MGMT_INTERFACE:-ens4}"
agent_ips=$(oc get agents -A -o yaml 2>/dev/null \
    | yq ".items[].status.inventory.interfaces[] | select(.name == \"${mgmt_iface}\") | .ipV4Addresses[0]" 2>/dev/null \
    | cut -d/ -f1 | grep -viE '^(null)?$' | sort -u)
echo "agent ${mgmt_iface} IPs: ${agent_ips:-<none>}" > "$LOG_DIR/ssh-repro-summary.txt"

# (a) from the EC2 box (reaches agents over the libvirt bridge) -- auth/sudo/sysctl
while IFS= read -r ip; do
    [ -n "$ip" ] || continue
    # -n: don't read stdin, or ssh swallows the while-read herestring and only
    # the first agent gets tested.
    # shellcheck disable=SC2086
    ssh -n $ssh_opts -v -i "${HOME}/.ssh/id_rsa" "core@${ip}" \
        'sudo sysctl -qw net.ipv4.conf.all.rp_filter=0' \
        > "$LOG_DIR/ssh-repro-box-${ip}.txt" 2>&1 || true
done <<< "$agent_ips"

# (b) from a pod on the cluster OVN network -- the same path AAP's EE uses.
# Reuse the box key (~/.ssh/id_rsa): it IS the key AAP uses (it became
# SERVER_SSH_KEY) and is guaranteed present, unlike scraping it back out of a
# secret. Run in ansible-aap, where the EE job pods actually run, so egress /
# NetworkPolicy match.
if [ -n "$agent_ips" ] && [ -f "${HOME}/.ssh/id_rsa" ]; then
    repro_ns="ansible-aap"
    oc create secret generic ssh-repro-key -n "$repro_ns" --from-file=id="${HOME}/.ssh/id_rsa" \
        --dry-run=client -o yaml 2>/dev/null | oc apply -f - >/dev/null 2>&1 || true
    while IFS= read -r ip; do
        [ -n "$ip" ] || continue
        pod="ssh-repro-$(echo "$ip" | tr '.' '-')"
        oc delete pod "$pod" -n "$repro_ns" --ignore-not-found >/dev/null 2>&1 || true
        oc apply -f - >/dev/null 2>&1 <<EOF || true
apiVersion: v1
kind: Pod
metadata:
  name: ${pod}
  namespace: ${repro_ns}
spec:
  restartPolicy: Never
  containers:
  - name: ssh
    image: ghcr.io/osac-project/osac-aap:latest
    command:
    - sh
    - -c
    - cp /k/id /tmp/id && chmod 600 /tmp/id && ssh ${ssh_opts} -v -i /tmp/id core@${ip} 'sudo sysctl -qw net.ipv4.conf.all.rp_filter=0'
    volumeMounts:
    - name: k
      mountPath: /k
      readOnly: true
  volumes:
  - name: k
    secret:
      secretName: ssh-repro-key
EOF
        for _ in $(seq 1 20); do
            phase=$(oc get pod "$pod" -n "$repro_ns" -o jsonpath='{.status.phase}' 2>/dev/null || true)
            { [ "$phase" = "Succeeded" ] || [ "$phase" = "Failed" ]; } && break
            sleep 6
        done
        oc logs "$pod" -n "$repro_ns" > "$LOG_DIR/ssh-repro-pod-${ip}.txt" 2>&1 || true
        oc delete pod "$pod" -n "$repro_ns" --ignore-not-found >/dev/null 2>&1 || true
    done <<< "$agent_ips"
    oc delete secret ssh-repro-key -n "$repro_ns" --ignore-not-found >/dev/null 2>&1 || true
fi

info "Diagnostics saved to $LOG_DIR"
