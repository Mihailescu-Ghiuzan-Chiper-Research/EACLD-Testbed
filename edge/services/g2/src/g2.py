"""EACLD G2: MQTT forwarding with explicitly staged, one-shot interventions.

Normal forwarding preserves every byte. A future edge-control agent may stage
header mutation or replay; G2 does not start experiments autonomously and
never has HMAC keys or re-signs frames.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import threading
import socketserver

from eacld_contracts.topics import G1_TRANSIT_SUBSCRIPTION, accepted_topic

LOGGER = logging.getLogger(__name__)
SERVICE_VERSION = "0.1.0"
HEADER_SIZE = 62
FRAME_SIZE = 94
HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 8300
CONTROL_SOCKET = "/run/eacld/g2/control.sock"


@dataclass(frozen=True)
class Mutation:
    offset: int
    xor_mask: int


@dataclass(frozen=True)
class Replay:
    payload: bytes
    require_target_telemetry: bool = False


def route_transit_topic(topic: str) -> tuple[int, str]:
    """Accept only canonical transit/g1/<uint16> topics."""
    if not isinstance(topic, str):
        raise ValueError("MQTT topic must be text")
    parts = topic.split("/")
    if len(parts) != 3 or parts[:2] != ["transit", "g1"]:
        raise ValueError("only transit/g1/<node_id> is accepted")
    segment = parts[2]
    if not segment or not segment.isascii() or not segment.isdecimal():
        raise ValueError("node_id must use ASCII decimal digits")
    node_id = int(segment)
    if node_id > 65535 or str(node_id) != segment:
        raise ValueError("node_id must be canonical unsigned 16-bit decimal")
    return node_id, accepted_topic(node_id)


def _check_node_id(node_id: int) -> None:
    if type(node_id) is not int or not 0 <= node_id <= 65535:
        raise ValueError("node_id must be an unsigned 16-bit integer")


class Gateway2:
    """The future control agent can stage one action per node, once per frame.

    Interventions are deliberately not activated via environment variables,
    unauthenticated HTTP endpoints, or MQTT payloads. A separate controlled
    integration with eacld-edge-control remains to be implemented.
    """

    def __init__(self) -> None:
        self.ready = threading.Event()
        self._subscription_mid: int | None = None
        self._lock = threading.Lock()
        self._pending: dict[int, Mutation | Replay] = {}
        self._last_forwarded: dict[int, bytes] = {}
        self._recent_forwarded: dict[int, deque[bytes]] = {}
        self._mutation_events: dict[int, dict] = {}
        self._mutation_counter = 0
        self._replay_events: dict[int, dict] = {}
        self._replay_counter = 0

    def stage_mutation(self, node_id: int, *, offset: int, xor_mask: int) -> None:
        """XOR one header byte of the next 94-byte frame; keep MAC unchanged."""
        _check_node_id(node_id)
        if type(offset) is not int or not 0 <= offset < HEADER_SIZE:
            raise ValueError("offset must select an authenticated header byte")
        if type(xor_mask) is not int or not 1 <= xor_mask <= 255:
            raise ValueError("xor_mask must be an integer from 1 to 255")
        with self._lock:
            self._pending[node_id] = Mutation(offset, xor_mask)

    def stage_replay(self, node_id: int, *, frame_hex: str | None = None) -> None:
        """Queue a captured, previously forwarded frame for one-time replay.

        When provided, frame_hex must identify an exact frame still present
        in G2's bounded forwarding history. This prevents a race between
        selecting an authenticated VM-CORE baseline and staging the replay.
        """
        _check_node_id(node_id)
        chosen: bytes | None = None
        if frame_hex is not None:
            if (not isinstance(frame_hex, str) or len(frame_hex) != FRAME_SIZE * 2
                    or any(ch not in "0123456789abcdef" for ch in frame_hex)):
                raise ValueError("frame_hex must be canonical 94-byte lowercase hex")
            chosen = bytes.fromhex(frame_hex)
        with self._lock:
            if node_id in self._pending:
                raise ValueError("another G2 intervention is already pending")
            previous = self._last_forwarded.get(node_id) if chosen is None else chosen
            if previous is None or previous not in self._recent_forwarded.get(node_id, ()):
                raise ValueError("selected frame is absent from recent G2 forwarding history")
            if (chosen is not None and (
                len(previous) != FRAME_SIZE or previous[1] != 1
                or int.from_bytes(previous[2:4], "big") != node_id
            )):
                raise ValueError("selected frame is not target-node telemetry")
            self._pending[node_id] = Replay(
                previous, require_target_telemetry=chosen is not None
            )

    def clear_intervention(self, node_id: int) -> None:
        _check_node_id(node_id)
        with self._lock:
            self._pending.pop(node_id, None)

    def intervention_status(self, node_id: int) -> dict:
        """Expose one-shot action status and H3/H4 G2 wire evidence.

        The latest mutation and replay events survive clear until G2 restarts.
        Neither status nor frames include a research cause label or node key.
        """
        _check_node_id(node_id)
        with self._lock:
            pending = self._pending.get(node_id)
            return {
                "status": "ok", "node_id": node_id,
                "pending_action": (
                    "mutate" if isinstance(pending, Mutation)
                    else "replay" if isinstance(pending, Replay) else None
                ),
                "last_mutation": (
                    dict(self._mutation_events[node_id])
                    if node_id in self._mutation_events else None
                ),
                "last_replay": (
                    dict(self._replay_events[node_id])
                    if node_id in self._replay_events else None
                ),
            }

    def on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        self.ready.clear()
        self._subscription_mid = None
        if reason_code != 0:
            LOGGER.error("G2 MQTT connection rejected: %s", reason_code)
            return
        result, mid = client.subscribe(G1_TRANSIT_SUBSCRIPTION, qos=1)
        if result != 0:
            LOGGER.error("G2 MQTT subscription request failed: %s", result)
            return
        self._subscription_mid = mid

    def on_subscribe(self, _client, _userdata, mid, reason_codes, _properties) -> None:
        if mid != self._subscription_mid:
            return
        if not reason_codes or any(code.is_failure for code in reason_codes):
            self.ready.clear()
            LOGGER.error("G2 MQTT subscription was rejected")
            return
        self.ready.set()

    def on_disconnect(self, _client, _userdata, _flags, _reason_code, _properties) -> None:
        self.ready.clear()
        self._subscription_mid = None

    def on_message(self, client, _userdata, message) -> None:
        try:
            node_id, target = route_transit_topic(message.topic)
        except ValueError:
            LOGGER.warning("G2 ignored message on an invalid transit topic")
            return
        payload = message.payload
        if not isinstance(payload, bytes):
            LOGGER.warning("G2 ignored an MQTT payload that was not bytes")
            return

        with self._lock:
            action = self._pending.get(node_id)
            outgoing = payload
            if isinstance(action, Mutation) and len(payload) == FRAME_SIZE:
                changed = bytearray(payload)
                changed[action.offset] ^= action.xor_mask
                outgoing = bytes(changed)
            elif isinstance(action, Mutation):
                LOGGER.warning("G2 deferred mutation: frame is not 94 bytes")

            result = client.publish(target, outgoing, qos=1, retain=False)
            if result.rc != 0:
                LOGGER.error("G2 MQTT forwarding failed with code %s", result.rc)
                return
            if len(outgoing) == FRAME_SIZE:
                self._last_forwarded[node_id] = outgoing
                self._recent_forwarded.setdefault(
                    node_id, deque(maxlen=64)
                ).append(outgoing)
            if isinstance(action, Mutation) and len(payload) == FRAME_SIZE:
                self._mutation_counter += 1
                self._mutation_events[node_id] = {
                    "event_id": self._mutation_counter,
                    "offset": action.offset,
                    "xor_mask": action.xor_mask,
                    "original_frame_hex": payload.hex(),
                    "mutated_frame_hex": outgoing.hex(),
                }
                self._pending.pop(node_id, None)
            elif isinstance(action, Replay):
                if (action.require_target_telemetry and (
                    len(payload) != FRAME_SIZE or payload[1] != 1
                    or int.from_bytes(payload[2:4], "big") != node_id
                    or payload[6:22] != action.payload[6:22]
                    or int.from_bytes(payload[22:30], "big")
                    <= int.from_bytes(action.payload[22:30], "big")
                )):
                    LOGGER.warning("G2 deferred replay: no newer target-node telemetry")
                    return
                result = client.publish(target, action.payload, qos=1, retain=False)
                if result.rc == 0:
                    self._replay_counter += 1
                    self._replay_events[node_id] = {
                        "event_id": self._replay_counter,
                        "reference_frame_hex": action.payload.hex(),
                        "trigger_frame_hex": payload.hex(),
                        "replayed_frame_hex": action.payload.hex(),
                    }
                    self._pending.pop(node_id, None)
                else:
                    LOGGER.error("G2 MQTT replay failed with code %s", result.rc)


def create_health_server(gateway: Gateway2, *, host: str = HEALTH_HOST,
                         port: int = HEALTH_PORT) -> ThreadingHTTPServer:
    """Container-local readiness; no externally published G2 control port."""
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



def create_command_server(gateway: Gateway2, path: str = CONTROL_SOCKET):
    """Private Unix-socket control channel, shared only with edge-control."""
    socket_path = Path(path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        if not socket_path.is_socket():
            raise RuntimeError("G2 socket path exists but is not a socket")
        socket_path.unlink()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            try:
                raw = self.rfile.readline(2049)
                if not raw or len(raw) > 2048 or not raw.endswith(b"\n"):
                    raise ValueError("invalid command length")
                request = json.loads(raw)
                if not isinstance(request, dict):
                    raise ValueError("command must be a JSON object")
                action = request.get("action")
                if action == "health" and set(request) == {"action"}:
                    reply = {"status": "ok" if gateway.ready.is_set() else "unavailable"}
                elif action == "mutate" and set(request) == {
                    "action", "node_id", "offset", "xor_mask"
                }:
                    gateway.stage_mutation(request["node_id"],
                                           offset=request["offset"],
                                           xor_mask=request["xor_mask"])
                    reply = {"status": "ok"}
                elif action == "replay" and set(request) in (
                    {"action", "node_id"},
                    {"action", "node_id", "frame_hex"},
                ):
                    gateway.stage_replay(request["node_id"],
                                         frame_hex=request.get("frame_hex"))
                    reply = {"status": "ok"}
                elif action == "clear" and set(request) == {"action", "node_id"}:
                    gateway.clear_intervention(request["node_id"])
                    reply = {"status": "ok"}
                elif action == "status" and set(request) == {"action", "node_id"}:
                    reply = gateway.intervention_status(request["node_id"])
                else:
                    raise ValueError("unsupported command or fields")
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
                reply = {"status": "error", "reason": str(error)}
            self.wfile.write((json.dumps(reply, separators=(",", ":")) + "\n").encode())

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    server = Server(str(socket_path), Handler)
    socket_path.chmod(0o660)
    return server


def main() -> None:
    import paho.mqtt.client as mqtt

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    password_file = Path(os.environ.get("EACLD_G2_MQTT_PASSWORD_FILE", "/run/eacld/mqtt_password"))
    password = password_file.read_text(encoding="utf-8").rstrip("\r\n")
    if not password:
        raise ValueError("G2 MQTT password file is empty")

    gateway = Gateway2()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="eacld-g2",
                         protocol=mqtt.MQTTv311)
    client.username_pw_set("g2", password)
    client.on_connect = gateway.on_connect
    client.on_subscribe = gateway.on_subscribe
    client.on_disconnect = gateway.on_disconnect
    client.on_message = gateway.on_message
    client.reconnect_delay_set(min_delay=1, max_delay=20)

    health_server = create_health_server(gateway)
    command_server = create_command_server(gateway, os.environ.get("EACLD_G2_CONTROL_SOCKET", CONTROL_SOCKET))
    command_thread = threading.Thread(target=command_server.serve_forever,
                                      name="g2-control", daemon=True)
    command_thread.start()
    thread = threading.Thread(target=health_server.serve_forever,
                              name="g2-health", daemon=True)
    thread.start()
    try:
        host = os.environ.get("EACLD_MQTT_HOST", "mosquitto")
        port = int(os.environ.get("EACLD_MQTT_PORT", "1883"))
        client.connect_async(host, port, keepalive=30)
        client.loop_forever(retry_first_connection=True)
    finally:
        command_server.shutdown()
        command_server.server_close()
        Path(command_server.server_address).unlink(missing_ok=True)
        health_server.shutdown()
        health_server.server_close()


if __name__ == "__main__":
    main()
