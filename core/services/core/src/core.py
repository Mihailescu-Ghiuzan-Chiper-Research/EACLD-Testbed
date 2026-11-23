"""VM-CORE: signed MQTT intake, live E0 diagnosis and NET-CONTROL API."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import threading

import paho.mqtt.client as mqtt
import psycopg

from eacld_contracts.topics import ACCEPTED_SUBSCRIPTION

from .ingest import PostgresStore
from .experiment_api import (
    TelemetryChanged, parse_node, parse_episode,
    summarize_reading, summarize_diagnosis,
)
from .runner import DiagnosisRunner, load_engine
from .verifier import FrameVerifier, load_keyring

LOGGER = logging.getLogger(__name__)
VERSION = "0.1.0"
CONTROL_SOURCE = "10.60.30.10"
CORE_HOST = "0.0.0.0"
CORE_PORT = 8300
MAX_REQUEST = 2048


class CoreMqtt:
    def __init__(self, store: PostgresStore, verifier: FrameVerifier):
        self.store = store
        self.verifier = verifier
        self.ready = threading.Event()
        self._subscription_mid = None

    def on_connect(self, client, _userdata, _flags, reason, _properties) -> None:
        self.ready.clear()
        if reason != 0:
            LOGGER.error("VM-CORE MQTT connection rejected: %s", reason)
            return
        code, mid = client.subscribe(ACCEPTED_SUBSCRIPTION, qos=1)
        if code == 0:
            self._subscription_mid = mid
        else:
            LOGGER.error("VM-CORE MQTT subscribe failed: %s", code)

    def on_subscribe(self, _client, _userdata, mid, reasons, _properties) -> None:
        if mid == self._subscription_mid and reasons and not any(x.is_failure for x in reasons):
            self.ready.set()

    def on_disconnect(self, _client, _userdata, _flags, _reason, _properties) -> None:
        self.ready.clear()
        self._subscription_mid = None

    def on_message(self, _client, _userdata, message) -> None:
        try:
            self.store.append(message.topic, message.payload, self.verifier)
        except (ValueError, OSError, psycopg.Error):
            LOGGER.exception("VM-CORE failed to ingest accepted telemetry")


def _valid_request(request: object, modes: list[str]) -> dict:
    required = {
        "node_id", "mode", "episode_id", "block_id",
        "baseline_temperature_mC", "high_delta_mC"
    }
    if not isinstance(request, dict) or not (
        set(request) == required or
        set(request) == required | {"expected_telemetry_id"}
    ):
        raise ValueError("invalid diagnosis request fields")
    if "expected_telemetry_id" in request:
        expected = request["expected_telemetry_id"]
        if type(expected) is not int or expected < 1 or expected >= 1 << 63:
            raise ValueError("invalid expected telemetry identifier")
    node = request["node_id"]
    if type(node) is not int or not 0 <= node <= 65535:
        raise ValueError("invalid node_id")
    if request["mode"] not in modes:
        raise ValueError("invalid network mode")
    for field in ("episode_id", "block_id"):
        val = request[field]
        if not isinstance(val, str) or not 1 <= len(val) <= 128:
            raise ValueError("invalid experiment identifier")
    baseline = request["baseline_temperature_mC"]
    delta = request["high_delta_mC"]
    if type(baseline) is not int or not -(1 << 31) <= baseline < (1 << 31):
        raise ValueError("invalid baseline")
    if type(delta) is not int or not 1 <= delta <= 100000000:
        raise ValueError("invalid threshold")
    return request


def create_control_server(runner: DiagnosisRunner, gateway: CoreMqtt, *,
                          host: str = CORE_HOST, port: int = CORE_PORT,
                          allowed_source: str = CONTROL_SOURCE) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, code: int, payload: dict) -> None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def allowed(self, *, health: bool = False) -> bool:
            source = self.client_address[0]
            return source == allowed_source or (health and source == "127.0.0.1")

        def do_GET(self) -> None:
            if not self.allowed(health=True):
                self.send_json(403, {"detail": "forbidden"})
                return
            if self.path == "/healthz":
                online = gateway.ready.is_set()
                self.send_json(200 if online else 503, {
                    "status": "ok" if online else "unavailable",
                    "service_version": VERSION
                })
                return
            if not self.allowed():
                self.send_json(403, {"detail": "forbidden"})
                return
            if self.path.startswith("/v1/telemetry/latest?"):
                try:
                    node_id = parse_node(self.path)
                except ValueError:
                    self.send_json(400, {"detail": "invalid telemetry query"})
                    return
                try:
                    reading = runner.store.latest(node_id)
                except (OSError, psycopg.Error):
                    LOGGER.exception("VM-CORE telemetry lookup failed")
                    self.send_json(503, {"detail": "telemetry backend unavailable"})
                    return
                if reading is None:
                    self.send_json(404, {"detail": "no telemetry for node"})
                else:
                    self.send_json(200, summarize_reading(reading))
                return
            if self.path.startswith("/v1/diagnosis?"):
                try:
                    episode_id = parse_episode(self.path)
                except ValueError:
                    self.send_json(400, {"detail": "invalid diagnosis query"})
                    return
                try:
                    record = runner.store.get_diagnosis(episode_id)
                except (OSError, psycopg.Error, ValueError):
                    LOGGER.exception("VM-CORE diagnosis evidence lookup failed")
                    self.send_json(503, {"detail": "diagnosis evidence unavailable"})
                    return
                if record is None:
                    self.send_json(404, {"detail": "unknown diagnosis episode"})
                else:
                    self.send_json(200, summarize_diagnosis(record))
                return
            self.send_json(404, {"detail": "not found"})

        def do_POST(self) -> None:
            if not self.allowed():
                self.send_json(403, {"detail": "forbidden"})
                return
            if self.path != "/v1/diagnose":
                self.send_json(404, {"detail": "not found"})
                return
            if not gateway.ready.is_set():
                self.send_json(503, {"detail": "MQTT not ready"})
                return
            try:
                n = int(self.headers.get("Content-Length", "-1"))
                if not 1 <= n <= MAX_REQUEST:
                    raise ValueError("invalid request length")
                kwargs = _valid_request(json.loads(self.rfile.read(n)), runner.config["modes"])
            except (ValueError, TypeError, json.JSONDecodeError):
                self.send_json(400, {"detail": "invalid diagnosis request"})
                return
            try:
                result = runner.diagnose(**kwargs)
            except LookupError:
                self.send_json(404, {"detail": "no accepted telemetry for requested node"})
                return
            except TelemetryChanged:
                self.send_json(409, {"detail": "telemetry changed before diagnosis"})
                return
            except psycopg.errors.UniqueViolation:
                self.send_json(409, {"detail": "duplicate experiment episode_id"})
                return
            except (OSError, psycopg.Error, ArithmeticError):
                LOGGER.exception("VM-CORE diagnosis failed")
                self.send_json(503, {"detail": "diagnosis backend unavailable"})
                return
            self.send_json(200, result)

        def log_message(self, fmt: str, *args) -> None:
            LOGGER.info("%s %s", self.client_address[0], fmt % args)

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    password = Path(os.environ.get("EACLD_PG_PASSWORD_FILE",
                "/run/eacld/pg_password")).read_text(encoding="utf-8").rstrip("\r\n")
    mqtt_password = Path(os.environ.get("EACLD_CORE_MQTT_PASSWORD_FILE",
                "/run/eacld/mqtt_password")).read_text(encoding="utf-8").rstrip("\r\n")
    if not password or not mqtt_password:
        raise ValueError("database and broker passwords are required")
    conninfo = psycopg.conninfo.make_conninfo(
        host=os.environ.get("EACLD_PG_HOST", "postgres"),
        port=int(os.environ.get("EACLD_PG_PORT", "5432")),
        dbname=os.environ.get("EACLD_PG_DB", "eacld"),
        user=os.environ.get("EACLD_PG_USER", "eacld"),
        password=password
    )
    store = PostgresStore(conninfo)
    store.initialize()
    verifier = FrameVerifier(load_keyring(os.environ.get(
        "EACLD_CORE_NODE_KEYS", "/run/eacld/node_keys.json")))
    engine, cfg = load_engine(
        config=os.environ.get("EACLD_E0_CONFIG",
            "/app/vendor/EACLD_E0_Experimental_Package/config/e0_config.json"),
        fitted=os.environ.get("EACLD_E0_FITTED",
            "/app/vendor/EACLD_E0_Experimental_Package/results/fitted_model.json"),
    )
    runner = DiagnosisRunner(engine=engine, config=cfg, store=store, verifier=verifier)
    gateway = CoreMqtt(store, verifier)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="eacld-core", protocol=mqtt.MQTTv311)
    client.username_pw_set("core", mqtt_password)
    client.on_connect = gateway.on_connect
    client.on_subscribe = gateway.on_subscribe
    client.on_disconnect = gateway.on_disconnect
    client.on_message = gateway.on_message
    client.reconnect_delay_set(min_delay=1, max_delay=20)

    server = create_control_server(runner, gateway)
    thread = threading.Thread(target=server.serve_forever,
                              name="core-control", daemon=True)
    thread.start()
    try:
        client.connect_async(os.environ.get("EACLD_MQTT_HOST", "10.60.10.14"),
                             int(os.environ.get("EACLD_MQTT_PORT", "1883")),
                             keepalive=30)
        client.loop_forever(retry_first_connection=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
