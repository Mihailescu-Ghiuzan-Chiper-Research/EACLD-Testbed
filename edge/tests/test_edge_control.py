"""Unit and loopback tests for private VM-EDGE experimental control."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import importlib.util
import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from src.g2 import Gateway2, create_command_server

ROOT = Path(__file__).resolve().parents[1]
AGENT_FILE = ROOT / "services/edge-control/src/agent.py"
spec = importlib.util.spec_from_file_location("eacld_edge_agent", AGENT_FILE)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=0)


def payload():
    return bytes(range(94))


def send_socket(path, command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(2)
        conn.connect(path)
        conn.sendall(json.dumps(command).encode() + b"\n")
        return json.loads(conn.recv(4096))


class SocketControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "control.sock")
        self.gateway = Gateway2()
        self.server = create_command_server(self.gateway, self.path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def test_ready_health_changes(self):
        self.assertEqual(send_socket(self.path, {"action": "health"}), {"status": "unavailable"})
        self.gateway.ready.set()
        self.assertEqual(send_socket(self.path, {"action": "health"}), {"status": "ok"})

    def test_mutation_only_on_matching_node_and_header(self):
        client = FakeClient()
        self.assertEqual(send_socket(self.path, {"action": "mutate", "node_id": 12,
                                                  "offset": 10, "xor_mask": 7})["status"], "ok")
        for node in (11, 12, 12):
            self.gateway.on_message(client, None,
                                    SimpleNamespace(topic=f"transit/g1/{node}", payload=payload()))
        self.assertEqual(client.published[0][1], payload())
        self.assertEqual(client.published[1][1][10], 10 ^ 7)
        self.assertEqual(client.published[1][1][-32:], payload()[-32:])
        self.assertEqual(client.published[2][1], payload())

    def test_replay_and_clear(self):
        client = FakeClient()
        self.gateway.on_message(client, None,
                                SimpleNamespace(topic="transit/g1/12", payload=payload()))
        self.assertEqual(send_socket(self.path, {"action": "replay", "node_id": 12})["status"], "ok")
        fresh = bytes(reversed(payload()))
        self.gateway.on_message(client, None,
                                SimpleNamespace(topic="transit/g1/12", payload=fresh))
        self.assertEqual([p[1] for p in client.published], [payload(), fresh, payload()])
        self.assertEqual(send_socket(self.path, {"action": "clear", "node_id": 12})["status"], "ok")

    def test_invalid_command_rejected(self):
        for cmd in ({"action": "replay", "node_id": 99},
                    {"action": "mutate", "node_id": True, "offset": 0, "xor_mask": 1},
                    {"action": "mutate", "node_id": 1, "offset": 62, "xor_mask": 1},
                    {"action": "health", "extra": 1}):
            self.assertEqual(send_socket(self.path, cmd)["status"], "error")


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "control.sock")
        self.gateway = Gateway2()
        self.socket_server = create_command_server(self.gateway, self.path)
        self.socket_thread = threading.Thread(target=self.socket_server.serve_forever, daemon=True)
        self.socket_thread.start()
        self.http = agent.create_server(host="127.0.0.1", port=0,
                                        allowed_source="127.0.0.1", socket_path=self.path)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(2)
        self.socket_server.shutdown()
        self.socket_server.server_close()
        self.socket_thread.join(2)
        self.temp.cleanup()

    def post(self, body):
        req = urllib.request.Request(self.url + "/v1/g2/intervention",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        return self.opener.open(req, timeout=2)

    def test_health_and_control_roundtrip(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.opener.open(self.url + "/healthz", timeout=2)
        self.assertEqual(cm.exception.code, 503)
        self.gateway.ready.set()
        with self.opener.open(self.url + "/healthz", timeout=2) as response:
            self.assertEqual(json.load(response)["status"], "ok")
        with self.post({"action": "mutate", "node_id": 3, "offset": 2, "xor_mask": 1}) as response:
            self.assertEqual(json.load(response)["status"], "ok")
        client = FakeClient()
        self.gateway.on_message(client, None,
                                SimpleNamespace(topic="transit/g1/3", payload=payload()))
        self.assertEqual(client.published[0][1][2], 2 ^ 1)

    def test_wrong_source_cannot_modify(self):
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(2)
        self.http = agent.create_server(host="127.0.0.1", port=0,
                                        allowed_source="10.60.30.10", socket_path=self.path)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post({"action": "clear", "node_id": 1})
        self.assertEqual(cm.exception.code, 403)

    def test_invalid_fields_rejected_without_state_change(self):
        for body in ({"action": "replay", "node_id": True},
                     {"action": "mutate", "node_id": 1, "offset": 62, "xor_mask": 1},
                     {"action": "unknown", "node_id": 1},
                     {"action": "clear", "node_id": 1, "extra": 2}):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.post(body)
            self.assertEqual(cm.exception.code, 400)

    def test_missing_g2_socket_returns_unavailable(self):
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(2)
        self.http = agent.create_server(host="127.0.0.1", port=0,
                                        allowed_source="127.0.0.1",
                                        socket_path=self.path + ".missing")
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post({"action": "clear", "node_id": 1})
        self.assertEqual(cm.exception.code, 503)


if __name__ == "__main__":
    unittest.main()
