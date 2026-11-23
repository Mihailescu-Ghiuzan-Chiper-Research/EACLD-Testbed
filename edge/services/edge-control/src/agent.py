"""VM-EDGE control agent for explicit G2 experimental interventions.

Binds only NET-CONTROL; the VM-CONTROL address is the only authorized
remote caller. A private Unix socket carries commands to the G2 process.
No MQTT credentials, node keys, or ground truth enter this service.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import socket
from urllib.parse import parse_qs, urlsplit

LOGGER = logging.getLogger(__name__)
VERSION = "0.1.0"
CONTROL_HOST = "10.60.30.14"
CONTROL_PORT = 8300
CONTROL_SOURCE = "10.60.30.10"
G2_SOCKET = "/run/eacld/g2/control.sock"
MAX_BODY = 1024


def request_g2(command: dict, *, socket_path: str = G2_SOCKET) -> dict:
    data = json.dumps(command, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(data) > MAX_BODY:
        raise ValueError("command is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2.0)
        client.connect(socket_path)
        client.sendall(data)
        chunks = bytearray()
        while len(chunks) <= MAX_BODY:
            part = client.recv(1024)
            if not part:
                break
            chunks.extend(part)
            if b"\n" in part:
                break
    if len(chunks) > MAX_BODY or b"\n" not in chunks:
        raise OSError("invalid G2 control response")
    response = json.loads(bytes(chunks).split(b"\n", 1)[0])
    if not isinstance(response, dict) or response.get("status") not in ("ok", "error", "unavailable"):
        raise OSError("invalid G2 control response")
    return response


def _validate_command(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    action = value.get("action")
    node_id = value.get("node_id")
    if type(node_id) is not int or not 0 <= node_id <= 65535:
        raise ValueError("node_id must be an unsigned 16-bit integer")
    if action == "mutate":
        if set(value) != {"action", "node_id", "offset", "xor_mask"}:
            raise ValueError("invalid mutation fields")
        if type(value["offset"]) is not int or not 0 <= value["offset"] < 62:
            raise ValueError("offset must be a header byte index")
        if type(value["xor_mask"]) is not int or not 1 <= value["xor_mask"] <= 255:
            raise ValueError("xor_mask must be in [1, 255]")
    elif action in ("replay", "clear"):
        if set(value) != {"action", "node_id"}:
            raise ValueError("unexpected command fields")
    else:
        raise ValueError("unsupported intervention action")
    return value


def _status_node(path: str) -> int:
    url = urlsplit(path)
    if url.path != "/v1/g2/intervention/status":
        raise ValueError("unexpected status endpoint")
    query = parse_qs(url.query, keep_blank_values=True)
    if set(query) != {"node_id"} or len(query["node_id"]) != 1:
        raise ValueError("invalid status query")
    value = query["node_id"][0]
    if not value.isascii() or not value.isdecimal():
        raise ValueError("invalid node_id")
    node = int(value)
    if not 0 <= node <= 65535 or str(node) != value:
        raise ValueError("node_id is not canonical uint16")
    return node


def create_server(*, host: str = CONTROL_HOST, port: int = CONTROL_PORT,
                  allowed_source: str = CONTROL_SOURCE,
                  socket_path: str = G2_SOCKET) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def is_authorized(self, *, health: bool) -> bool:
            source = self.client_address[0]
            return source == allowed_source or (health and source in ("127.0.0.1", CONTROL_HOST))

        def do_GET(self) -> None:
            if self.path == "/healthz":
                if not self.is_authorized(health=True):
                    self.send_json(403, {"detail": "forbidden"})
                    return
                try:
                    response = request_g2({"action": "health"}, socket_path=socket_path)
                    healthy = response["status"] == "ok"
                except (OSError, ValueError, json.JSONDecodeError):
                    healthy = False
                self.send_json(200 if healthy else 503, {
                    "status": "ok" if healthy else "unavailable",
                    "service_version": VERSION,
                })
                return
            if not self.is_authorized(health=False):
                self.send_json(403, {"detail": "forbidden"})
                return
            if not self.path.startswith("/v1/g2/intervention/status"):
                self.send_json(404, {"detail": "not found"})
                return
            try:
                node = _status_node(self.path)
            except ValueError:
                self.send_json(400, {"detail": "invalid intervention status query"})
                return
            try:
                reply = request_g2({"action": "status", "node_id": node},
                                   socket_path=socket_path)
            except (OSError, ValueError, json.JSONDecodeError):
                self.send_json(503, {"detail": "G2 control unavailable"})
                return
            if reply.get("status") != "ok":
                self.send_json(503, {"detail": "G2 status unavailable"})
                return
            self.send_json(200, reply)

        def do_POST(self) -> None:
            if not self.is_authorized(health=False):
                self.send_json(403, {"detail": "forbidden"})
                return
            if self.path != "/v1/g2/intervention":
                self.send_json(404, {"detail": "not found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "-1"))
                if size < 0 or size > MAX_BODY:
                    raise ValueError("invalid request size")
                command = _validate_command(json.loads(self.rfile.read(size)))
            except (ValueError, TypeError, json.JSONDecodeError):
                self.send_json(400, {"detail": "invalid intervention"})
                return
            try:
                reply = request_g2(command, socket_path=socket_path)
            except (OSError, ValueError, json.JSONDecodeError):
                self.send_json(503, {"detail": "G2 control unavailable"})
                return
            if reply.get("status") != "ok":
                self.send_json(400, {"detail": reply.get("reason", "G2 rejected command")})
                return
            self.send_json(200, {"status": "ok"})

        def log_message(self, fmt: str, *args) -> None:
            LOGGER.info("%s %s", self.client_address[0], fmt % args)

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = create_server()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
