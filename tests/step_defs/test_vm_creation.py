"""Step definitions for VM creation feature."""

import base64
import json
import re
import subprocess
import time

from pytest_bdd import given, when, then, scenarios, parsers

VM_READY_TIMEOUT = 600  # 10 minutes
VM_CHECK_INTERVAL = 30  # 30 seconds
SSH_TIMEOUT = 60  # seconds to wait for SSH connection
SSH_RETRY_INTERVAL = 10  # seconds between SSH retries

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


@when(parsers.parse('I create a VM with template "{template}" and parameters:'))
def create_vm_with_parameters(
    fulfillment_config, vm_context, created_vms, template, datatable
):
    """Create a VM with specified template and parameters from table."""
    cli_path = fulfillment_config["cli_path"]

    # Build command with template
    cmd = [cli_path, "create", "virtualmachine", "--template", template]

    # Add parameters from datatable (skip header row, skip empty values)
    for row in datatable[1:]:  # Skip header row
        name, value = row[0], row[1]
        if name and value:  # Only add non-empty parameters
            cmd.extend(["-p", f"{name}={value}"])

    result = subprocess.run(cmd, capture_output=True, text=True)
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


@when(parsers.parse('I create a VM with template "{template}" and generated SSH key'))
def create_vm_with_ssh_key(
    fulfillment_config, vm_context, created_vms, ssh_keypair, template
):
    """Create a VM with SSH public key parameter."""
    cli_path = fulfillment_config["cli_path"]
    public_key = ssh_keypair["public_key"]

    cmd = [
        cli_path, "create", "virtualmachine",
        "--template", template,
        "-p", f"ssh_public_key={public_key}",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"Failed to create VM: {result.stderr}"

    uuid_match = re.findall(r"'([^']+)'", result.stdout)
    assert uuid_match, f"Could not extract VM UUID from output: {result.stdout}"

    vm_uuid = uuid_match[0]
    vm_context["vm_uuid"] = vm_uuid
    vm_context["ssh_keypair"] = ssh_keypair
    created_vms.append(vm_uuid)


@when(parsers.parse('I create a VM with template "{template}" and cloud-init with SSH key'))
def create_vm_with_cloud_init(
    fulfillment_config, vm_context, created_vms, ssh_keypair, template
):
    """Create a VM with cloud-init configuration including SSH key."""
    cli_path = fulfillment_config["cli_path"]
    public_key = ssh_keypair["public_key"]

    # Create cloud-init user-data with SSH key
    cloud_init_config = f"""#cloud-config
users:
  - name: fedora
    ssh_authorized_keys:
      - {public_key}
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
"""
    cloud_init_b64 = base64.b64encode(cloud_init_config.encode()).decode()

    cmd = [
        cli_path, "create", "virtualmachine",
        "--template", template,
        "-p", f"cloud_init_config={cloud_init_b64}",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"Failed to create VM: {result.stderr}"

    uuid_match = re.findall(r"'([^']+)'", result.stdout)
    assert uuid_match, f"Could not extract VM UUID from output: {result.stdout}"

    vm_uuid = uuid_match[0]
    vm_context["vm_uuid"] = vm_uuid
    vm_context["ssh_keypair"] = ssh_keypair
    vm_context["ssh_user"] = "fedora"  # User created by cloud-init
    created_vms.append(vm_uuid)


@then("I should be able to SSH into the VM")
def verify_ssh_access(vm_context):
    """Verify SSH connectivity to the VM using virtctl ssh."""
    vm_uuid = vm_context["vm_uuid"]
    ssh_keypair = vm_context["ssh_keypair"]
    private_key_path = ssh_keypair["private_key_path"]

    # Find the KubeVirt VirtualMachine by fulfillment UUID label
    result = subprocess.run(
        [
            "oc", "get", "virtualmachine", "-A",
            "-l", f"cloudkit.openshift.io/virtualmachine-uuid={vm_uuid}",
            "-o", "jsonpath={.items[0].metadata.namespace}/{.items[0].metadata.name}",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Failed to find VM: {result.stderr}"
    assert result.stdout, f"No VirtualMachine found with UUID {vm_uuid}"

    ns_name = result.stdout.strip()
    namespace, vm_name = ns_name.split("/")

    # The VMI namespace follows pattern: {base-namespace}-{vm-name}
    vmi_namespace = f"{namespace}-{vm_name}"

    # Determine SSH user (fedora for cloud-init, default for ssh_public_key)
    ssh_user = vm_context.get("ssh_user", "fedora")

    # Try to SSH with virtctl (retries)
    start_time = time.time()
    last_error = None

    while time.time() - start_time < SSH_TIMEOUT:
        result = subprocess.run(
            [
                "virtctl", "ssh",
                "-n", vmi_namespace,
                "-i", private_key_path,
                "--known-hosts=/dev/null",
                f"--username={ssh_user}",
                f"vmi/{vm_name}",
                "--command", "echo 'SSH connection successful'",
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            assert "SSH connection successful" in result.stdout
            return

        last_error = result.stderr or result.stdout
        print(f"SSH attempt failed: {last_error}, retrying...")
        time.sleep(SSH_RETRY_INTERVAL)

    raise AssertionError(
        f"Failed to SSH into VM {vm_uuid} ({vm_name}) as {ssh_user}: {last_error}"
    )
