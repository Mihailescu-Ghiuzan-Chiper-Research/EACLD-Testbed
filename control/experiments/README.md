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


## H2 — node-local measurement bias

VM-NODES now exposes an operator-restricted, in-memory bias-control interface.
Use the runner on **VM-CONTROL**; it requires no manual JSON editing or
restarting the VM-NODES service. The H1 runner and VM-CORE telemetry-ID pinning
are reused.

From the repository checkout on VM-CONTROL:

```bash
cd /opt/eacld/control
python3 experiments/h2_run.py \
  --episode-id H2-001 \
  --block-id B01 \
  --node-id 1 \
  --mode nominal \
  --baseline-temperature-mc 22000 \
  --high-delta-mc 3000 \
  --bias-temperature-mc 6912 \
  --humidity-mpct 50000 \
  --timeout-seconds 120
```

The selected example starts with the process at 22000 mC, leaves the common
process unchanged, and changes node 1's signed measurement to 28912 mC. The
initial temperature-bias and humidity-bias settings for that node must both
be zero; the runner checks these values instead of silently overwriting an
unknown configuration.

Execution sequence:

1. Confirm the four control agents and VM-CORE readiness.
2. Read the current bias of the selected node through
   `GET /v1/nodes/bias?node_id=1` on 10.60.30.12:8300.
3. Reset the private process on VM-CONTROL and wait for a new authenticated,
   fresh baseline telemetry record.
4. Apply the H2 bias through `POST /v1/nodes/bias`; verify its value and
   monotonically increased revision via a subsequent readback.
5. Confirm that the shared process state has not changed.
6. Wait for authenticated, fresh telemetry containing the biased sensor
   temperature, and diagnose that exact received message using
   `expected_telemetry_id`.
7. Retrieve the persisted VM-CORE diagnostic record and compare its selected
   telemetry ID and observed fields with those collected before diagnosis.
8. Save the private H2 intervention record separately on VM-CONTROL; restore
   and verify the original node bias even when an execution error occurs.

Results are placed under `~/eacld-experiments/H2-001/` on VM-CONTROL,
including `initial_bias.json`, `baseline_telemetry.json`,
`bias_activation.json`, `diagnosis_response.json`,
`core_diagnosis_record.json`, `process_state.json`,
`private_ground_truth.json`, `bias_restoration.json`, and
`manifest.json`. Failures leave `failure.json` and the evidence already
recorded. Do not reuse the same episode identifier.

Focused local runner tests:

```bash
python3 -m unittest discover -s control/tests -p test_h2_run.py -v
```

The runner uses the active node's current configuration rather than the
example population in `nodes/config/nodes.example.json`. The numeric values
above illustrate a reproducible H2 episode; experimental parameter selection
remains part of the research design.


## H3 — controlled G2 header mutation

H3 alters exactly one authenticated header byte after VM-NODES has signed the
message. G2 keeps the original HMAC bytes, so VM-CORE should record
`mac_status=invalid`. VM-CONTROL preserves the before/after frame evidence.

Run on VM-CONTROL from the full repository checkout:

```bash
cd /opt/eacld/control
python3 experiments/h3_run.py \
  --episode-id H3-001 \
  --block-id B01 \
  --node-id 1 \
  --mode nominal \
  --baseline-temperature-mc 22000 \
  --high-delta-mc 3000 \
  --humidity-mpct 50000 \
  --offset 40 \
  --xor-mask 37 \
  --timeout-seconds 120
```

These illustrative values change the 32-bit big-endian temperature field
from 22000 mC to 28912 mC. The frame header is 62 bytes long; its signed
temperature occupies byte offsets **38–41** (zero-based). The mutation
affects one of those four bytes only. The last 32 bytes (HMAC-SHA256) remain
unchanged.

Execution sequence:

1. Confirm VM-CONTROL reachability of agents, VM-CORE readiness and zero
   sensor bias for the selected node.
2. Confirm that G2 has no pending action for that node, and snapshot its
   previous mutation-event counter.
3. Reset the shared process and wait for a fresh authenticated baseline
   message in VM-CORE.
4. Send the one-shot `mutate` intervention to VM-EDGE; poll its restricted
   status endpoint for the *new* mutation event.
5. Verify the original 94-byte frame, the single-byte XOR, the unchanged
   32-byte HMAC and the expected mutated temperature from G2 evidence.
6. Locate the exact mutated frame in VM-CORE's persisted telemetry by node,
   bytes and minimum received-record ID, rather than relying on the newest
   message. Require `mac_status=invalid` and `sequence_status=unavailable`.
7. Invoke `POST /v1/diagnose` with `selected_telemetry_id`; retrieve the
   persisted record and confirm that EACLD used this particular frame.
8. Verify the common simulated process stayed at baseline. Preserve private
   intervention details on VM-CONTROL only and clear the G2 intervention,
   including after failures.

The output folder is `~/eacld-experiments/H3-001/` on VM-CONTROL.
Key files are `parameters.json`, `baseline_telemetry.json`,
`edge_initial_status.json`, `edge_mutation_evidence.json` (before/after
signed frames), `core_mutated_telemetry.json`,
`diagnosis_response.json`, `core_diagnosis_record.json`,
`private_ground_truth.json`, `edge_cleanup.json` and `manifest.json`.
Failures leave `failure.json` and the evidence already collected.
Use a distinct episode identifier for each repetition.

Focused local unit tests:

```bash
python3 -m unittest discover -s control/tests -p test_h3_run.py -v
PYTHONPATH=edge/services/g2:shared python3 -m unittest \
  discover -s edge/tests -p test_h3_mutation_evidence.py -v
```

VM-CORE tests, in its configured test environment:

```bash
PYTHONPATH=core/services/core:core/vendor/e0:shared python3 -m unittest \
  discover -s core/tests -p test_h3_core_selection.py -v
```
