#!/usr/bin/env bash
#
# Install prerequisites and cache the SNO snapshot flavor.
# Idempotent — safe to re-run.
#
set -euo pipefail

MGMT_IMAGE="${MGMT_IMAGE:-quay.io/osac-project/cluster-flavors:sno-4-22}"
FLAVOR_NAME="sno-4-22"

info() { echo "==> $*"; }

# ---------- Docker (needed by containerlab) ----------

if ! command -v docker &>/dev/null; then
    info "Installing Docker..."
    dnf install -y dnf-plugins-core
    dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    dnf install -y docker-ce docker-ce-cli containerd.io
    systemctl enable --now docker
else
    info "Docker already installed"
fi

# ---------- Containerlab ----------

if ! command -v containerlab &>/dev/null; then
    info "Installing containerlab..."
    bash -c "$(curl -sL https://get.containerlab.dev)"
else
    info "Containerlab already installed"
fi

# ---------- KVM / libvirt ----------

if ! command -v virsh &>/dev/null; then
    info "Installing libvirt/KVM..."
    dnf install -y qemu-kvm libvirt virt-install
    systemctl enable --now libvirtd
else
    info "libvirt already installed"
fi

# ---------- Ansible ----------

if ! command -v ansible-playbook &>/dev/null; then
    info "Installing Ansible..."
    dnf install -y ansible-core
fi

# ---------- OpenShift CLI (oc) ----------

if ! command -v oc &>/dev/null; then
    info "Installing OpenShift CLI..."
    TMP_OC=$(mktemp -d)
    curl -sL https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz \
        | tar xz -C "${TMP_OC}"
    install -m 0755 "${TMP_OC}/oc" /usr/local/bin/oc
    install -m 0755 "${TMP_OC}/kubectl" /usr/local/bin/kubectl 2>/dev/null || true
    rm -rf "${TMP_OC}"
else
    info "oc already installed"
fi

# ---------- cluster-tool ----------

CLUSTER_TOOL_DIR="/opt/cluster-tool"
CLUSTER_TOOL_BIN="/usr/local/bin/cluster-tool"

if [[ ! -x "${CLUSTER_TOOL_BIN}" ]]; then
    info "Installing cluster-tool..."
    if [[ -d "${CLUSTER_TOOL_DIR}" ]]; then
        git -C "${CLUSTER_TOOL_DIR}" pull --ff-only
    else
        git clone https://github.com/osac-project/cluster-tool.git "${CLUSTER_TOOL_DIR}"
    fi
    install -m 0755 "${CLUSTER_TOOL_DIR}/cluster-tool" "${CLUSTER_TOOL_BIN}"
fi

if ! "${CLUSTER_TOOL_BIN}" servers 2>/dev/null | grep -q "local"; then
    info "Setting up cluster-tool local server..."
    "${CLUSTER_TOOL_BIN}" connect local --host local --data-path /var/lib/cluster-tool
    sudo "${CLUSTER_TOOL_BIN}" setup client
else
    info "cluster-tool already configured"
fi

# ---------- Other tools ----------

for tool in sshpass envsubst jq; do
    if ! command -v "$tool" &>/dev/null; then
        info "Installing ${tool}..."
        dnf install -y "$tool" || pip3 install "$tool" 2>/dev/null || true
    fi
done

# ---------- Docker iptables workaround ----------
#
# Docker sets the iptables FORWARD chain policy to DROP.
# Libvirt creates NAT rules in nftables, but iptables and nftables are
# evaluated independently — Docker's DROP overrides libvirt's ACCEPT,
# leaving VMs with no internet access.

if iptables -S FORWARD 2>/dev/null | grep -q "\-P FORWARD DROP"; then
    if ! iptables -C FORWARD -s 192.168.0.0/16 -j ACCEPT 2>/dev/null; then
        info "Adding iptables FORWARD rules for libvirt VMs..."
        iptables -I FORWARD -s 192.168.0.0/16 -j ACCEPT
        iptables -I FORWARD -d 192.168.0.0/16 -j ACCEPT
        iptables -I FORWARD -s 10.0.0.0/8 -j ACCEPT
        iptables -I FORWARD -d 10.0.0.0/8 -j ACCEPT
    fi
fi

# ---------- Pull snapshot flavor ----------

if cluster-tool flavors 2>/dev/null | grep -q "$FLAVOR_NAME"; then
    info "Snapshot flavor '${FLAVOR_NAME}' already cached"
else
    info "Pulling snapshot flavor '${FLAVOR_NAME}'..."
    cluster-tool pull "$MGMT_IMAGE"
fi

info "setup-infra complete."
