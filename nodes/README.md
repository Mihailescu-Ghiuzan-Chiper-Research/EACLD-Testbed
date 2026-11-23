# VM-NODES — eacld-nodes

This service implements the EACLD virtual sensor population, authenticated MQTT telemetry, direct active node challenges, and the NET-CONTROL health endpoint specified in architecture V1 (Tables 6–9).

## Network interfaces

| Function | Network | Destination |
| --- | --- | --- |
| Shared virtual-process reading | NET-PLANT | GET http://10.60.40.10:8100/v1/state |
| Raw telemetry publication | NET-DATA | MQTT 10.60.10.14:1883, raw/<node_id> |
| Direct node challenge | NET-PROBE | POST http://10.60.20.12:8202/v1/challenge |
| Restricted agent health | NET-CONTROL | GET http://10.60.30.12:8300/healthz |

VM-NODES has static interfaces at 10.60.10.12, 10.60.20.12, 10.60.30.12 and 10.60.40.12. System administration is separate on VMnet8. The Compose configuration publishes only the NET-PROBE and NET-CONTROL listeners on their respective research-network IPs; it does not publish ports on VMnet8.

## Runtime and source layout

- `services/nodes/src/nodes.py`: configuration validation, virtual-node management, MQTT client, telemetry loop, and control health listener.
- `services/nodes/src/sensor.py`: independent read-only observation of the shared process, with optional node-local measurement bias.
- `services/nodes/src/frame.py`: per-node boot identifier, shared sequence counter, nonce generation, and 94-byte HMAC-authenticated frame creation using the shared library.
- `services/nodes/src/challenge.py`: `nonce_hex`, `node_id` request to `frame_hex` type-2 response, without traversing the telemetry gateway.

Both telemetry and challenge frames for a given node share its boot identifier and sequence state. Challenge nonces are returned unmodified within the authenticated frame. MQTT messages are published only to `raw/<node_id>` using the `node` identity. QoS 1 is an implementation choice pending the experiment protocol; it does not change the wire format.

The runtime independently reads VM-CONTROL for observations. It receives no private intervention labels, experiment truth, or injector configuration. Node-local bias values are implementation settings; their experimental activation/schedules are defined outside this software specification.

## Deployment files and confidential inputs

A **full repository checkout** at `/opt/eacld` is required for the Docker build, because the service imports `shared/eacld_contracts`. The VM-specific Compose file remains at `/opt/eacld/nodes/compose.yaml`. Start from `/opt/eacld/nodes`:

    cp config/nodes.example.json config/nodes.json

Edit `config/nodes.json` for the planned number of sensors, zones, protocol version, key versions, sampling interval and bias values. The example defines two illustrative nodes; these are not experimental population assumptions. Provision `secrets/node_keys.json` **only on VM-NODES and to the verifier on VM-CORE** using unique cryptographically random keys, e.g.:

    {
      "1": {"1": "<64-or-more-hexadecimal-characters>"},
      "2": {"1": "<64-or-more-hexadecimal-characters>"}
    }

Create `secrets/mqtt_password` containing the broker password for the `node` MQTT account, with no surrounding quotes. These files must not be committed. The `.gitignore` excludes the entire `nodes/secrets/` directory except its placeholder, and the actual `nodes/config/nodes.json`.

    docker compose up --build -d

Both HTTP listeners run in the same container, on distinct ports. `8300` accepts requests only from 10.60.30.10 and loopback. In addition, the host network/firewall configuration must restrict NET-CONTROL to the intended source; application checks alone do not establish the network topology. Agent configuration operations beyond health are not specified in V1 and have not been invented here.

## Verification

Run local unit/API tests from the repository root, using Python 3.12 and installed `fastapi`, `httpx`, and `uvicorn`:

    PYTHONPATH=nodes/services/nodes:shared python3 -m unittest discover -s nodes/tests -v

The local tests cover the process reader, sensor bias, wire-frame authentication and shared sequence numbers, challenge endpoint, control health access rules, configuration validation, and telemetry publish calls with a mocked broker. Deployment against Mosquitto and the five-VM network requires separate integration testing.
