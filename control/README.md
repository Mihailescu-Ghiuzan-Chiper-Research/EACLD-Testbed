# VM-CONTROL — eacld-control

This deployment provides the read-only virtual-process observation service and the local, private experiment-control implementation described in the EACLD technical architecture (V1, Tables 6, 7 and 9).

## Placement and interface

- Virtual machine: VM-CONTROL (Ubuntu Server 24.04 LTS)
- NET-PLANT: 10.60.40.10/24
- NET-CONTROL: 10.60.30.10/24
- Public observation: GET http://10.60.40.10:8100/v1/state
- Response: `temperature_mC`, `humidity_mPct`, `step_id`
- Health: GET http://10.60.40.10:8100/healthz

VM-NODES and VM-REFERENCE independently read the same virtual-process state through NET-PLANT. The public HTTP API has no mutation or private-state endpoints.

## Components

The application source includes `plant.py` (deterministic discrete process), `orchestrator.py` (private operator commands and NET-CONTROL health checks), `injector.py` (private process-level interventions), `private_store.py` (transactional state, interventions and private audit history), and `app.py` (public read-only API).

The initial model is an integer, first-order thermal/humidity process. Its parameters and initial conditions are **configurable implementation defaults**, not calibrated experimental parameters or an approved experimental scenario. The experimental protocol will specify values and schedules for each run.

The intervention scheduler in VM-CONTROL affects the shared process (H1). Sensor bias (H2), post-authentication frame modification (H3) and replay (H4) are implemented in their respective services, not here. The scheduler uses half-open step intervals `[start_step, end_step)`.

## Deployment

After configuring VMnet5 with 10.60.40.10/24, from `/opt/eacld/control`:

    docker compose up --build -d

Private SQLite state is persisted only under `/opt/eacld/control/volumes/private`. No private data is mounted into VM-CORE. The Compose port is bound to the NET-PLANT IP, not ADMIN (VMnet8).

Private operator commands run **locally** on VM-CONTROL via `docker compose exec eacld-control python -m src.orchestrator ...`. Examples:

    docker compose exec eacld-control python -m src.orchestrator reset
    docker compose exec eacld-control python -m src.orchestrator inject --start-step 3 --end-step 7 --temperature-disturbance-mC 500
    docker compose exec eacld-control python -m src.orchestrator step --count 12
    docker compose exec eacld-control python -m src.orchestrator state

A fixed-step paced run can be invoked with `run --count 12 --interval-ms 1000`. Use the `private` command only on VM-CONTROL when the private audit record is needed. The database is protected by filesystem permissions; it is not exposed over HTTP. The database and audit trail are reset by the `reset` command.

Only agent health checks are implemented on NET-CONTROL at this stage (`agent-health nodes|reference|edge|core`). Remote agent-configuration requests are not invented: their API operations will be implemented consistently when the relevant agent services are developed.

## Tests

From the repository root, with Python 3.12, FastAPI and HTTPX installed:

    PYTHONPATH=control/services/control python3 -m unittest discover -s control/tests -v

Tests validate deterministic process evolution, restart persistence, private interventions, concurrent atomic stepping, CLI behavior and public HTTP response isolation.
