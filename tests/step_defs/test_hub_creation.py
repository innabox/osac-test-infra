"""Step definitions for hub creation feature."""

import json
import subprocess
import time

from pytest_bdd import given, when, then, scenarios, parsers

scenarios("../features/hub_creation.feature")


@given("the fulfillment service is accessible")
def verify_fulfillment_service(fulfillment_config):
    """Verify the fulfillment CLI is available."""
    cli_path = fulfillment_config["cli_path"]
    result = subprocess.run([cli_path, "--help"], capture_output=True, text=True)
    assert result.returncode == 0, f"fulfillment-cli not accessible: {result.stderr}"


@given("I am authenticated with the fulfillment CLI")
def verify_authenticated(keycloak_token):
    """Verify Keycloak authentication succeeded."""
    assert keycloak_token, "Keycloak authentication failed"


@when("I create a hub with a unique ID")
def create_hub_unique_id(fulfillment_config, hub_kubeconfig, hub_context, created_hubs):
    """Create a hub with a unique ID."""
    hub_id = f"e2e-test-hub-{int(time.time())}"
    namespace = fulfillment_config["namespace"]
    hub_context["hub_id"] = hub_id
    hub_context["namespace"] = namespace

    cli_path = fulfillment_config["cli_path"]

    result = subprocess.run(
        [cli_path, "create", "hub",
         "--id", hub_id,
         "--namespace", namespace,
         "--kubeconfig", hub_kubeconfig],
        capture_output=True,
        text=True,
    )
    hub_context["creation_result"] = result
    created_hubs.append({"id": hub_id, "namespace": namespace})

    assert result.returncode == 0, f"Failed to create hub: {result.stderr}"


@when(parsers.parse('I create a hub with ID "{hub_id}"'))
def create_hub_specific_id(fulfillment_config, hub_kubeconfig, hub_context, created_hubs, hub_id):
    """Create a hub with a specific ID."""
    namespace = fulfillment_config["namespace"]
    hub_context["hub_id"] = hub_id
    hub_context["namespace"] = namespace

    cli_path = fulfillment_config["cli_path"]

    result = subprocess.run(
        [cli_path, "create", "hub",
         "--id", hub_id,
         "--namespace", namespace,
         "--kubeconfig", hub_kubeconfig],
        capture_output=True,
        text=True,
    )
    hub_context["creation_result"] = result
    created_hubs.append({"id": hub_id, "namespace": namespace})

    assert result.returncode == 0, f"Failed to create hub: {result.stderr}"


@then("the hub should be registered in the fulfillment service")
def verify_hub_registered(fulfillment_config, grpc_token, hub_context):
    """Verify the hub is registered via gRPC."""
    hub_id = hub_context["hub_id"]
    address = fulfillment_config["address"]

    result = subprocess.run(
        ["grpcurl", "-insecure",
         "-H", f"Authorization: Bearer {grpc_token}",
         "-d", json.dumps({"id": hub_id}),
         address,
         "private.v1.Hubs/Get"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Failed to get hub via gRPC: {result.stderr}"

    hub_data = json.loads(result.stdout)
    hub_context["hub_data"] = hub_data
    assert "object" in hub_data, "Hub data missing 'object' field"


@then("the hub details should be retrievable via gRPC")
def verify_hub_details_retrievable(hub_context):
    """Verify hub details are present."""
    hub_data = hub_context.get("hub_data", {})
    assert "object" in hub_data, "Hub details not retrievable"
    assert "id" in hub_data["object"], "Hub ID not in response"


@then(parsers.parse('the hub ID should be "{expected_id}"'))
def verify_hub_id(hub_context, expected_id):
    """Verify the hub ID matches expected value."""
    hub_data = hub_context.get("hub_data", {})
    actual_id = hub_data.get("object", {}).get("id")
    assert actual_id == expected_id, f"Expected hub ID '{expected_id}', got '{actual_id}'"
