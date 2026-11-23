# VM-EDGE: Mosquitto, eacld-g1 and eacld-g2

This directory contains the VM-EDGE MQTT broker and both gateway stages defined in the EACLD technical architecture (V1). The eacld-edge-control agent is implemented and communicates privately with G2 over a shared Unix socket.

## Transport and access boundaries

The Mosquitto 2.x broker listens at **10.60.10.14:1883** on NET-DATA (`vmbr610`). The host-port mapping is limited to that address; the ADMIN interface (`vmbr1`, VLAN 200) is isolated from EACLD application listeners. G1 connects to the broker through the local Docker network using `mosquitto:1883`, without a host-published listener.

The four distinct broker identities and topic ACLs are defined in `config/acl`:

| Principal | Subscribe | Publish |
| --- | --- | --- |
| node | None | `raw/+` |
| g1 | `raw/+` | `transit/g1/+` |
| g2 | `transit/g1/+` | `accepted/+` |
| core | `accepted/+` | None |

The four identities are provisioned in the **private, hashed** `config/passwd` database, which is excluded from Git. Each must use a distinct password. Do not disable broker authentication or expand the topic permissions.

G1 republishes each `raw/<node_id>` payload unchanged to `transit/g1/<node_id>`. VM-CORE performs HMAC verification and sequence/freshness classification; the gateway operates on the original frame bytes. G2 subscribes to `transit/g1/+` and forwards byte-identical payloads to `accepted/<node_id>` when no intervention is staged. Its one-shot `stage_mutation(node_id, offset, xor_mask)` operation changes one authenticated header byte in a 94-byte frame while leaving the HMAC unchanged. Its `stage_replay(node_id)` operation retransmits a previously queued frame after forwarding the next incoming frame. The private G2 control socket exposes these primitives to eacld-edge-control. The NET-CONTROL agent accepts commands only from VM-CONTROL; experimental activation and scheduling remain under VM-CONTROL orchestration.

The gateway uses MQTT QoS 1 for its incoming subscription and outgoing publish. MQTT QoS 1 permits duplicate delivery; VM-CORE classifies the resulting frame sequence. G1 publishes messages with `retain=False`, and VM-CORE persists accepted-topic telemetry for diagnostic correlation.

## Source and deployment

- `services/g1/src/g1.py`: first-stage topic mapping, Paho MQTT callback handling and container-local health endpoint.
- `services/g2/src/g2.py`: second-stage forwarding, one-shot mutation/replay, local health endpoint, and Unix-domain control socket.
- `services/edge-control/src/agent.py`: NET-CONTROL HTTP agent with source address checks and a private G2 Unix socket.
- `services/g1/Dockerfile`, `services/g1/requirements.txt`: G1 Python 3.12 container image.
- `services/g2/Dockerfile`, `services/g2/requirements.txt`: G2 Python 3.12 container image.
- `services/edge-control/Dockerfile`: NET-CONTROL agent container.
- `config/mosquitto.conf`, `config/acl`: V1 broker configuration.
- `compose.yaml`: Mosquitto, G1, G2 and edge-control. The shared g2-control volume carries only the private socket.
- `volumes/mosquitto`: runtime persistence on VM-EDGE only.

On VM-EDGE, check out the full repository under `/opt/eacld` and use `/opt/eacld/edge` as the working directory:

    cd /opt/eacld/edge

Before `docker compose up`, **provision the private authentication files**. Create `config/passwd` using the `mosquitto_passwd` utility from Mosquitto 2.x. The password entries must be generated for **node**, **g1**, **g2**, and **core**, using four distinct passwords. For example, when the Mosquitto package is installed on VM-EDGE:

    mosquitto_passwd -c config/passwd node
    mosquitto_passwd config/passwd g1
    mosquitto_passwd config/passwd g2
    mosquitto_passwd config/passwd core

Do not use `-c` after the initial command: it recreates the password database and would remove prior identities. Give the broker container's user read access to `config/passwd`, without making the file world-readable. Provision `secrets/g1_mqtt_password` and `secrets/g2_mqtt_password` containing the distinct plaintext **g1** and **g2** passwords, respectively, with a final newline. Each is mounted read-only into its corresponding container; neither is mounted into VM-NODES or VM-CORE. The separate `node` password is provisioned on VM-NODES using its own secret file. Do not commit either password file.

Ensure that `/opt/eacld/edge/volumes/mosquitto` is writable by the Mosquitto container's user, then start the three available components:

    docker compose up --build -d

Each gateway container exposes `GET /healthz` on **127.0.0.1:8300 inside the container only**. It reports `ok` after the broker acknowledges its subscription (`raw/+` for G1, `transit/g1/+` for G2), and `unavailable` otherwise. This endpoint is not published to any VM interface; it is intended for the Docker health check. The edge-control service binds only to VM-EDGE's NET-CONTROL IP 10.60.30.14:8300 using host networking, preserving the VM-CONTROL caller address. G2 remains on its private Docker network and does not publish any application port. GET /healthz is exposed for authorized health checks. POST /v1/g2/intervention requires a VM-CONTROL (10.60.30.10) source and a body such as {"action":"mutate","node_id":12,"offset":10,"xor_mask":1}, {"action":"replay","node_id":12}, or {"action":"clear","node_id":12}. These commands stage the next selected G2 action; they do not select research scenarios or automate intervention timing.

## Local verification

From the repository root, with Python 3.12:

    PYTHONPATH=edge/services/g1:shared python3 -m unittest discover -s edge/tests -p 'test_g1.py' -v
    PYTHONPATH=edge/services/g2:shared python3 -m unittest discover -s edge/tests -p 'test_g2.py' -v

The G1, G2 and edge-control tests check topic mapping, byte preservation (including invalid MACs), subscription state, container-local health, broker ACLs, controlled header modification, replay and Compose port boundaries. The local callback suite uses a simulated MQTT client. Distributed message transport, mutation and replay were exercised during block B01 on the five-VM testbed.

Run the additional private control tests with:

    PYTHONPATH=edge/services/g2:shared python3 -m unittest discover -s edge/tests -p test_edge_control.py -v

G2/agent roundtrip unit tests use a local Unix socket. The VM-EDGE services also participated in the distributed H1–H4 B01 execution.


## H3 gateway evidence

For controlled H3 experiments, G2 records the exact original and mutated
94-byte frames when a one-shot mutation is successfully queued for MQTT
forwarding. The evidence includes a monotonic per-process `event_id`,
the byte offset, XOR mask and both frame payloads as hexadecimal strings.
The HMAC bytes are not recalculated.

VM-CONTROL may inspect:

```text
GET http://10.60.30.14:8300/v1/g2/intervention/status?node_id=1
```

The response contains `status`, `node_id`, `pending_action`
(`mutate`, `replay` or `null`) and `last_mutation` (an object or
`null`). Status requests are authorized only for the VM-CONTROL source,
unlike the local health check. The corresponding G2 Unix-socket command
is `{"action":"status","node_id":1}`.

`last_mutation` remains available after the one-shot action is consumed
or cleared, until another mutation of that node or a G2 service restart.
Its publication counter indicates a successful *queue request*; reception
and HMAC verification are confirmed independently from VM-CORE, using
an exact stored-frame lookup.

The executable operator procedure is in
`control/experiments/h3_run.py`; its usage and artifact names are
documented under `control/experiments/README.md`.

Local focused tests:

```bash
PYTHONPATH=edge/services/g2:shared python3 -m unittest \
  discover -s edge/tests -p test_h3_mutation_evidence.py -v
```


## H4 replay capture and evidence

G2 keeps up to 64 recent successfully forwarded 94-byte frames per node.
The H4 operator first identifies an authentic, fresh reference frame in
VM-CORE and then stages the exact captured payload via:

```text
POST http://10.60.30.14:8300/v1/g2/intervention
{"action":"replay","node_id":1,"frame_hex":"<188 lowercase hexadecimal characters>"}
```

The optional `frame_hex` extension selects a frame already in G2's
recent forwarding history. The original `{"action":"replay","node_id":1}`
control command remains available for previous users of the G2 one-shot
primitive. An H4 experiment must use the exact-frame form.

When a new sensor telemetry frame of the same node and boot arrives with
a larger sequence number, G2 forwards it, then publishes the saved
reference frame byte for byte (including the original 32-byte HMAC).
No signing keys are used by G2.

The restricted status endpoint described under H3 also exposes
`last_replay`: a monotonically numbered event with
`reference_frame_hex`, `trigger_frame_hex` and
`replayed_frame_hex`. Both types of intervention events remain available
until the next event of the same type for that node or a G2 restart.
Status replies may exceed 1024 bytes when H3 and H4 events are both
present; the agent supports replies up to 4096 bytes.

VM-CONTROL compares G2's event with the exact stored VM-CORE records.
G2's successful publish-request result is cross-checked against the
authenticated/fresh triggering record and authentic/stale replay record in
VM-CORE. H4 cleanup uses the same `clear` command as H3.

Focused tests:

```bash
PYTHONPATH=edge/services/g2:shared python3 -m unittest \
  discover -s edge/tests -p test_h4_replay_evidence.py -v
```
