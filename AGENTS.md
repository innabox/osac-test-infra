# AGENTS.md

> [!IMPORTANT]
> **The e2e test suite has moved (OSAC-3593).** Test suites now live in the
> [osac mono-repo](https://github.com/osac-project/osac). **Do not add, modify,
> or delete tests in this repository** — make those changes in the mono-repo
> instead. The test-writing guidance below is retained for reference but applies
> to the suites in their new mono-repo home.

AI agent documentation for osac-test-infra. Read this before working in this repository.

## What This Repo Does

Integration and E2E test infrastructure for OSAC. Tests exercise the fulfillment API (gRPC/REST), Kubernetes CRs, and multi-cluster provisioning flows. Tests run in containers with pre-installed tools (oc, kubectl, grpcurl, osac CLI) against live OSAC deployments.

**Primary focus**: End-to-end validation of VMaaS (ComputeInstance), CaaS (ClusterOrder), storage, and catalog APIs.

## Quick Start

```bash
uv sync
make test-vmaas
```

See [Running Tests Locally](#running-tests-locally) below for the full command reference
(all suites, single-test filters, sequential debugging runs) and required environment variables.

## Infrastructure Backend Layer

The repo has two layers, connected by a **contract** (see [`infra/contract.md`](infra/contract.md)
and [README.md](README.md) for the human-facing overview):

- **`infra/<backend>/`** — pluggable infrastructure backends that provision a cluster and deploy
  OSAC. Each backend implements `Makefile` (targets `setup-infra`, `deploy-infra`, `deploy-osac`,
  `setup-<suite>`, `destroy-osac`, `destroy-infra`, `gather-infra`, `gather-<suite>`) and a
  `capabilities` file declaring `SUPPORTED_SUITES`. `deploy-osac` writes `.env.infra` with the
  cluster config tests need. Currently only `infra/netris/` is implemented (`SUPPORTED_SUITES="caas bmaas bmaas_networking"`
  per `infra/netris/capabilities`).
- **`tests/<suite>/`** — the pytest suites documented throughout this file. They are
  infrastructure-agnostic: they only consume environment variables, never which backend provisioned
  the cluster. **This layer is unaffected by the infra-backend layer** — the "run tests against an
  existing cluster" workflow (`uv sync && make test-vmaas`, etc.) still works exactly as before and
  remains the primary path when you already have a live OSAC deployment.

Orchestration lives in the root `Makefile`: `make e2e INFRA=netris SUITE=caas` runs the full
validate → setup-infra → deploy-infra → deploy-osac → setup-suite → run-tests pipeline; each step
can also be run individually (`make setup-infra INFRA=netris`, etc.). `_validate-backend` checks
`capabilities` before anything runs; `_validate-suite-contract` checks `.env.infra` against a
per-suite `tests/<suite>/contract` file's `REQUIRED_VARS` before invoking `make test-<suite>`.

Only touch this layer when working on infrastructure provisioning itself — for writing or fixing
pytest tests, everything below in this file still applies unchanged.

## Repository Structure

```
osac-test-infra/
├── infra/
│   ├── contract.md     # Backend contract spec (targets, capabilities, .env.infra)
│   └── netris/         # Ansible-based backend (Makefile, capabilities, vendored netris-lab/)
├── tests/
│   ├── vmaas/          # ComputeInstance lifecycle, console, security, subnets
│   ├── caas/           # ClusterOrder lifecycle, credentials, templates
│   ├── storage/        # Tenant storage lifecycle
│   ├── catalog/        # CatalogItem lifecycle
│   ├── bmaas/              # BareMetalInstance lifecycle
│   ├── bmaas_networking/   # BMaaS networking E2E
│   │                       # Cross-cutting concern fixtures live in tests/conftest.py
│   └── core/               # Client wrappers, helpers, runner primitives
├── Makefile            # Build targets: test, lint, format, test-<suite>, plus infra orchestration
├── Containerfile       # UBI9 + oc/kubectl/grpcurl/osac CLI
├── pyproject.toml      # Python dependencies, ruff/basedpyright config
└── .github/workflows/  # CI: pre-commit, E2E runs, slash commands
```

## Test Framework Architecture

**Framework**: pytest with pytest-xdist (parallel execution, default 4 workers)

**Session-scoped fixtures** (`tests/conftest.py`):
- `namespace` — Test namespace (from OSAC_NAMESPACE env var)
- `cluster_domain` — Hub cluster domain
- `grpc` — GRPCClient for fulfillment API
- `cli` — OsacCLI wrapper with token refresh
- `k8s_hub_client` — K8sClient for hub (management) cluster
- `jwt_cli_admin`, `jwt_cli_user` — CLI clients with JWT authentication
- `jwt_grpc_tenant1`, `jwt_grpc_tenant2` — gRPC clients with tenant JWT tokens

**Client abstractions** (`tests/core/`):
- `grpc_client.py` — grpcurl wrapper for public/private fulfillment API
- `k8s_client.py` — kubectl wrapper with hub/VM cluster support (OSAC_VM_KUBECONFIG)
- `osac_cli.py` — osac CLI wrapper with automatic token refresh
- `runner.py` — process execution (`run`, `run_unchecked`), `poll_until`, `env`
- `helpers.py` — `wait_for_*` state-transition helpers built on `poll_until`, plus `assert_grpc_rejected`
- `keycloak.py` / `keycloak_admin.py` — JWT retrieval and Keycloak admin API helpers for tenant/user setup

## Writing New Tests

> [!IMPORTANT]
> **OSAC-3593: write new tests in the [osac mono-repo](https://github.com/osac-project/osac), not here.**
> The conventions below still describe how the suites are structured, but any new
> or changed test must land in the mono-repo.

### Test Organization

Place tests in the appropriate suite directory:
- `tests/vmaas/` — ComputeInstance features
- `tests/caas/` — ClusterOrder features
- `tests/storage/` — Tenant storage
- `tests/catalog/` — CatalogItem features
- `tests/bmaas/` — BareMetalInstance features
- `tests/bmaas_networking/` — BMaaS networking E2E

### Naming Convention

`test_<resource>_<scenario>.py` (e.g., `test_compute_instance_delete_during_provision.py`)

### Standard Test Pattern

```python
from tests.core.helpers import wait_for_cr, wait_for_deletion, wait_for_provision, wait_for_running

def test_compute_instance_lifecycle(grpc, cli, k8s_hub_client, default_subnet):
    # 1. Create via gRPC (or the CLI's create_compute_instance / create_compute_instance_with_catalog_item)
    ci_id = grpc.create_compute_instance(catalog_item="...", subnet_ids=[default_subnet])

    # 2. Wait for the CR to appear on the hub cluster
    name = wait_for_cr(k8s=k8s_hub_client, uuid=ci_id)

    # 3. Wait for provisioning to finish, then for the VM to report Running
    wait_for_provision(k8s=k8s_hub_client, name=name)
    wait_for_running(k8s=k8s_hub_client, name=name)

    # 4. Verify via K8s and gRPC
    assert k8s_hub_client.get_compute_instance_phase(name=name) == "Ready"
    assert ci_id in grpc.list_compute_instance_ids()

    # 5. Delete
    grpc.delete_compute_instance(ci_id=ci_id)

    # 6. Verify removal
    wait_for_deletion(k8s=k8s_hub_client, name=name)
```

**Key principles**:
- Always wait for state transitions (never assume immediate readiness)
- Verify via both K8s CR and gRPC API
- Clean up resources in fixture teardown or explicit delete steps
- Use the `wait_for_*` helpers (built on `poll_until`) for waiting; use `poll_until` directly for custom wait conditions

### Multi-Cluster Testing

For VM-based tests, access the workload cluster via `k8s_virt_client` fixture (scoped to vmaas tests in `tests/vmaas/conftest.py`):

```python
def test_vm_instance_exists(grpc, k8s_hub_client, k8s_virt_client, default_subnet):
    ci_id = grpc.create_compute_instance(catalog_item="...", subnet_ids=[default_subnet])
    name = wait_for_cr(k8s=k8s_hub_client, uuid=ci_id)
    vm_namespace = k8s_hub_client.get_compute_instance_vm_namespace(name=name)

    # CR on hub cluster
    phase = k8s_hub_client.get_compute_instance_phase(name=name)

    # VirtualMachineInstance on VM cluster
    status = k8s_virt_client.get_vm_printable_status(name=name, vm_namespace=vm_namespace)
```

**Environment**: Set `OSAC_VM_KUBECONFIG` to workload cluster kubeconfig. The `k8s_virt_client` fixture is only available in vmaas tests. In single-cluster dev setups (VMs run on the hub), set `OSAC_VM_KUBECONFIG` to the same value as `KUBECONFIG`; in two-cluster setups, point it at the virt cluster's kubeconfig.

### Authentication Patterns

Use session fixtures for JWT tokens:
- `jwt_cli_admin` — CLI client with admin JWT (tenant1_admin user)
- `jwt_cli_user` — CLI client with user JWT (tenant1_user)
- `jwt_grpc_tenant1` — gRPC client with tenant1 JWT token
- `jwt_grpc_tenant2` — gRPC client with tenant2 JWT token

**gRPC calls with JWT** (resource-specific methods, or the generic `call`):
```python
vn_ids = jwt_grpc_tenant1.list_virtual_network_ids()
# or, for operations without a dedicated wrapper method:
result = jwt_grpc_tenant1.call(service="osac.public.v1.VirtualNetworks/List")
```

**CLI calls** (token refresh is automatic — the fixture's `token_script` re-runs on each login):
```python
result = cli.get("virtual-network")
```

### Error Validation

Use `assert_grpc_rejected` with `pytest.raises` for expected gRPC failures:

```python
import subprocess
import pytest

from tests.core.helpers import assert_grpc_rejected

with pytest.raises(subprocess.CalledProcessError) as exc_info:
    grpc.create_external_ip(name="test-ip", pool=full_pool_id)
assert_grpc_rejected(exc_info, "FailedPrecondition")
```

## Key Files to Study

**Exemplar tests**:
- `tests/vmaas/test_compute_instance_creation.py` — Full create/provision/delete flow (`test_compute_instance_lifecycle` function)
- `tests/vmaas/test_compute_instance_restart.py` — State transitions, field updates
- `tests/caas/test_cluster_create.py` — ClusterOrder provisioning
- `tests/vmaas/test_compute_instance_api_fields.py` — API/CLI field parity validation

**Client implementations**:
- `tests/core/grpc_client.py` — grpcurl wrapper (resource-specific create/get/list/delete methods, plus generic `call`)
- `tests/core/k8s_client.py` — kubectl wrapper (resource-specific getters, e.g. `get_compute_instance_phase`, `get_vm_printable_status`)
- `tests/core/runner.py` — `poll_until` (configurable `retries`/`delay`, keyword-only, requires a `description`)
- `tests/core/helpers.py` — `wait_for_*` helpers built on `poll_until`, and `assert_grpc_rejected`

**Fixtures**:
- `tests/conftest.py` — Session-scoped clients, namespace, domain, auth tokens

## Running Tests Locally

### Prerequisites

1. **Python 3.11+**: Install via system package manager or pyenv
2. **uv**: Install with `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`
3. **Cluster access**: Running OSAC deployment with cluster-admin kubeconfig
4. **osac CLI**: Download from GitHub releases (match deployed version)
5. **grpcurl**: Download from GitHub releases (version 1.9.1 recommended)

### Environment Variables

```bash
export OSAC_NAMESPACE=osac                           # OSAC deployment namespace
export KUBECONFIG=/path/to/hub-kubeconfig            # Management cluster
export OSAC_VM_KUBECONFIG=/path/to/vm-kubeconfig     # Workload cluster (for VM tests)
export OSAC_FULFILLMENT_ADDRESS=host:port            # Fulfillment API address (auto-derived if not set)
export OSAC_FULFILLMENT_PRIVATE_ADDRESS=host:port    # Fulfillment private API address
export OSAC_KEYCLOAK_URL=https://keycloak-host       # Keycloak URL for JWT auth
export OSAC_KEYCLOAK_ADMIN_PASSWORD=<password>       # Keycloak admin password (defaults to "admin" in tests/conftest.py if unset)
export OSAC_JWT_PASSWORD=<password>                  # Password for test JWT users (defaults to "foobar" in tests/conftest.py if unset)
export OSAC_SERVICE_ACCOUNT=admin                    # ServiceAccount used for `oc create token` (default: admin)
export OSAC_CLI_PATH=osac                            # Path to the osac CLI binary (default: osac)
export OSAC_VM_TEMPLATE=ocp-virt-vm                  # ComputeInstance template (vmaas tests, default shown)
```

Token creation is automatic: fixtures run `oc create token <service-account> -n <namespace> --as system:admin` (ServiceAccount) or fetch a JWT from Keycloak (tenant tests) — there is no token env var to set manually.

### Run Tests

```bash
make test                                    # All suites (parallel)
TEST=test_name make test-vmaas              # Specific test with filter
```

See README.md for full test execution documentation.

### Debugging Failed Tests

**View gRPC request/response**:
- Run with `-v` (verbose) or `-s` (no output capture) to see print statements and assertion failures inline
- Check `reports/*.xml` for JUnit test results (XML format with test case results, failures, and timing)

**Inspect cluster state**:
```bash
# Hub cluster CRs
kubectl -n $OSAC_NAMESPACE get computeinstances,virtualnetworks,subnets

# VM cluster instances
kubectl --kubeconfig=$OSAC_VM_KUBECONFIG get vmi -n $OSAC_NAMESPACE

# Fulfillment logs
kubectl -n $OSAC_NAMESPACE logs -l app=fulfillment-service --tail=100
```

**Gather diagnostics**:
```bash
./scripts/gather-osac-logs.sh  # Collect logs with redaction
```

## CI Workflows

### Pre-commit Checks

**Workflow**: `.github/workflows/pre-commit.yaml`
- Runs on every PR
- Pre-commit hooks: yamllint, ansible-lint, trailing-whitespace, detect-private-key, check-json
- Note: ruff is not configured in pre-commit hooks (run via Makefile instead)

**Run locally**:
```bash
make lint    # ruff check + ruff format --check (not part of pre-commit)
make format  # ruff format (not part of pre-commit)
pre-commit run --all-files  # yamllint, ansible-lint, standard hooks
```

### E2E Test Runs

Each E2E flavor is split into a reusable `workflow_call` file plus a separate "caller" file that
owns the triggers — GitHub Actions triggers are per-file, so this keeps unrelated trigger types
(schedule, `workflow_dispatch`, slash-command) out of the reusable workflow's own history:

- `.github/workflows/e2e-vmaas.yml` — reusable (`workflow_call`), cluster-tool snapshot restoration
  - `.github/workflows/e2e-vmaas-caller.yml` — `workflow_dispatch` entry point, calls `e2e-vmaas.yml`
- `.github/workflows/e2e-vmaas-full-install.yml` — reusable (`workflow_call`), full install from scratch (no snapshot)
  - `.github/workflows/e2e-vmaas-full-install-caller.yml` — triggers on PR to `main`, a 12-hour
    schedule (`0 */12 * * *`), and `workflow_dispatch`; calls `e2e-vmaas-full-install.yml`

Both flavors accept `test-suite`/`test-filter` inputs and output JUnit XML to `reports/`.

**Fork PR workflow**:
- `.github/workflows/slash-command.yml` — Listens for `/test`, `/retest`, `/cancel`, and `/ok-to-test` PR comments
- `.github/workflows/slash-command-handler.yml` — Triggers E2E workflow after slash command approval
- `.github/workflows/ok-to-test-label-cleanup.yml` — Removes the `ok-to-test` label whenever new commits are pushed, requiring an org member to re-approve
- Tests run in container with Vault secrets injection


### Monitoring Workflows

Unrelated to the pytest E2E suites — these validate/deploy the `monitoring/` stack, split into two
files for the same per-file-trigger reason as the E2E workflows above (so the deploy job never
shows up, even as "skipped", on a PR that only touches monitoring config):
- `.github/workflows/validate-monitoring.yml` — runs on PRs touching `monitoring/**`; config/rules/dashboard sanity checks, no secrets needed
- `.github/workflows/deploy-monitoring.yml` — runs on push to `main` (or manual dispatch) on the dedicated `monitoring-central` self-hosted runner; refreshes the live stack and fans out to registered remote runners

### Slack On-Call Rotation

- `.github/workflows/rotate-slack-oncall.yml` — targets 6am Europe (CET/CEST, `Europe/Berlin`) every Wednesday; since Actions cron is UTC-only and Central Europe switches between CET/CEST, it schedules both UTC offsets (`0 4 * * 3` and `0 5 * * 3`) and a "Check Europe-local run time" gate step matches `github.event.schedule` against whichever entry corresponds to the *current* UTC offset (not just a wall-clock hour check, which a delayed run of the inactive-season cron could cross into and cause a duplicate). Also has `workflow_dispatch`. Runs on `ubuntu-latest` with plain repo secrets (no Vault — unlike the workflows above, this has no self-hosted-runner dependency)
- `scripts/rotate-slack-oncall.sh` — picks `(whole weeks elapsed since a fixed reference Wednesday) % len(rotation list)` as the index into `SLACK_ONCALL_ROTATION_LIST` (not the ISO week number itself, which resets from 52/53 back to 1 every year and would otherwise pick the same person twice at that boundary for some list lengths), updates `SLACK_ONCALL_USERGROUP_ID`'s sole member via `usergroups.users.update`, and posts the new on-call (plus the Wed-through-Tue duty window) to `SLACK_ONCALL_CHANNEL_ID`. Stateless by design: the index and window are derived from the most recently started Wednesday on or before "today", so a missed/re-run week or a manual run on a non-Wednesday still self-corrects to the right person and dates instead of drifting. Logs use the rotation index, not the selected email, since this repo is public and Actions logs are visible to anyone
- Required secrets: `SLACK_ONCALL_BOT_TOKEN` (scopes: `chat:write`, `usergroups:write`, `users:read`, `users:read.email`), `SLACK_ONCALL_USERGROUP_ID`, `SLACK_ONCALL_CHANNEL_ID`, `SLACK_ONCALL_ROTATION_LIST` (comma-separated emails)

## Code Style

**Formatter**: ruff format (line-length: 120, skip-magic-trailing-comma: true)

**Linter**: ruff check with rules E, F, W, I, UP, ANN, B, SIM, RUF (target: py311)

**Type checking**: basedpyright (reportUnusedCallResult/Any/ExplicitAny: false)

**Import annotations**: All Python files start with `from __future__ import annotations`

**Per-file ignores**: `tests/*` skip ANN101 (self annotation), ANN201 (return type)

## Common Patterns

### Waiting for State Transitions

Use `poll_until` for custom conditions. It is keyword-only and requires a `description` for failure messages:

```python
from tests.core.runner import poll_until

poll_until(
    fn=lambda: k8s_hub_client.get_compute_instance_phase(name=name, checked=False),
    until=lambda phase: phase == "Ready",
    retries=60,
    delay=5,
    description=f"ComputeInstance {name} to become Ready",
)
```

**Built-in wait helpers** (in `tests/core/helpers.py`, all built on `poll_until`):
- `wait_for_deletion(k8s=k8s_client, name=cr_name)` — Wait for CR removal
- `wait_for_virtual_network_cr(k8s=k8s_client, uuid=vn_id)` — Wait for VirtualNetwork CR creation
- `wait_for_subnet_cr(k8s=k8s_client, uuid=subnet_id)` — Wait for Subnet CR creation
- `wait_for_virtual_network_ready(k8s=k8s_client, name=vn_name)` — Wait for VirtualNetwork Ready state
- `wait_for_subnet_ready(k8s=k8s_client, name=subnet_name)` — Wait for Subnet Ready state

### Dual-Cluster Verification

Verify both hub CR and workload cluster resources:

```python
# Hub cluster CR
assert k8s_hub_client.get_compute_instance_phase(name=name) == "Ready"

# VM cluster VirtualMachineInstance
assert k8s_virt_client.get_vm_printable_status(name=name, vm_namespace=vm_namespace) == "Running"
```

### API/CLI Parity Testing

Validate that CLI output matches gRPC API fields (see `tests/vmaas/test_compute_instance_cli_fields.py`):

```python
# Create via CLI
ci_uuid = cli.create_compute_instance(catalog_item="...", subnet="...")

# Get via CLI (raw output, e.g. YAML) and gRPC (structured dict)
cli_output = cli.get("computeinstance", output="yaml")
grpc_output = grpc.get_compute_instance(ci_id=ci_uuid)

# Compare field values (parse cli_output as needed)
```

## Cross-Cutting Concern Testing

Features like metering, storage, networking, and catalog span multiple domains. Use the **composable fixture pattern** instead of duplicating verification logic:

1. **Verifier class** in `tests/core/<concern>.py`
2. **Fixture + marker** in `tests/conftest.py`
3. **Tests inject the fixture** + add `@pytest.mark.<concern>` marker

```python
@pytest.mark.metering
def test_vm_lifecycle(grpc, cli, k8s_hub_client, default_subnet, metering):
    uuid = cli.create_compute_instance(...)
    metering.expect("osac.resource.created.v1", resource_id=uuid)
    # ... lifecycle ...
    # metering.verify() runs on fixture teardown
```

Tests fail fast when concern infra is missing (e.g., `METERING_ADAPTER_URL` not set). Use `pytest -m "not metering"` to exclude metering tests when the test adapter is unavailable.

## Security & Secrets

**Vault integration**: CI workflows fetch secrets via AppRole authentication
- Vault server config: `vault/config/vault.hcl`
- Secrets: pull-secret, AAP license, snapshot registry auth
- CI writes secrets to `$RUNNER_TEMP` with 600 permissions

**Authentication**:
- Keycloak for JWT tokens (realm: osac, organization scopes)
- ServiceAccount tokens via `oc create token <service-account> -n <namespace> --as system:admin`
- Bearer token authentication for gRPC calls

**Pre-commit protection**: `detect-private-key` hook prevents credential commits

## Container Execution

Tests run in UBI9 container with pre-installed binaries:

**Containerfile**:
- Base: registry.access.redhat.com/ubi9/ubi:latest
- Binaries: oc, kubectl, grpcurl (1.9.1 by default, override via `GRPCURL_VERSION` build arg)
- osac CLI: resolved from the latest `fulfillment-service` GitHub release at build time (or pinned via `OSAC_VERSION`/`OSAC_CLI_BIN` build args for a pre-built binary), with checksum verification
- Python: 3.11+, uv package manager
- Dependency lock: `uv.lock` file ensures reproducible builds
- Entry point: pytest with reports/ output

**Build**:
```bash
podman build -t osac-test-infra:latest .
```

**Run**:
```bash
podman run --rm \
  -v $KUBECONFIG:/kubeconfig:Z \
  -e KUBECONFIG=/kubeconfig \
  -e OSAC_NAMESPACE=osac \
  osac-test-infra:latest
```

## Commit Conventions

**Format**: `OSAC-XXXX: <description>` or `NO-ISSUE: <description>`

**Examples**:
- `OSAC-1675: Add /ok-to-test slash command for fork PR E2E approval`
- `NO-ISSUE: Update tests to use valid user with valid tenant`
- `OSAC-1909: Add periodic E2E runs with Slack notifications`

**Common patterns**:
- Feature additions: `OSAC-XXXX: Add <feature>`
- Bug fixes: `OSAC-XXXX: Fix <issue>`
- Infrastructure: `NO-ISSUE: <change>`

## Common Debugging Scenarios

### Test hangs during provisioning

**Cause**: Resource stuck in Pending state
**Debug**:
```bash
kubectl -n $OSAC_NAMESPACE get computeinstance <name> -o yaml
kubectl -n $OSAC_NAMESPACE logs -l app=fulfillment-service --tail=100
```

### gRPC call returns "Unauthenticated"

**Cause**: Expired or invalid token. Session-scoped `grpc`/`cli` fixtures request a token once per run, so a long test session can outlast the token's `4h` duration
**Fix**: For manual/interactive debugging, mint a fresh token the same way the fixtures do:
```bash
oc create token $OSAC_SERVICE_ACCOUNT -n $OSAC_NAMESPACE --duration 4h --as system:admin
```

### Test fails with "resource not found"

**Cause**: Race condition or cleanup from previous run
**Debug**:
```bash
kubectl -n $OSAC_NAMESPACE get computeinstances
kubectl -n $OSAC_NAMESPACE get virtualnetworks
```

### VM cluster resource missing

**Cause**: OSAC_VM_KUBECONFIG not set or incorrect
**Fix**:
```bash
export OSAC_VM_KUBECONFIG=/path/to/vm-kubeconfig
kubectl --kubeconfig=$OSAC_VM_KUBECONFIG get vmi -n $OSAC_NAMESPACE
```

## Adding New Test Suites

1. **Create suite directory**: `tests/<suite-name>/`
2. **Add conftest.py**: Define suite-specific fixtures if needed
3. **Add Makefile target**: `make test-<suite-name>` in root Makefile
4. **Update CI workflow**: Add new suite to E2E workflow matrix
5. **Document patterns**: Add suite-specific conventions to this file

## References

**Test documentation**: See individual test files for inline comments and examples
**Client APIs**: See `tests/core/*.py` for client method signatures
**Fixture reference**: See `tests/conftest.py` for session-scoped fixtures
**CI workflows**: See `.github/workflows/` for E2E and pre-commit automation
