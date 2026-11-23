"""EACLD gateway stage G1: byte-preserving MQTT forwarding.

G1 only maps raw/<node_id> to transit/g1/<node_id>. It deliberately
never parses the frame, checks HMAC, alters payloads, or accesses keys.
Any authentication or freshness failure is VM-CORE's responsibility.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import threading

from eacld_contracts.topics import RAW_SUBSCRIPTION, g1_transit_topic

LOGGER = logging.getLogger(__name__)
SERVICE_VERSION = "0.1.0"
DEFAULT_MQTT_HOST = "mosquitto"
DEFAULT_MQTT_PORT = 1883
DEFAULT_PASSWORD_FILE = "/run/eacld/mqtt_password"
HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 8300


def route_raw_topic(topic: str) -> str:
    """Map one canonical numeric raw topic without inspecting the payload."""
    if not isinstance(topic, str):
        raise ValueError("MQTT topic must be text")
    parts = topic.split("/")
    if len(parts) != 2 or parts[0] != "raw":
        raise ValueError("only raw/<node_id> is accepted")
    segment = parts[1]
    if not segment or not segment.isascii() or not segment.isdecimal():
        raise ValueError("node_id must use ASCII decimal digits")
    node_id = int(segment)
    if str(node_id) != segment or node_id > 65535:
        raise ValueError("node_id must be canonical unsigned 16-bit decimal")
    return g1_transit_topic(node_id)


class Gateway1:
    """MQTT callbacks for transparent, QoS-1 forwarding.

    Ready means the broker acknowledged the raw/+ subscription. It does
    not imply end-to-end connectivity to the downstream G2 or VM-CORE.
    """

    def __init__(self) -> None:
        self.ready = threading.Event()
        self._subscription_mid: int | None = None

    def on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        self.ready.clear()
        self._subscription_mid = None
        if reason_code != 0:
            LOGGER.error("G1 MQTT connection rejected: %s", reason_code)
            return
        result, mid = client.subscribe(RAW_SUBSCRIPTION, qos=1)
        if result != 0:
            LOGGER.error("G1 MQTT subscription request failed: %s", result)
            return
        self._subscription_mid = mid

    def on_subscribe(self, _client, _userdata, mid, reason_codes, _properties) -> None:
        if mid != self._subscription_mid:
            return
        if not reason_codes or any(code.is_failure for code in reason_codes):
            self.ready.clear()
            LOGGER.error("G1 MQTT subscription was rejected")
            return
        self.ready.set()

    def on_disconnect(self, _client, _userdata, _flags, _reason_code, _properties) -> None:
        self.ready.clear()
        self._subscription_mid = None

    def on_message(self, client, _userdata, message) -> None:
        try:
            target = route_raw_topic(message.topic)
        except ValueError:
            LOGGER.warning("G1 ignored message on an invalid raw topic")
            return
        # Pass bytes through untouched, including any deliberately invalid MAC.
        # No validation, re-signing, transformation, or deduplication is done.
        payload = message.payload
        if not isinstance(payload, bytes):
            LOGGER.warning("G1 ignored an MQTT payload that was not bytes")
            return
        result = client.publish(target, payload, qos=1, retain=False)
        if result.rc != 0:
            LOGGER.error("G1 MQTT forwarding failed with code %s", result.rc)


def create_health_server(gateway: Gateway1, *, host: str = HEALTH_HOST,
                         port: int = HEALTH_PORT) -> ThreadingHTTPServer:
    """Container-local health endpoint. No host-port publication is required."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/healthz":
                self.send_error(404)
                return
            healthy = gateway.ready.is_set()
            body = json.dumps({"status": "ok" if healthy else "unavailable",
                               "service_version": SERVICE_VERSION}).encode("utf-8")
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    """Connect to the internal Mosquitto service and run until stopped."""
    import paho.mqtt.client as mqtt

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    password_path = Path(os.environ.get("EACLD_G1_MQTT_PASSWORD_FILE", DEFAULT_PASSWORD_FILE))
    password = password_path.read_text(encoding="utf-8").rstrip("\r\n")
    if not password:
        raise ValueError("G1 MQTT password file is empty")

    gateway = Gateway1()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="eacld-g1",
                         protocol=mqtt.MQTTv311)
    client.username_pw_set("g1", password)
    client.on_connect = gateway.on_connect
    client.on_subscribe = gateway.on_subscribe
    client.on_disconnect = gateway.on_disconnect
    client.on_message = gateway.on_message
    client.reconnect_delay_set(min_delay=1, max_delay=20)

    health_server = create_health_server(gateway)
    health_thread = threading.Thread(target=health_server.serve_forever,
                                     name="g1-health", daemon=True)
    health_thread.start()
    try:
        host = os.environ.get("EACLD_MQTT_HOST", DEFAULT_MQTT_HOST)
        port = int(os.environ.get("EACLD_MQTT_PORT", str(DEFAULT_MQTT_PORT)))
        client.connect_async(host, port, keepalive=30)
        client.loop_forever(retry_first_connection=True)
    finally:
        health_server.shutdown()
        health_server.server_close()


if __name__ == "__main__":
    main()
