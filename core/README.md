# VM-CORE — distributed EACLD diagnosis

VM-CORE implements authenticated MQTT ingestion, persistent anti-replay checking, active evidence acquisition, the original E0 Bayesian policy engine, and PostgreSQL 16 persistence, following technical architecture V1.

## Runtime flow

- The core subscribes to the post-gateway MQTT stream `accepted/+` on VM-EDGE (10.60.10.14:1883).
- ingest.py stores accepted-topic payloads with their authentication and freshness classifications. verifier.py uses node/version HMAC keys available only on VM-NODES and VM-CORE. sequence_registry.py persists the last authenticated sequence per node/boot identifier. Invalidly authenticated frames cannot advance the high watermark.
- live_env.py implements the original E0 LiveEnvironment public state and execute(action) method. message_verify and sequence_check return the stored checks; reference_read directly reads VM-REFERENCE at 10.60.20.13:8201; node_challenge directly requests VM-NODES at 10.60.20.12:8202 and validates its nonce and HMAC.
- `runner.py` loads the E0 `PolicyEngine`, configuration and fitted model. Policy B6_EACLD selects verification actions executed against the distributed services. Latency is measured as elapsed wall-clock time, transmitted bytes are derived from observed application payloads, and costs use the configured nominal E0 values.
- `core.py` exposes the source-restricted NET-CONTROL interface on 10.60.30.15:8300. Intervention labels and schedules remain on VM-CONTROL.

The VM-CORE container loads the E0 Bayesian engine, fitted model and configuration from `vendor/e0/e0_source.zip.b64`. The package contains `eacld/__init__.py`, `eacld/core.py`, `eacld/simulator.py`, `config/e0_config.json`, `results/fitted_model.json`, and `tests/test_core.py` under `EACLD_E0_Experimental_Package/`. The Docker build verifies the `core.py` SHA-256 digest `cedddf154553162a4bda1e12fc48045162af0918ffd975a001141ae2e4f05f58`. The original E0 ZIP stored on TUIAȘI Drive has SHA-256 digest `0c495ac36ef06c5654673a6996d6478953750dd0eb414ba45eede0e1c3404454`.

## Explicit diagnosis API

`GET /healthz` reports readiness after the MQTT `accepted/+` subscription is established. VM-CONTROL (10.60.30.10) invokes `POST /v1/diagnose` on 10.60.30.15:8300 to run diagnosis.

Example diagnostic request:

    {
      "node_id": 12,
      "episode_id": "operator-assigned-id",
      "block_id": "operator-assigned-block",
      "mode": "nominal",
      "baseline_temperature_mC": 22000,
      "high_delta_mC": 3000
    }

The diagnosis request supplies operating mode, baseline and temperature threshold. The API defaults to the latest accepted telemetry; `expected_telemetry_id` enforces the latest-record identity for H1/H2 (HTTP 409 on mismatch), while `selected_telemetry_id` selects a specific persisted record for H3/H4. These two selection parameters are mutually exclusive. The E0 policy chooses verification actions and persists posterior probabilities, selected checks, observable outcomes, costs, latency and risk. Intervention ground truth remains on VM-CONTROL.

VM-CONTROL initiates each diagnostic episode after selecting the telemetry record.

## Deployment

Check out the complete repository at /opt/eacld on VM-CORE. Provision three files under /opt/eacld/core/secrets:

- node_keys.json: the same per-node/key-version hexadecimal keyring as VM-NODES;
- mqtt_password: broker password of the core principal;
- pg_password: password of the PostgreSQL eacld user.

Runtime credentials are mounted read-only from the service secret files. PostgreSQL operates within the Docker-internal network; causal intervention ground truth is stored on VM-CONTROL.

VM-CORE has NET-DATA 10.60.10.15, NET-PROBE 10.60.20.15 and NET-CONTROL 10.60.30.15 (plus the separate administration network), per V1.

    cd /opt/eacld/core
    docker compose up --build -d

## Verification

`core/tests/test_core_service.py` covers HMAC verification, keyring and topic handling, active probes, nonce verification, E0 inference integration, MQTT ingestion and restricted operator control. The nine E0 mathematical core tests are included in the suite. The consolidated H1–H4 regression script loads the E0 source package through `core/vendor/e0/EACLD_E0_Experimental_Package`. Block B01 passed 72 regression tests; GitHub Actions provides a manual execution workflow.

Distributed B01 execution exercised MQTT transport, persistent telemetry records and E0 inference across the five virtual machines.


## Operator evidence API (H1–H4)

VM-CONTROL (10.60.30.10) accesses the following source-restricted NET-CONTROL routes:

- `GET /v1/telemetry/latest?node_id=1`: the newest accepted frame for
  that node, including its stable database ID, observable decoded values,
  raw frame hexadecimal bytes, HMAC status and freshness classification.
- `GET /v1/telemetry/match?node_id=1&after_id=<id>&frame_hex=<188-lowercase-hex>`:
  the earliest exact 94-byte match after a pre-intervention telemetry
  watermark. This avoids losing the single mutated H3 frame when a newer
  normal frame is received.
- `POST /v1/diagnose`: original six research fields plus either
  `expected_telemetry_id` (latest-record guard) or
  `selected_telemetry_id` (exact persisted record), never both.
- `GET /v1/diagnosis?episode_id=...`: the persisted diagnosis with
  the exact frame and observations used by the original E0 engine.

H3 expects an invalid HMAC and unavailable sequence classification for
the modified frame. PostgreSQL retains such received frames, but an
invalid frame cannot advance the authenticated sequence watermark. VM-CONTROL records the intervention action and original pre-mutation frame from G2; VM-CORE persists the received telemetry and verification status.

Focused tests are in `core/tests/test_operator_api.py` and
`core/tests/test_h3_core_selection.py`.


## H4 authenticated replay classification

The H4 operator reuses `GET /v1/telemetry/match`,
`POST /v1/diagnose` with `selected_telemetry_id`, and the persisted
diagnosis-readback endpoint introduced for H1–H3. H4 uses the same telemetry matching, selected-record diagnosis and persisted result interfaces established for H1–H3.

For a retransmitted, previously accepted 94-byte frame, the verifier
records `mac_status=valid` because its signed bytes are unchanged.
The persisted sequence watermark produces `sequence_status=stale`
once the original and a strictly newer message have already been
accepted for that node and boot. The H4 runner binds B6 inference to the selected persisted stale record.

Focused tests are in `core/tests/test_h4_replay_selection.py`.
