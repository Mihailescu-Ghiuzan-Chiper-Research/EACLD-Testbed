# VM-EDGE: Mosquitto, eacld-g1 and eacld-g2

This directory contains the VM-EDGE MQTT broker and both gateway stages defined in the EACLD technical architecture (V1). The eacld-edge-control agent is implemented and communicates privately with G2 over a shared Unix socket.

## Transport and access boundaries

The Mosquitto 2.x broker listens at **10.60.10.14:1883** on NET-DATA (VMnet2). The host-port mapping is limited to that address; no EACLD application port is published on the ADMIN interface (VMnet8). G1 connects to the broker through the local Docker network using `mosquitto:1883`, without a host-published listener.

The four distinct broker identities and topic ACLs are defined in `config/acl`:

| Principal | Subscribe | Publish |
| --- | --- | --- |
| node | None | `raw/+` |
| g1 | `raw/+` | `transit/g1/+` |
| g2 | `transit/g1/+` | `accepted/+` |
| core | `accepted/+` | None |

The four identities are provisioned in the **private, hashed** `config/passwd` database, which is excluded from Git. Each must use a distinct password. Do not disable broker authentication or expand the topic permissions.

G1 consumes a message from `raw/<node_id>` and republishes the payload, byte for byte, to `transit/g1/<node_id>`. It neither checks nor recalculates HMAC, changes frame contents, stores node keys, or suppresses duplicate messages. VM-CORE is responsible for authenticity and freshness verification. G2 subscribes to `transit/g1/+` and forwards byte-identical payloads to `accepted/<node_id>` when no intervention is staged. Its one-shot `stage_mutation(node_id, offset, xor_mask)` operation changes one authenticated header byte in a 94-byte frame while leaving the HMAC unchanged. Its `stage_replay(node_id)` operation retransmits a previously queued frame after forwarding the next incoming frame. The private G2 control socket exposes these primitives to eacld-edge-control. The NET-CONTROL agent accepts commands only from VM-CONTROL; experimental activation and scheduling remain under VM-CONTROL orchestration.

The gateway uses MQTT QoS 1 for its incoming subscription and outgoing publish. QoS 1 can deliver duplicates; G1 does not remove them, so the downstream verifier can observe replay/freshness conditions. All G1 output messages use `retain=False`. A successful publish request is not proof that downstream G2 or VM-CORE received a frame.

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

The G1, G2 and edge-control tests check topic mapping, byte preservation (including invalid MACs), subscription state, container-local health, broker ACLs, controlled header modification, replay and Compose port boundaries. Local callback tests use a fake MQTT client; they do **not** establish that a live Mosquitto broker or the distributed VM deployment works. Those checks remain for the integration stage.

Run the additional private control tests with:

    PYTHONPATH=edge/services/g2:shared python3 -m unittest discover -s edge/tests -p test_edge_control.py -v

G2/agent roundtrip tests use local Unix sockets; no live VM-EDGE deployment has been verified.


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
