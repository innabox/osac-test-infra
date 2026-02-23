# ComputeInstance API Fields Test Role

## Overview

This role tests the new explicit API fields introduced in MGMT-23103 for ComputeInstance resources. The test creates a ComputeInstance using explicit API fields (not templateParameters), then validates both field mutability and immutability.

## Tests Performed

### 1. ComputeInstance Creation
- **Test**: Create ComputeInstance using explicit API fields
  - Uses `image`, `cores`, `memoryGiB`, `bootDisk`, `runStrategy` fields
  - Does NOT use `templateParameters` (validates new API approach)
  - Verifies provision job succeeds
  - Verifies ComputeInstance reaches Running state

### 2. runStrategy Mutability Tests
- **Test**: Change runStrategy from `Always` to `Halted`
  - Verifies VirtualMachine stops (`spec.runStrategy=Halted`, `status=Stopped`)
  - Verifies VirtualMachineInstance is terminated

- **Test**: Change runStrategy from `Halted` to `Always`
  - Verifies VirtualMachine starts (`spec.runStrategy=Always`, `status=Running`)
  - Verifies new VirtualMachineInstance is created and running

### 3. Immutability Validation Tests
- **Test**: Attempt to change `cores` field
  - Verifies patch is rejected with "cores is immutable" error

- **Test**: Attempt to change `memoryGiB` field
  - Verifies patch is rejected with "memoryGiB is immutable" error

- **Test**: Attempt to change `image.sourceRef` field
  - Verifies patch is rejected with "image is immutable" error

### 4. Cleanup
- **Test**: Delete the test ComputeInstance
  - Verifies resource is removed from the cluster

## Prerequisites

- Variable `test_namespace` must be set
- Namespace must exist and be accessible

## Usage

### Using the dedicated playbook

```bash
ansible-playbook playbooks/test_compute_instance_api_fields.yml \
  -e test_namespace=vmaas-dev
```

### As part of another playbook

```yaml
- name: Run API fields test
  ansible.builtin.include_role:
    name: test_compute_instance_api_fields
  vars:
    test_namespace: vmaas-dev
```

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `test_instance_name` | `e2e-test-api-fields-<random>` | Name for test ComputeInstance |
| `test_image_ref` | `quay.io/fedora/fedora-coreos:stable` | OCI image reference |
| `test_cores` | 2 | CPU cores |
| `test_memory_gib` | 4 | Memory in GiB |
| `test_boot_disk_size` | 20 | Boot disk size in GiB |
| `status_poll_interval` | 10 | Seconds between status checks |
| `vm_state_change_timeout` | 120 | Max seconds to wait for VM state changes |
| `provision_timeout` | 600 | Max seconds to wait for provisioning |

## Expected Outcomes

All tests should PASS:
- ✓ ComputeInstance created with explicit API fields
- ✓ Provision job succeeds
- ✓ ComputeInstance reaches Running state
- ✓ VM stops when runStrategy=Halted
- ✓ VM starts when runStrategy=Always
- ✓ Immutable fields reject patch attempts
- ✓ ComputeInstance cleanup succeeds

## Related

- Issue: https://issues.redhat.com/browse/MGMT-23103
- Enhancement Proposal: https://github.com/osac-project/enhancement-proposals/pull/21
- Pull Request: https://github.com/osac-project/osac-operator/pull/111
