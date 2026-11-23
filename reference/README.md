# VM-REFERENCE — eacld-reference

VM-REFERENCE implements the EACLD reference-acquisition path (architecture V1, Tables 6, 7 and 9). It reads the shared virtual process on VM-CONTROL through its own application and NET-PLANT connection, independently of the VM-NODES telemetry path.

## Network interfaces

| Role | Proxmox network | Interface |
| --- | --- | --- |
| Read shared process | NET-PLANT / vmbr640 | GET `http://10.60.40.10:8100/v1/state` |
| Independent reference acquisition | NET-PROBE / vmbr620 | POST `http://10.60.20.13:8201/v1/reference/read` |
| Restricted control-agent health | NET-CONTROL / vmbr630 | GET `http://10.60.30.13:8300/healthz` |

VM-REFERENCE has static research IPs 10.60.20.13, 10.60.30.13, and
10.60.40.13. It has no NET-DATA interface. Administrative connectivity uses `vmbr1` (VLAN 200). Docker Compose binds the HTTP interfaces to their designated NET-PROBE and NET-CONTROL addresses.

## Reference-read contract

`POST /v1/reference/read` performs a **fresh**, read-only upstream GET on
NET-PLANT for each request. No request-body fields are required by V1.
A successful response includes **exactly**:

- `temperature_mC`: signed integer temperature from the shared process
- `status`: `ok` for a successful reference acquisition
- `timestamp`: UTC ISO 8601 timestamp when the reference acquisition finished

The status and timestamp identify each reference-acquisition result. The service validates the upstream `humidity_mPct` and `step_id` fields for consistency and returns the three documented response fields.
Unexpected upstream fields (including any private metadata) are dropped.
If the public process cannot be read, is unavailable, or sends invalid data,
this service returns HTTP 503 without inventing a measurement or revealing
upstream/private values. The NET-CONTROL health endpoint reports the state of the local listener.

The NET-CONTROL listener allows source IP 10.60.30.10 (VM-CONTROL), as well
as local 127.0.0.1 for the container health check. Its authorized monitoring route is `GET /healthz`.
Network/firewall controls on the host must also restrict this traffic.

## Deployment

Keep a full repository checkout under `/opt/eacld` because the Docker build
uses the shared EACLD interface package. On VM-REFERENCE, after assigning
NET-PLANT 10.60.40.13/24, NET-PROBE 10.60.20.13/24 and NET-CONTROL
10.60.30.13/24, run from `/opt/eacld/reference`:

    docker compose up --build -d

The service reads the shared process and provides reference measurements through `services/reference/src/reference.py`. The reference component was deployed on Ubuntu Server 26.04 LTS as part of the five-VM B01 execution.

## Unit tests

From the repository root with Python 3.12, FastAPI, HTTPX, and Uvicorn:

    PYTHONPATH=reference/services/reference:shared python3 -m unittest discover -s reference/tests -v

Unit tests use an injected process reader to verify fresh measurements, field isolation, response validation, failure behavior, source restrictions and both APIs.
