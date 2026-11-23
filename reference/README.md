# VM-REFERENCE — eacld-reference

This service implements the independent reference-acquisition path in EACLD
architecture V1 (Tables 6, 7, and 9). It **does not** use VM-NODES, MQTT,
VM-EDGE, node HMAC keys, or private intervention data. It reads the same public
process hosted by VM-CONTROL that the virtual sensors observe, using a separate
application and communication path.

## Network interfaces

| Role | VMware network | Interface |
| --- | --- | --- |
| Read shared process | NET-PLANT / VMnet5 | GET `http://10.60.40.10:8100/v1/state` |
| Independent reference acquisition | NET-PROBE / VMnet3 | POST `http://10.60.20.13:8201/v1/reference/read` |
| Restricted control-agent health | NET-CONTROL / VMnet4 | GET `http://10.60.30.13:8300/healthz` |

VM-REFERENCE has static research IPs 10.60.20.13, 10.60.30.13, and
10.60.40.13. It has no NET-DATA interface. Administrative connectivity on
VMnet8 must not expose application ports. Docker Compose binds incoming
HTTP services only to the intended NET-PROBE and NET-CONTROL host addresses.

## Reference-read contract

`POST /v1/reference/read` performs a **fresh**, read-only upstream GET on
NET-PLANT for each request. No request-body fields are required by V1.
A successful response includes **exactly**:

- `temperature_mC`: signed integer temperature from the shared process
- `status`: `ok` for a successful reference acquisition
- `timestamp`: UTC ISO 8601 timestamp when the reference acquisition finished

The status value and timestamp representation are implementation conventions,
not experiment outcomes. The upstream `humidity_mPct` and `step_id` fields
are validated for consistency, but are not returned by the reference API.
Unexpected upstream fields (including any private metadata) are dropped.
If the public process cannot be read, is unavailable, or sends invalid data,
this service returns HTTP 503 without inventing a measurement or revealing
upstream/private values. The control health endpoint reports the local
listener's status only; it is not a guarantee that VM-CONTROL is reachable.

The NET-CONTROL listener allows source IP 10.60.30.10 (VM-CONTROL), as well
as local 127.0.0.1 for the container health check. Its only route is
GET `/healthz`; no experiment control operations are implemented at this stage.
Network/firewall controls on the host must also restrict this traffic.

## Deployment

Keep a full repository checkout under `/opt/eacld` because the Docker build
uses the shared EACLD interface package. On VM-REFERENCE, after assigning
NET-PLANT 10.60.40.13/24, NET-PROBE 10.60.20.13/24 and NET-CONTROL
10.60.30.13/24, run from `/opt/eacld/reference`:

    docker compose up --build -d

The container does not require MQTT credentials, HMAC keys, a local reference
truth database, or a separate experimental process. The source code is in
`services/reference/src/reference.py`. Deployment verification on the target
Ubuntu 24.04 VM is pending.

## Unit tests

From the repository root with Python 3.12, FastAPI, HTTPX, and Uvicorn:

    PYTHONPATH=reference/services/reference:shared python3 -m unittest discover -s reference/tests -v

The tests use an injected process reader instead of making a connection to
an unprovisioned VM. They verify fresh reads, public-field isolation,
response validation, failure behavior, source restrictions, and both APIs.
