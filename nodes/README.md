# VM-NODES — eacld-nodes

This service implements the EACLD virtual sensor population, authenticated MQTT telemetry, direct active node challenges, and the restricted NET-CONTROL health and H2 sensor-bias operations.

## Network interfaces

| Function | Network | Destination |
| --- | --- | --- |
| Shared virtual-process reading | NET-PLANT | GET http://10.60.40.10:8100/v1/state |
| Raw telemetry publication | NET-DATA | MQTT 10.60.10.14:1883, raw/<node_id> |
| Direct node challenge | NET-PROBE | POST http://10.60.20.12:8202/v1/challenge |
| Restricted control agent | NET-CONTROL | GET http://10.60.30.12:8300/healthz; GET/POST /v1/nodes/bias |

VM-NODES has static interfaces at 10.60.10.12, 10.60.20.12, 10.60.30.12 and 10.60.40.12. Administration uses `vmbr1` (VLAN 200). Docker Compose binds the NET-PROBE and NET-CONTROL listeners to their designated research-network addresses.

## Runtime and source layout

- `services/nodes/src/nodes.py`: configuration validation, virtual-node management, MQTT client, telemetry loop, and restricted control listener.
- `services/nodes/src/sensor.py`: independent read-only observation of the shared process, with optional node-local measurement bias.
- `services/nodes/src/frame.py`: per-node boot identifier, shared sequence counter, nonce generation, and 94-byte HMAC-authenticated frame creation using the shared library.
- `services/nodes/src/challenge.py`: `nonce_hex`, `node_id` request to `frame_hex` type-2 response, without traversing the telemetry gateway.

Both telemetry and challenge frames for a given node share its boot identifier and sequence state. Challenge nonces are returned unmodified within the authenticated frame. MQTT messages are published only to `raw/<node_id>` using the `node` identity. MQTT QoS 1 is used for telemetry publication.

The runtime reads process observations from VM-CONTROL and publishes authenticated telemetry. VM-CONTROL retains the private intervention record and activates node-local H2 measurement bias through the restricted control interface.

## Deployment files and confidential inputs

A **full repository checkout** at `/opt/eacld` is required for the Docker build, because the service imports `shared/eacld_contracts`. The VM-specific Compose file remains at `/opt/eacld/nodes/compose.yaml`. Start from `/opt/eacld/nodes`:

    cp config/nodes.example.json config/nodes.json

Edit `config/nodes.json` for the planned number of sensors, zones, protocol version, key versions, sampling interval and bias values. The example file defines two sample nodes; block B01 uses `node_id=1`. Provision `secrets/node_keys.json` **only on VM-NODES and to the verifier on VM-CORE** using unique cryptographically random keys, e.g.:

    {
      "1": {"1": "<64-or-more-hexadecimal-characters>"},
      "2": {"1": "<64-or-more-hexadecimal-characters>"}
    }

Create `secrets/mqtt_password` containing the broker password for the `node` MQTT account, with no surrounding quotes. These files must not be committed. The `.gitignore` excludes the entire `nodes/secrets/` directory except its placeholder, and the actual `nodes/config/nodes.json`.

    docker compose up --build -d

Both HTTP listeners run in the same container, on distinct ports. `8300` accepts requests only from 10.60.30.10 and loopback. In addition, the host network/firewall configuration must restrict NET-CONTROL to the intended source; application checks alone do not establish the network topology. Runtime H2 bias control extends the V1 health interface; it modifies only the selected virtual sensor's measurement bias.

## Verification

Run local unit/API tests from the repository root, using Python 3.12 and installed `fastapi`, `httpx`, and `uvicorn`:

    PYTHONPATH=nodes/services/nodes:shared python3 -m unittest discover -s nodes/tests -v

The local tests cover the process reader, sensor bias, wire-frame authentication and shared sequence numbers, challenge endpoint, control health access rules, configuration validation, and telemetry publish calls with a mocked broker. Deployment against Mosquitto and the five-VM network requires separate integration testing.


## H2 controlled sensor-bias intervention

The VM-NODES control listener provides in-memory, per-node measurement-bias
control to the VM-CONTROL operator. It does not alter the shared virtual
process, change keys, edit nodes.json or restart the node publisher.

Only source 10.60.30.10 (VM-CONTROL) and local loopback are authorized on the
NET-CONTROL listener 10.60.30.12:8300.

- `GET /v1/nodes/bias?node_id=1` returns the node's active temperature and
  humidity measurement biases, together with a monotonic `revision`.
- `POST /v1/nodes/bias` accepts an exact JSON object containing
  `node_id`, `bias_temperature_mC` and `bias_humidity_mPct` (signed integers).
  The response confirms the applied values and incremented revision.

Example executed from VM-CONTROL (an H2 execution should use the automated
runner rather than manually issuing this command):

```bash
curl --fail-with-body -sS -H 'Content-Type: application/json' \
  -d '{"node_id":1,"bias_temperature_mC":6912,"bias_humidity_mPct":0}' \
  http://10.60.30.12:8300/v1/nodes/bias
```

The active bias is applied to subsequent authenticated telemetry and direct
node challenges. The independent reference path remains unchanged.
A fresh VM-NODES process starts from the configured `nodes.json` values;
runtime overrides are not persisted across service restarts. The H2 runner
records the prior values and restores them in a `finally` step.

Run focused tests in the VM-NODES test environment:

```bash
PYTHONPATH=nodes/services/nodes:shared python3 -m unittest \
  discover -s nodes/tests -p test_bias_control.py -v
```
