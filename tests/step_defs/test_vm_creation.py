"""Step definitions for VM creation feature."""

import json
import re
import subprocess
import time

from pytest_bdd import given, when, then, scenarios, parsers

VM_READY_TIMEOUT = 600  # 10 minutes
VM_CHECK_INTERVAL = 30  # 30 seconds

scenarios("../features/vm_creation.feature")


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


@given("a hub is available")
def hub_available(ensure_hub_exists):
    """Ensure hub exists for VM creation."""
    assert ensure_hub_exists, "No hub available for VM creation"


@given("the VM template exists")
def template_available(ensure_template_exists):
    """Ensure the VM template exists."""
    assert ensure_template_exists, "VM template not available"


@when(parsers.parse('I create a VM with template "{template}"'))
def create_vm_with_template(fulfillment_config, vm_context, created_vms, template):
    """Create a VM with specified template."""
    cli_path = fulfillment_config["cli_path"]

    result = subprocess.run(
        [cli_path, "create", "virtualmachine", "--template", template],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Failed to create VM: {result.stderr}"

    # Extract UUID from output (format: 'uuid-here')
    uuid_match = re.findall(r"'([^']+)'", result.stdout)
    assert uuid_match, f"Could not extract VM UUID from output: {result.stdout}"

    vm_uuid = uuid_match[0]
    vm_context["vm_uuid"] = vm_uuid
    created_vms.append(vm_uuid)


@then("the VM should be registered in the fulfillment service")
def verify_vm_registered(fulfillment_config, grpc_token, vm_context):
    """Verify VM appears in gRPC list."""
    vm_uuid = vm_context["vm_uuid"]
    address = fulfillment_config["address"]

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
    assert result.returncode == 0, f"Failed to list VMs via gRPC: {result.stderr}"

    vms_data = json.loads(result.stdout)
    vm_ids = [vm.get("id") for vm in vms_data.get("items", [])]
    assert vm_uuid in vm_ids, f"VM {vm_uuid} not found in list: {vm_ids}"

    vm_context["vm_data"] = next(
        (vm for vm in vms_data.get("items", []) if vm.get("id") == vm_uuid),
        None,
    )


@then(parsers.parse("the VM should reach ready state within {minutes:d} minutes"))
def wait_for_vm_ready(fulfillment_config, grpc_token, vm_context, minutes):
    """Poll until VM state is VIRTUAL_MACHINE_STATE_READY."""
    vm_uuid = vm_context["vm_uuid"]
    address = fulfillment_config["address"]
    timeout = min(minutes * 60, VM_READY_TIMEOUT)
    interval = VM_CHECK_INTERVAL

    start_time = time.time()
    last_state = None

    while time.time() - start_time < timeout:
        result = subprocess.run(
            [
                "grpcurl", "-insecure",
                "-H", f"Authorization: Bearer {grpc_token}",
                "-d", json.dumps({"id": vm_uuid}),
                address,
                "private.v1.VirtualMachines/Get",
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            print(f"Warning: Failed to get VM status: {result.stderr}")
            time.sleep(interval)
            continue

        try:
            vm_data = json.loads(result.stdout)
            vm_object = vm_data.get("object", {})
            status = vm_object.get("status", {})
            state = status.get("state", "")
            last_state = state

            if state == "VIRTUAL_MACHINE_STATE_READY":
                vm_context["vm_data"] = vm_object
                return

            print(f"VM {vm_uuid} state: {state}, waiting...")
        except json.JSONDecodeError:
            print(f"Warning: Failed to parse VM response: {result.stdout}")

        time.sleep(interval)

    elapsed = time.time() - start_time
    raise AssertionError(
        f"VM {vm_uuid} did not reach ready state within {minutes} minutes. "
        f"Last state: {last_state}, elapsed: {elapsed:.0f}s"
    )
