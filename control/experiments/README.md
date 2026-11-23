# Distributed H1–H4 experimental execution

This directory provides the H1–H4 experiment runners. They coordinate the VM-CONTROL process and intervention services with the source-restricted diagnosis API on VM-CORE. VM-CONTROL retains intervention labels, schedules and causal ground truth; VM-CORE processes selected telemetry and verification evidence.

## Prerequisites

The five VMs and seven services must already be deployed according to the
approved testbed specification. On VM-CONTROL, the complete repository must be
checked out in `/opt/eacld`. VM-NODES must publish an enabled, authenticated
node with `node_id=1`, zero temperature bias and zero humidity bias. MQTT
credentials, node HMAC keys, PostgreSQL, broker routing and control-agent
network access must be configured. The runners execute against the pre-provisioned services and credentials.

Run from **VM-CONTROL**, not VM-CORE or an administration workstation.
Python 3.12+ is sufficient for the host-side runner. Docker Compose is used
for private local operations against the running `eacld-control` container.

## H1 — process intervention

The following command reproduces episode H1-001 in experimental block B01 with node 1, a baseline temperature of 22,000 m°C and a process disturbance of +6,912 m°C:

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
   If the selected record is superseded, the request returns HTTP 409 and the runner retries within the configured timeout.
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
Each repetition uses a new episode identifier to retain the preceding evidence.

Each episode stores the selected telemetry, B6 diagnostic output and private causal ground truth for subsequent comparison.

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

## Verification

```bash
python3 -m unittest discover -s control/tests -p test_h1_run.py -v
```

VM-CORE operator API tests depend on the existing Python application
dependencies and preserved E0 model integration. Execute them in the
repository's VM-CORE test environment using the same package paths as other
core tests.

Block B01 executed episodes H1-001, H2-001, H3-001 and H4-001 on the five-VM Proxmox testbed. The four episode records contain the expected diagnoses, and the consolidated regression suite passed 72 tests. The verification script extracts the E0 package and configures the VM-CORE import path.


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

In B01, H2 holds the process at 22,000 m°C and applies a +6,912 m°C node-local bias, producing an authenticated sensor measurement of 28,912 m°C. The
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

The H2 runner reads and verifies the active node configuration before applying and restoring the B01 measurement bias.


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

The B01 H3 intervention changes the 32-bit big-endian temperature field from 22,000 to 28,912 m°C. The frame header is 62 bytes long; its signed
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
python3 -c "import base64,io,pathlib,zipfile; p=pathlib.Path('core/vendor/e0/e0_source.zip.b64'); z=zipfile.ZipFile(io.BytesIO(base64.b64decode(p.read_text()))); assert z.testzip() is None; z.extractall('core/vendor/e0')"
PYTHONPATH=core/services/core:core/vendor/e0/EACLD_E0_Experimental_Package:shared python3 -m unittest \
  discover -s core/tests -p test_h3_core_selection.py -v
```


## H4 — authenticated telemetry replay

H4 retransmits a **previously forwarded, authentic** 94-byte sensor frame.
The original HMAC is preserved. The VM-CORE sequence registry records the
retransmission as **stale**, whereas the HMAC verification remains **valid**.
The intervening newer frame is expected to be both authentic and fresh.

The operator executes H4 from VM-CONTROL with the same five-VM services
used for H1–H3:

```bash
cd /opt/eacld/control
python3 experiments/h4_run.py \
  --episode-id H4-001 \
  --block-id B01 \
  --node-id 1 \
  --mode nominal \
  --baseline-temperature-mc 22000 \
  --high-delta-mc 3000 \
  --humidity-mpct 50000 \
  --timeout-seconds 120
```

Block B01 uses temperature in m°C and humidity in mPct. During H4, the nominal process remains at 22,000 m°C and 50,000 mPct while G2 retransmits an authenticated earlier frame.

The runner performs the following sequence:

1. Verify control-agent readiness, zero node bias and the absence of a
   pending G2 intervention. Record the prior G2 replay event identifier.
2. Reset the simulated process and identify a new, authenticated, fresh
   telemetry frame as the **reference**. Preserve its full 94-byte payload
   and original VM-CORE record ID.
3. Stage G2 `replay` with `node_id` and the reference `frame_hex`.
   G2 atomically checks that this exact frame exists in its bounded recent
   forwarding history (64 frames per node); the operator does not supply
   arbitrary bytes for publication.
4. When G2 receives a newer frame with the same node and boot identifier,
   it forwards the new frame, then republishes the selected reference bytes.
   The event records the reference, trigger and replayed frame byte strings.
5. Verify the G2 replay event, the byte-for-byte identity of the copied
   frame, and the strictly increasing trigger sequence number.
6. Locate the fresh trigger frame in VM-CORE using its exact payload and
   record-ID watermark; subsequently locate the replayed reference payload
   **after the trigger's record ID**. Require `mac_status=valid` and
   `sequence_status=stale` on the replayed record.
7. Diagnose that selected stale record via `selected_telemetry_id`; read
   back the persisted EACLD decision and verify the exact input message.
8. Save the private H4 intervention record on VM-CONTROL, confirm unchanged
   process state, and clear G2 intervention state even after an error.

The output directory is `~/eacld-experiments/H4-001/`.
Successful execution records `parameters.json`,
`reference_telemetry.json`, `edge_initial_status.json`,
`edge_replay_evidence.json`, `core_trigger_telemetry.json`,
`core_replayed_telemetry.json`, `diagnosis_response.json`,
`core_diagnosis_record.json`, `private_ground_truth.json`,
`edge_cleanup.json` and `manifest.json`. Errors are recorded in
`failure.json`. Each episode requires a distinct identifier.

Run focused regression tests from the repository root:

```bash
python3 -m unittest discover -s control/tests -p test_h4_run.py -v
PYTHONPATH=edge/services/g2:shared python3 -m unittest \
  discover -s edge/tests -p test_h4_replay_evidence.py -v
python3 -c "import base64,io,pathlib,zipfile; p=pathlib.Path('core/vendor/e0/e0_source.zip.b64'); z=zipfile.ZipFile(io.BytesIO(base64.b64decode(p.read_text()))); assert z.testzip() is None; z.extractall('core/vendor/e0')"
PYTHONPATH=core/services/core:core/vendor/e0/EACLD_E0_Experimental_Package:shared python3 -m unittest \
  discover -s core/tests -p test_h4_replay_selection.py -v
```

The four experiment runners are `h1_run.py`, `h2_run.py`, `h3_run.py`
and `h4_run.py`. They use the common VM-CORE observation endpoints,
operator-only control routes, stable telemetry IDs and separate
VM-CONTROL ground-truth artifacts. Each experiment can be executed as an
independent, uniquely identified episode.

## H1–H4 regression verification

The operator CLI schemas and test-injection interfaces are checked together
by `control/tests/test_experiment_compatibility.py`. The consolidated
runner executes the focused operator, VM-NODES, G2 and VM-CORE regression
suites, stopping at the first failed group:

```bash
bash control/experiments/verify_h1_h4.sh
```

Run the verification script from a checkout with the service-level Python
dependencies installed. The script extracts `core/vendor/e0/e0_source.zip.b64`
and sets `PYTHONPATH` to `core/vendor/e0/EACLD_E0_Experimental_Package`
for the VM-CORE tests. Distributed experiment episodes are executed through
the four operator commands above.
