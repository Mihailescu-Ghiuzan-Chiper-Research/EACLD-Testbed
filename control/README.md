# VM-CONTROL — eacld-control

This deployment provides the read-only virtual-process observation service and the local, private experiment-control implementation described in the EACLD technical architecture (V1, Tables 6, 7 and 9).

## Placement and interface

- Virtual machine: VM-CONTROL (Ubuntu Server 26.04 LTS)
- NET-PLANT: 10.60.40.10/24
- NET-CONTROL: 10.60.30.10/24
- Public observation: GET http://10.60.40.10:8100/v1/state
- Response: `temperature_mC`, `humidity_mPct`, `step_id`
- Health: GET http://10.60.40.10:8100/healthz

VM-NODES and VM-REFERENCE read the same virtual-process state through separate NET-PLANT acquisition services. The HTTP API exposes process observations through the read-only `/v1/state` endpoint.

## Components

The application source includes `plant.py` (deterministic discrete process), `orchestrator.py` (private operator commands and NET-CONTROL health checks), `injector.py` (private process-level interventions), `private_store.py` (transactional state, interventions and private audit history), and `app.py` (public read-only API).

The simulated plant is a configurable, first-order integer temperature/humidity process. Block B01 uses the baseline and intervention settings specified by the H1–H4 experimental protocol.

The VM-CONTROL intervention scheduler modifies the shared process for H1. VM-NODES applies the H2 measurement bias, while gateway G2 applies the H3 frame modification and H4 replay. The scheduler uses half-open step intervals `[start_step, end_step)`.

## Deployment

After configuring NET-PLANT (`vmbr640`) with 10.60.40.10/24, from `/opt/eacld/control`:

    docker compose up --build -d

The SQLite process, intervention and audit state is persisted under `/opt/eacld/control/volumes/private`. VM-CORE consumes the public observation and verification interfaces. Docker Compose binds the process service to the NET-PLANT address.

Private operator commands run **locally** on VM-CONTROL via `docker compose exec eacld-control python -m src.orchestrator ...`. Examples:

    docker compose exec eacld-control python -m src.orchestrator reset
    docker compose exec eacld-control python -m src.orchestrator inject --start-step 3 --end-step 7 --temperature-disturbance-mC 500
    docker compose exec eacld-control python -m src.orchestrator step --count 12
    docker compose exec eacld-control python -m src.orchestrator state

A fixed-step paced run can be invoked with `run --count 12 --interval-ms 1000`. Use the `private` command only on VM-CONTROL when the private audit record is needed. The database is protected by filesystem permissions; it is not exposed over HTTP. The database and audit trail are reset by the `reset` command.

The orchestrator checks remote agent readiness over NET-CONTROL using `agent-health nodes|reference|edge|core`. The H2–H4 experiment runners invoke the restricted node and gateway intervention interfaces.

## Tests

From the repository root, with Python 3.12, FastAPI and HTTPX installed:

    PYTHONPATH=control/services/control python3 -m unittest discover -s control/tests -v

Tests validate deterministic process evolution, restart persistence, private interventions, concurrent atomic stepping, CLI behavior and public HTTP response isolation.
