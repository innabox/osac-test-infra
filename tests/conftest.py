"""Shared pytest fixtures for OSAC BDD tests."""

import base64
import os
import tempfile

import httpx
import pytest
import yaml
from kubernetes import client, config


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
