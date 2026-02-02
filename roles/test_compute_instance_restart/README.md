# test_compute_instance_restart

Ansible role for testing ComputeInstance restart functionality in OSAC.

## Overview

This role tests the VM restart feature (MGMT-22682) by:
1. Waiting for a ComputeInstance to reach Ready state
2. Recording the original VirtualMachineInstance (VMI) creation timestamp
3. Triggering a restart via the fulfillment-service API
4. Verifying the restart completed successfully:
   - `status.lastRestartedAt` is updated
   - `RestartInProgress` condition is set
   - A new VMI is created (with newer timestamp)
   - No `RestartFailed` condition exists

## Requirements

- ComputeInstance must already be created (typically by `test_compute_instance_creation` role)
- `compute_instance_order_uuid` variable must be set (contains the UUID of the ComputeInstance)
- Access to Kubernetes cluster with cloudkit-operator deployed
- kubectl with system:admin impersonation permissions
- fulfillment-service API access

## Dependencies

- `fulfillment_cli_base` - Provides gRPC token and base functionality

## Variables

### Required
- `compute_instance_order_uuid` - UUID of the ComputeInstance to restart
- `test_namespace` - Kubernetes namespace where the ComputeInstance exists

### Optional (with defaults)
- `compute_instance_template` - Template ID (default: `osac.templates.ocp_virt_vm`)
- `compute_instance_ready_timeout` - Timeout for waiting for Ready state in seconds (default: `900` / 15 minutes)
- `compute_instance_restart_timeout` - Timeout for restart completion in seconds (default: `300` / 5 minutes)
- `status_poll_interval` - Poll interval for status checks in seconds (default: `10`)

## Example Usage

### Standalone (requires existing ComputeInstance)
```yaml
- name: Test ComputeInstance restart
  ansible.builtin.include_role:
    name: test_compute_instance_restart
  vars:
    compute_instance_order_uuid: "abc-123-def-456"
    test_namespace: vmaas-dev
```

### Full test flow (recommended)
Use the `test_compute_instance_restart.yml` playbook which handles:
- Creation of test ComputeInstance
- Restart testing
- Cleanup

```bash
ansible-playbook playbooks/test_compute_instance_restart.yml -e test_namespace=vmaas-dev
```

## Verification Steps

The role performs these verification steps:

1. **Ready State**: Waits for ComputeInstance to reach `Ready` phase
2. **Original VMI**: Records the current VMI creation timestamp
3. **Restart Trigger**: Updates `spec.restartRequestedAt` via gRPC API
4. **Status Update**: Verifies `status.lastRestartedAt` is set
5. **Condition Check**: Verifies `RestartInProgress` condition is `True`
6. **New VMI**: Verifies a new VMI is created with a newer timestamp
7. **Failure Check**: Verifies no `RestartFailed` condition exists

## How It Works

### Restart Mechanism
The cloudkit-operator detects when `spec.restartRequestedAt` is set and newer than `status.lastRestartedAt`. When detected:
1. Controller deletes the VirtualMachineInstance
2. KubeVirt automatically recreates the VMI (restart)
3. Controller updates `status.lastRestartedAt` and sets `RestartInProgress` condition

### gRPC API Call
The role uses `grpcurl` to call the fulfillment-service API:
```bash
grpcurl -insecure \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"object": {"id": "...", "spec": {"restartRequestedAt": "2026-01-28T10:00:00Z"}}}' \
  fulfillment-api:8000 \
  fulfillment.v1.ComputeInstances/Update
```

## Test Tags

When used via the playbook:
- `info` - Display test information only
- `test` - Run the full test (creation + restart)
- `creation` - Run only ComputeInstance creation
- `restart` - Run only restart test (requires existing ComputeInstance)
- `cleanup` - Run only cleanup operations

## Troubleshooting

### ComputeInstance never reaches Ready state
- Check cloudkit-operator logs: `kubectl logs -n vmaas-dev deployment/cloudkit-operator-controller-manager`
- Verify webhook to AAP is working
- Check VirtualMachine creation in working namespace

### Restart doesn't trigger
- Verify `restartRequestedAt` was set: `kubectl get computeinstance -n vmaas-dev <name> -o jsonpath='{.spec.restartRequestedAt}'`
- Check cloudkit-operator logs for "New restart request detected"
- Verify gRPC API call succeeded (check `restart_trigger_result`)

### RestartFailed condition appears
- Check cloudkit-operator logs for error details
- Verify VirtualMachineReference exists in status
- Check if VMI can be deleted (RBAC permissions)

## Related

- Playbook: `playbooks/test_compute_instance_restart.yml`
- GitHub Issue: MGMT-22682
- cloudkit-operator PR: https://github.com/osac-project/osac-operator/pull/100
