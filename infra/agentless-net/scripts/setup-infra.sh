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

# ---------- cluster-tool ----------

if ! command -v cluster-tool &>/dev/null; then
    info "Installing cluster-tool..."
    # cluster-tool is a stdlib-only Python script (shebang #!/usr/bin/env
    # python3) distributed as a git repo, not a release binary -- install it
    # the same way scripts/machine-init.sh does. The previous
    # releases/latest/download URL pointed at a nonexistent repo, so curl
    # silently wrote the "Not Found" error page to the binary.
    CLUSTER_TOOL_DIR="/opt/cluster-tool"
    if [ ! -d "${CLUSTER_TOOL_DIR}/.git" ]; then
        git clone https://github.com/osac-project/cluster-tool.git "${CLUSTER_TOOL_DIR}"
    fi
    install -m 0755 "${CLUSTER_TOOL_DIR}/cluster-tool" /usr/local/bin/cluster-tool
fi

if ! cluster-tool servers 2>/dev/null | grep -q "local"; then
    info "Setting up cluster-tool local server..."
    cluster-tool connect local --host local --data-path /var/lib/cluster-tool
    # Full path: sudo's secure_path does not include /usr/local/bin, so
    # `sudo cluster-tool` fails with "command not found" even though the bare
    # calls above resolve fine (same reason bmaas uses an absolute path).
    sudo /usr/local/bin/cluster-tool setup client
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

# ---------- OpenShift / OSAC CLIs ----------
#
# The deploy/setup/test scripts call oc, osac, and (via osac-installer's
# `make install`) helm. Install them the same way scripts/machine-init.sh
# does. Idempotent -- each guarded by command -v.

if ! command -v oc &>/dev/null; then
    info "Installing oc/kubectl..."
    OC_URL="https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz"
    TMP_OC=$(mktemp -d)
    curl -sL "$OC_URL" | tar xz -C "$TMP_OC"
    install -m 0755 "$TMP_OC/oc" /usr/local/bin/oc
    install -m 0755 "$TMP_OC/kubectl" /usr/local/bin/kubectl 2>/dev/null || true
    rm -rf "$TMP_OC"
fi

if ! command -v osac &>/dev/null; then
    info "Installing osac CLI..."
    # osac-project/osac hosts releases for multiple components tagged
    # <component>/vX.Y.Z, so releases/latest can redirect to a different
    # component -- filter for fulfillment-service's own tag explicitly.
    OSAC_TAG=$(curl -sfL "https://api.github.com/repos/osac-project/osac/releases?per_page=100" \
        | jq -r '[.[] | select(.tag_name | test("^fulfillment-service/v[0-9]+\\.[0-9]+\\.[0-9]+$"))][0].tag_name // empty')
    if [ -z "$OSAC_TAG" ]; then
        echo "ERROR: no fulfillment-service release found on osac-project/osac" >&2
        exit 1
    fi
    curl -fL -o /usr/local/bin/osac \
        "https://github.com/osac-project/osac/releases/download/${OSAC_TAG}/osac_Linux_x86_64"
    chmod +x /usr/local/bin/osac
fi

if ! command -v helm &>/dev/null; then
    info "Installing helm..."
    HELM_VERSION="3.21.2"
    HELM_TARBALL="helm-v${HELM_VERSION}-linux-amd64.tar.gz"
    HELM_SHA256="0a745198de24545d0055cd8414bc8d2ba10363ef5f5d38369ea1b399671cc083"
    TMP_HELM=$(mktemp -d)
    curl -sL -o "${TMP_HELM}/${HELM_TARBALL}" "https://get.helm.sh/${HELM_TARBALL}"
    echo "${HELM_SHA256}  ${TMP_HELM}/${HELM_TARBALL}" | sha256sum -c -
    tar xzf "${TMP_HELM}/${HELM_TARBALL}" -C "$TMP_HELM"
    install -m 0755 "${TMP_HELM}/linux-amd64/helm" /usr/local/bin/helm
    rm -rf "$TMP_HELM"
fi

if ! command -v yq &>/dev/null; then
    info "Installing yq..."
    YQ_VERSION="v4.53.3"
    YQ_SHA256="fa52a4e758c63d38299163fbdd1edfb4c4963247918bf9c1c5d31d84789eded4"
    TMP_YQ=$(mktemp -d)
    curl -sL -o "${TMP_YQ}/yq" \
        "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_amd64"
    echo "${YQ_SHA256}  ${TMP_YQ}/yq" | sha256sum -c -
    install -m 0755 "${TMP_YQ}/yq" /usr/local/bin/yq
    rm -rf "$TMP_YQ"
fi

# The agentless-net flow needs a self-consistent SSH keypair on the box. The
# AAP runner SSHes into the servers (NMState live apply), the switches, and the
# net-node; setup-caas.sh puts ~/.ssh/id_rsa.pub into the InfraEnv's
# sshAuthorizedKey (so discovery agents trust it) and deploy-osac.sh injects the
# matching ~/.ssh/id_rsa as SERVER_SSH_KEY. Dev boxes already have one; the
# ephemeral EC2 box does not, so generate it here (idempotent — any
# self-consistent keypair works).
if [ ! -f "${HOME}/.ssh/id_rsa" ]; then
    info "Generating SSH keypair for AAP runner (NMState / switches / net-node)..."
    mkdir -p "${HOME}/.ssh"
    chmod 700 "${HOME}/.ssh"
    ssh-keygen -t rsa -b 4096 -N "" -f "${HOME}/.ssh/id_rsa" -q
fi

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
