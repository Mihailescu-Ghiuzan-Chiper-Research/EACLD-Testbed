# Shared EACLD contracts

This Python 3.12 package provides common interfaces for the distributed services specified in the EACLD experimental testbed architecture, V1 (Tables 7–9). It is a reusable library, not an additional runtime service or virtual machine.

## Binary frame

`eacld_contracts.frame` encodes and decodes the fixed 94-byte frame: an authenticated 62-byte big-endian header and a 32-byte HMAC-SHA256 authenticator.

The fields, in wire order, are `version` (1 byte), `type` (1), `node_id` (2), `zone_id` (1), `key_version` (1), `boot_id` (16), `sequence_no` (8), `uptime_ms` (8), `temperature_mC` (4 signed), `humidity_mPct` (4 unsigned), `nonce` (16), and `mac` (32). Message type 1 is telemetry; message type 2 is a challenge response. Callers supply `version` explicitly: the architecture does not assign its initial numerical value.

`sign_frame` calculates the MAC over the entire header. `verify_frame` validates authentication using constant-time comparison. **Authentication is separate from sequence and replay checks**, which are owned by VM-CORE's verification service. VM-EDGE only forwards or deliberately alters frames; it must not receive node HMAC keys.

## MQTT and HTTP contracts

`topics.py` defines `raw/<node_id>`, `transit/g1/<node_id>`, `accepted/<node_id>`, subscription patterns, and the four MQTT principals and their read/write topic permissions.

`interfaces.py` defines the three explicitly addressed HTTP endpoints (`GET /v1/state`, `POST /v1/reference/read`, `POST /v1/challenge`), the control-agent health endpoint, MQTT broker address, and the declared JSON fields of their exchanges. Unspecified JSON field types and service-specific behavior are intentionally left to the implementing services.

## Run unit tests

From the repository root, with Python 3.12:

    PYTHONPATH=shared python3 -m unittest discover -s shared/tests -v

The package has no third-party runtime dependencies.
