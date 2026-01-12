# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This repository contains Ansible-based test infrastructure for OSAC end-to-end testing. It focuses on testing hub creation and ComputeInstance lifecycle management through the OSAC fulfillment service using gRPC APIs and the fulfillment-cli tool.

## Architecture

### Directory Structure
- `playbooks/` - Main Ansible playbooks for executing tests
- `roles/` - Reusable Ansible roles for test functionality
  - `test_compute_instance_creation/` - Role for ComputeInstance creation, verification, and cleanup
  - `test_hub_creation/` - Role for hub creation, verification, and cleanup
  - `fulfillment_cli_base/` - Base role for fulfillment CLI operations (dependency)
- `inventory/` - Ansible inventory and group variables
  - `group_vars/all.yml` - Global configuration variables
- `retry/` - Ansible retry files for failed playbook runs

### Core Components

**Hub Creation Testing**
- Hub creation and registration using fulfillment-cli
- Hub verification through gRPC API calls (private.v1.Hubs/Get)
- Automated cleanup of hub resources and temporary files

**ComputeInstance Lifecycle Testing**
- ComputeInstance creation using fulfillment-cli with OSAC templates
- gRPC-based verification through the fulfillment service
- Status monitoring and readiness checks
- Automated cleanup and deletion

**Communication Methods**
- `fulfillment-cli` for hub/ComputeInstance creation operations
- `grpcurl` for direct API communication with fulfillment service
- gRPC authentication using Bearer tokens from OpenShift service accounts

## Development Commands

### Code Quality
```bash
# Run pre-commit hooks (yamllint, ansible-lint, etc.)
pre-commit run --all-files

# Run yamllint specifically
yamllint --strict *.yml *.yaml

# Run ansible-lint specifically
ansible-lint
```

### Running Tests

Execute hub creation test:
```bash
ansible-playbook playbooks/test_hub_creation.yml -e test_hub_id=my-test-hub-001 -e test_namespace=foobar
```

Execute ComputeInstance creation test:
```bash
ansible-playbook playbooks/test_compute_instance_creation.yml -e test_compute_instance_id=my-test-ci-001 -e test_namespace=foobar
```

### Test Tags

Use tags to run specific parts of tests:
- `info` - Display test information and resource listings
- `test` - Run actual creation tests (hub_creation, compute_instance_creation)
- `validation` - Resource verification and status checks
- `cleanup` - Resource deletion and file cleanup
- `mrclean` - Mass cleanup of test ComputeInstances with e2e-test-ci- prefix

Examples:
```bash
# Run only hub creation without cleanup
ansible-playbook playbooks/test_hub_creation.yml --tags test

# Run only ComputeInstance creation without cleanup
ansible-playbook playbooks/test_compute_instance_creation.yml --tags test

# Run only cleanup operations
ansible-playbook playbooks/test_compute_instance_creation.yml --tags cleanup

# Mass cleanup of all test ComputeInstances
ansible-playbook playbooks/test_compute_instance_creation.yml --tags mrclean
```

## Configuration

### Key Variables (inventory/group_vars/all.yml)

**Required for customization:**
- `cluster_domain_suffix` - OpenShift cluster domain (e.g., "apps.hcp.local.lab")
- `test_namespace` - Target namespace (default: "foobar")
- `fulfillment_cli_path` - Path to fulfillment-cli binary

**Auto-constructed addresses:**
- `fulfillment_address` - Constructed as `{app_name}-{namespace}.{domain}:{port}`
- `fulfillment_token_script` - OpenShift token creation command

**Hub creation specific:**
- `hub_service_account` - Service account for hub operations (default: "fulfillment-admin")
- `hub_token_duration` - Token validity period (default: "1h")

## ComputeInstance Template Configuration

Default ComputeInstance template: `cloudkit.templates.ocp_virt_vm`
- CPU: 2 cores
- Memory: 4GB
- Disk: 20GB
- Network: default
- Ready timeout: 15 minutes

## gRPC API Operations

**Hub Operations:**
- `private.v1.Hubs/Get` - Get specific hub details

**ComputeInstance Operations:**
- `private.v1.ComputeInstances/List` - List all ComputeInstances
- `private.v1.ComputeInstances/Get` - Get specific ComputeInstance details
- `private.v1.ComputeInstances/Delete` - Delete a ComputeInstance

All gRPC calls use insecure connections and require Bearer token authentication.

## Test Execution Flow

1. **Setup**: Display test information and parameters
2. **Creation**: Create hub/ComputeInstance using fulfillment-cli with specified parameters
3. **Verification**: Verify registration via gRPC API
4. **Monitoring**: Wait for resources to reach desired status
5. **Cleanup**: Delete resources and remove temporary files
6. **Logging**: Record test results to test-execution.log

## Error Handling

- Failed tests trigger automatic cleanup of temporary files
- Resource deletion verification ensures proper cleanup
- All operations include proper error logging and failure messages
- Retry functionality available through Ansible retry files in `retry/` directory
