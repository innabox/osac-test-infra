"""Shared pytest fixtures for OSAC BDD tests."""

import base64
import json
import os
import subprocess
import tempfile

import httpx
import pytest
import yaml
from kubernetes import client, config

TEST_HUB_PREFIX = "e2e-test-hub-"
TEST_VM_PREFIX = "e2e-test-vm-"
VM_TEMPLATE = "osac.templates.ocp_virt_vm"
VM_READY_TIMEOUT = 600  # 10 minutes
VM_CHECK_INTERVAL = 30  # 30 seconds


@pytest.fixture(scope="session")
def fulfillment_config():
    """Configuration for the fulfillment service."""
    namespace = os.environ.get("TEST_NAMESPACE", "foobar")
    cluster_domain = os.environ.get("CLUSTER_DOMAIN_SUFFIX", "apps.hcp.local.lab")
    app_name = os.environ.get("FULFILLMENT_APP_NAME", "fulfillment-api")
    port = os.environ.get("FULFILLMENT_PORT", "443")

    return {
        "namespace": namespace,
        "cluster_domain": cluster_domain,
        "address": f"{app_name}-{namespace}.{cluster_domain}:{port}",
        "cli_path": os.environ.get("FULFILLMENT_CLI_PATH", "fulfillment-cli"),
        "keycloak_url": f"https://keycloak-keycloak.{cluster_domain}",
    }


@pytest.fixture(scope="session")
def keycloak_token(fulfillment_config):
    """Authenticate with Keycloak and return access token.

    Also writes the fulfillment-cli config file for CLI authentication.
    """
    username = os.environ.get("KEYCLOAK_USERNAME")
    password = os.environ.get("KEYCLOAK_PASSWORD")

    if not username or not password:
        pytest.fail("KEYCLOAK_USERNAME and KEYCLOAK_PASSWORD must be set")

    token_url = f"{fulfillment_config['keycloak_url']}/realms/innabox/protocol/openid-connect/token"

    response = httpx.post(
        token_url,
        data={
            "client_id": "fulfillment-cli",
            "username": username,
            "password": password,
            "grant_type": "password",
            "scope": "openid groups username",
        },
        verify=False,
    )

    if response.status_code != 200:
        pytest.fail(f"Keycloak authentication failed: {response.text}")

    token_data = response.json()
    access_token = token_data.get("access_token")

    if not access_token:
        pytest.fail("No access_token in Keycloak response")

    # Write fulfillment-cli config file for CLI authentication
    config_dir = os.path.expanduser("~/.config/fulfillment-cli")
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "config.json")

    import json
    from datetime import datetime, timedelta, timezone

    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    cli_config = {
        "access_token": access_token,
        "refresh_token": "",
        "insecure": True,
        "address": fulfillment_config["address"],
        "token_expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    with open(config_path, "w") as f:
        json.dump(cli_config, f)

    return access_token


@pytest.fixture(scope="session")
def grpc_token(keycloak_token):
    """Alias for keycloak_token for gRPC calls."""
    return keycloak_token


@pytest.fixture(scope="session")
def hub_kubeconfig(fulfillment_config):
    """Generate kubeconfig for hub creation by extracting hub-access secret."""
    namespace = fulfillment_config["namespace"]

    # Load kubeconfig and create API client
    config.load_kube_config()
    v1 = client.CoreV1Api()

    # Get cluster server URL from current context
    _, active_context = config.list_kube_config_contexts()
    cluster_name = active_context["context"]["cluster"]

    # Load kubeconfig to get server URL
    kubeconfig_file = os.environ.get("KUBECONFIG", os.path.expanduser("~/.kube/config"))
    with open(kubeconfig_file) as f:
        kube_config = yaml.safe_load(f)

    server_url = None
    for cluster in kube_config.get("clusters", []):
        if cluster["name"] == cluster_name:
            server_url = cluster["cluster"]["server"]
            break

    if not server_url:
        pytest.fail(f"Could not find server URL for cluster {cluster_name}")

    # Extract server name for context naming
    server_name = server_url.split("//")[1].split(".")[0] if "//" in server_url else "cluster"

    # Get token from hub-access secret
    try:
        secret = v1.read_namespaced_secret("hub-access", namespace)
        token_b64 = secret.data.get("token")
        if not token_b64:
            pytest.fail(f"Secret hub-access in {namespace} has no 'token' key")
        token = base64.b64decode(token_b64).decode("utf-8")
    except client.exceptions.ApiException as e:
        pytest.fail(f"Failed to read hub-access secret: {e}")

    # Generate kubeconfig
    kubeconfig_content = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{
            "name": server_name,
            "cluster": {
                "server": server_url,
                "insecure-skip-tls-verify": True,
            },
        }],
        "contexts": [{
            "name": server_name,
            "context": {
                "cluster": server_name,
                "namespace": namespace,
                "user": f"system:serviceaccount:{namespace}:hub-access",
            },
        }],
        "current-context": server_name,
        "users": [{
            "name": f"system:serviceaccount:{namespace}:hub-access",
            "user": {
                "token": token,
            },
        }],
    }

    # Write to temp file
    fd, kubeconfig_path = tempfile.mkstemp(prefix="hub-kubeconfig-", suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        yaml.dump(kubeconfig_content, f)

    yield kubeconfig_path

    # Cleanup
    if os.path.exists(kubeconfig_path):
        os.remove(kubeconfig_path)


@pytest.fixture
def created_hubs():
    """Track created hubs for cleanup."""
    hubs = []
    yield hubs


@pytest.fixture
def hub_context():
    """Shared context for hub creation steps."""
    return {}


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_hubs(request, fulfillment_config, grpc_token):
    """Clean up all test hubs at the end of the test session."""
    yield

    # List all hubs
    address = fulfillment_config["address"]
    result = subprocess.run(
        [
            "grpcurl", "-insecure",
            "-H", f"Authorization: Bearer {grpc_token}",
            address,
            "private.v1.Hubs/List",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Warning: Failed to list hubs for cleanup: {result.stderr}")
        return

    try:
        hubs_data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Warning: Failed to parse hubs list: {result.stdout}")
        return

    # Find and delete test hubs
    hubs = hubs_data.get("items", [])
    test_hubs = [h for h in hubs if h.get("id", "").startswith(TEST_HUB_PREFIX)]

    for hub in test_hubs:
        hub_id = hub["id"]
        delete_result = subprocess.run(
            [
                "grpcurl", "-insecure",
                "-H", f"Authorization: Bearer {grpc_token}",
                "-d", json.dumps({"id": hub_id}),
                address,
                "private.v1.Hubs/Delete",
            ],
            capture_output=True,
            text=True,
        )
        if delete_result.returncode == 0:
            print(f"Cleaned up test hub: {hub_id}")
        else:
            print(f"Warning: Failed to delete hub {hub_id}: {delete_result.stderr}")


# VM-specific fixtures


@pytest.fixture
def vm_context():
    """Shared context for VM creation steps."""
    return {}


@pytest.fixture
def created_vms():
    """Track created VMs for cleanup."""
    vms = []
    yield vms


@pytest.fixture
def ssh_keypair():
    """Generate a temporary SSH key pair for testing."""
    key_dir = tempfile.mkdtemp(prefix="ssh-test-")
    private_key_path = os.path.join(key_dir, "id_rsa")
    public_key_path = os.path.join(key_dir, "id_rsa.pub")

    # Generate SSH key pair using ssh-keygen
    result = subprocess.run(
        [
            "ssh-keygen", "-t", "rsa", "-b", "2048",
            "-f", private_key_path,
            "-N", "",  # No passphrase
            "-C", "test@osac-e2e",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to generate SSH key: {result.stderr}")

    # Read the public key
    with open(public_key_path) as f:
        public_key = f.read().strip()

    yield {
        "private_key_path": private_key_path,
        "public_key_path": public_key_path,
        "public_key": public_key,
        "key_dir": key_dir,
    }

    # Cleanup
    import shutil
    shutil.rmtree(key_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def ensure_hub_exists(fulfillment_config, hub_kubeconfig, grpc_token):
    """Ensure a hub exists for VM creation. Create one if needed."""
    address = fulfillment_config["address"]
    cli_path = fulfillment_config["cli_path"]
    namespace = fulfillment_config["namespace"]

    # Check if any hub exists via gRPC
    result = subprocess.run(
        [
            "grpcurl", "-insecure",
            "-H", f"Authorization: Bearer {grpc_token}",
            address,
            "private.v1.Hubs/List",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        try:
            hubs = json.loads(result.stdout).get("items", [])
            if hubs:
                return hubs[0]["id"]  # Use existing hub
        except json.JSONDecodeError:
            pass

    # Create a test hub for VM tests
    hub_id = "e2e-test-hub-for-vms"
    create_result = subprocess.run(
        [
            cli_path, "create", "hub",
            "--id", hub_id,
            "--namespace", namespace,
            "--kubeconfig", hub_kubeconfig,
        ],
        capture_output=True,
        text=True,
    )
    if create_result.returncode != 0:
        pytest.fail(f"Failed to create hub for VM tests: {create_result.stderr}")

    return hub_id


@pytest.fixture(scope="session")
def ensure_template_exists(fulfillment_config, keycloak_token):
    """Verify the VM template exists."""
    cli_path = fulfillment_config["cli_path"]
    template = VM_TEMPLATE

    result = subprocess.run(
        [cli_path, "get", "virtualmachinetemplates"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        pytest.fail(f"Failed to list templates: {result.stderr}")

    if template not in result.stdout:
        pytest.fail(f"Template '{template}' not found. Available: {result.stdout}")

    return template


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_vms(request, fulfillment_config, grpc_token):
    """Clean up all test VMs at the end of the test session."""
    yield

    # List all VMs
    address = fulfillment_config["address"]
    cli_path = fulfillment_config["cli_path"]
    result = subprocess.run(
        [
            "grpcurl", "-insecure",
            "-H", f"Authorization: Bearer {grpc_token}",
            address,
            "private.v1.VirtualMachines/List",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Warning: Failed to list VMs for cleanup: {result.stderr}")
        return

    try:
        vms_data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Warning: Failed to parse VMs list: {result.stdout}")
        return

    # Find and delete test VMs (those created by tests have UUIDs tracked)
    # For safety, we delete all VMs as they are test resources
    vms = vms_data.get("items", [])
    for vm in vms:
        vm_id = vm.get("id", "")
        if not vm_id:
            continue

        delete_result = subprocess.run(
            [cli_path, "delete", "virtualmachine", vm_id],
            capture_output=True,
            text=True,
        )
        if delete_result.returncode == 0:
            print(f"Cleaned up test VM: {vm_id}")
        else:
            print(f"Warning: Failed to delete VM {vm_id}: {delete_result.stderr}")
