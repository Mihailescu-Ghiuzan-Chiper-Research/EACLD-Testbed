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

The diagnosis request specifies operating mode, baseline and temperature threshold. The selected accepted telemetry is used for E0 inference, which chooses verification actions under the finite-horizon policy. The persisted result contains posterior probabilities, selected checks, observable outcomes, costs, latency and risk. Intervention ground truth remains on VM-CONTROL.

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
