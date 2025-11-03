# OSAC Test Infrastructure

Ansible-based test infrastructure for OSAC end-to-end testing.

## Overview

This repository contains automated testing capabilities for OSAC hub creation and management through the OSAC fulfillment service.

## Architecture

### Directory Structure
```
playbooks/          # Main Ansible playbooks for executing tests
roles/              # Reusable Ansible roles for test functionality
├── fulfillment_cli_base/     # Base role for fulfillment CLI operations
├── test_hub_creation/        # Hub creation/deletion testing
```

### Core Components

**Hub Creation Testing**
- Hub creation and registration using fulfillment-cli
- Hub verification through gRPC API calls
- Automated cleanup of hub resources and temporary files

**Communication Methods**
- `fulfillment-cli` for hub creation operations
- `grpcurl` for direct API communication with fulfillment service
- gRPC authentication using Bearer tokens

## Quick Start

### Prerequisites
- Ansible installed
- Access to OSAC fulfillment service
- `fulfillment-cli` binary
- `grpcurl` for gRPC API calls

### Running Tests

Execute hub creation test:
```bash
ansible-playbook playbooks/test_hub_creation.yml
```

## Configuration

### Variables (Found in group_vars/all.yml)

- KUBECONFIG: Path to your KUBECONFIG
- fulfillment_cli_path: Where to find fulfillment-cli
- test_namespace: What namespace to use
- testing_workspace: What directory to put your test results in
- cluster_domain_suffix: The service dns entry for your cluster. (ie apps.hcp.local.lab)
- fulfillment_app_name: Kubernetes app name for your fulfillment api service.
- fulfillment_port: Listening port for your fulfillment-api

- osac_installer_fulfillment_address: "{{ fulfillment_app_name }}-{{ test_namespace }}.{{ cluster_domain_suffix }}:{{ fulfillment_port }}"
- fulfillment_token_script: "oc create token fulfillment-admin -n {{ test_namespace }} --duration 1h --as system:admin"

### Hub creation specific overrides
- hub_service_account: "fulfillment-admin"
- hub_token_namespace: "{{ test_namespace }}"
- hub_token_duration: "1h"


### Fulfillment Address Configuration

The fulfillment address follows standard OpenShift application naming:
```
<app-name>-<namespace>.<cluster-domain>:<port>
```

Configuration variables:
- `fulfillment_app_name` - Application name (default: "fulfillment-api")
- `test_namespace` - Target namespace
- `cluster_domain_suffix` - User-configurable cluster domain (e.g., "apps.hcp.local.lab")
- `fulfillment_port` - Service port (default: "443")

The complete address is automatically constructed as:
```
fulfillment-api-foobar.apps.hcp.local.lab:443
```

## Test Execution Flow

1. **Setup**: Display test information and parameters
2. **Creation**: Create hub using fulfillment-cli
3. **Verification**: Verify hub registration via gRPC API
4. **Cleanup**: Delete hub and remove temporary files
5. **Logging**: Record test results to test-execution.log

## Error Handling

- Hub deletion verification ensures proper cleanup
- All operations include proper error logging and failure messages
- Retry functionality available through Ansible retry files
