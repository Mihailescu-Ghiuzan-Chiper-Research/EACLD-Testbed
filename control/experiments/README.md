# H1 distributed execution

This directory provides one operator-side experiment runner. It drives the
existing private process orchestrator on VM-CONTROL and the restricted
diagnosis service on VM-CORE. It never sends the injected H1 label, schedule,
or private ground truth to VM-CORE.

## Prerequisites

The five VMs and seven services must already be deployed according to the
approved testbed specification. On VM-CONTROL, the complete repository must be
checked out in `/opt/eacld`. VM-NODES must publish an enabled, authenticated
node with `node_id=1`, zero temperature bias and zero humidity bias. MQTT
credentials, node HMAC keys, PostgreSQL, broker routing and control-agent
network access must be configured. This experiment runner does not install
services or change private keys.

Run from **VM-CONTROL**, not VM-CORE or an administration workstation.
Python 3.12+ is sufficient for the host-side runner. Docker Compose is used
for private local operations against the running `eacld-control` container.

## Single invocation

The following values reproduce the *provisional H1 parameter example*
introduced in the operational protocol. They are not a calibrated or
approved scientific experiment design:

```bash
cd /opt/eacld/control
python3 experiments/h1_run.py \
  --episode-id H1-001 \
  --block-id B01 \
  --node-id 1 \
  --mode nominal \
  --baseline-temperature-mc 22000 \
  --high-delta-mc 3000 \
  --disturbance-temperature-mc 6912 \
  --humidity-mpct 50000 \
  --timeout-seconds 120
```

The command performs, in order:

1. Check the available control agents and VM-CORE MQTT readiness.
2. Record the current accepted-telemetry ID for the target node.
3. Reset the private shared process and confirm a new authenticated, fresh
   baseline reading after reset.
4. Schedule H1 at process step 1 only and advance exactly one step.
5. Save the private process intervention log on VM-CONTROL.
6. Observe incoming authenticated, fresh telemetry with the expected
   temperature and request B6 diagnosis of that exact telemetry record.
   A superseded frame produces HTTP 409 instead of silently diagnosing a
   different frame; the runner can retry before the timeout.
7. Read back the persisted VM-CORE diagnosis and verify its telemetry ID,
   temperature, authentication and freshness flags.
8. Write an episode manifest and retain both the public diagnostic evidence
   and the private H1 truth in distinct files on VM-CONTROL.

Successful results are under `~/eacld-experiments/H1-001/` on
VM-CONTROL. Files include `parameters.json`, `git_commit.txt`,
`baseline_telemetry.json`, `process_state.json`,
`private_ground_truth.json`, `diagnosis_response.json`,
`core_diagnosis_record.json`, and `manifest.json`.
Failures leave `failure.json` and any evidence captured beforehand.
A new episode must use a new identifier; this runner does not overwrite old
results.

The sequence verifies the intervention and the identity of the diagnostic
input. It **does not** assert that the B6 decision is correct; that judgment
must come from subsequent research analysis.

## VM-CORE integration

The restricted VM-CORE API adds:

- `GET /v1/telemetry/latest?node_id=1`: last accepted frame with a stable
  telemetry ID and observable decoded fields.
- `POST /v1/diagnose`: accepts the original six request fields and the
  optional `expected_telemetry_id`. If that ID is no longer the latest
  accepted telemetry when diagnosis begins, returns HTTP 409.
- `GET /v1/diagnosis?episode_id=H1-001`: persisted result with the exact
  telemetry used during inference.

The new observation routes have the same VM-CONTROL-only source-IP
restriction as the diagnosis route. Ground-truth labels remain outside VM-CORE.

## Local tests and deployment status

```bash
python3 -m unittest discover -s control/tests -p test_h1_run.py -v
```

VM-CORE operator API tests depend on the existing Python application
dependencies and preserved E0 model integration. Execute them in the
repository's VM-CORE test environment using the same package paths as other
core tests.

The H1 runner's local simulated tests are separate from five-VM end-to-end
verification. Live-VM execution remains pending until the infrastructure is
available. The pre-existing core GitHub Actions import failure remains a
separate CI issue.
