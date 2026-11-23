"""H3 G2 evidence: original frame, modified frame, and private status route."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from src.g2 import Gateway2, create_command_server

EDGE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "h3_edge_agent", EDGE / "services/edge-control/src/agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload))
        return SimpleNamespace(rc=0)


class H3GatewayTests(unittest.TestCase):
    def test_one_shot_mutation_records_exact_wire_bytes(self):
        g2 = Gateway2()
        client = FakeClient()
        header = bytearray(62)
        header[1] = 1
        header[2:4] = (1).to_bytes(2, "big")
        header[38:42] = (22000).to_bytes(4, "big", signed=True)
        header[42:46] = (50000).to_bytes(4, "big")
        original = bytes(header) + b"m" * 32
        self.assertIsNone(g2.intervention_status(1)["last_mutation"])

        g2.stage_mutation(1, offset=40, xor_mask=37)
        self.assertEqual(g2.intervention_status(1)["pending_action"], "mutate")
        g2.on_message(client, None, SimpleNamespace(
            topic="transit/g1/1", payload=original))
        altered = client.published[0][1]
        status = g2.intervention_status(1)
        event = status["last_mutation"]
        self.assertIsNone(status["pending_action"])
        self.assertEqual(event["offset"], 40)
        self.assertEqual(event["xor_mask"], 37)
        self.assertEqual(event["event_id"], 1)
        self.assertEqual(bytes.fromhex(event["original_frame_hex"]), original)
        self.assertEqual(bytes.fromhex(event["mutated_frame_hex"]), altered)
        self.assertEqual(altered[-32:], original[-32:])
        self.assertEqual(int.from_bytes(altered[38:42], "big", signed=True), 28912)

        g2.on_message(client, None, SimpleNamespace(
            topic="transit/g1/1", payload=original))
        self.assertEqual(client.published[1][1], original)
        self.assertEqual(g2.intervention_status(1)["last_mutation"], event)
        g2.clear_intervention(1)
        self.assertEqual(g2.intervention_status(1)["last_mutation"], event)

    def test_unsuccessful_mqtt_publish_does_not_record_mutation(self):
        g2 = Gateway2()
        client = FakeClient()
        client.publish = lambda *a, **kw: SimpleNamespace(rc=5)
        g2.stage_mutation(1, offset=40, xor_mask=37)
        g2.on_message(client, None, SimpleNamespace(
            topic="transit/g1/1", payload=b"0" * 94))
        self.assertIsNone(g2.intervention_status(1)["last_mutation"])
        self.assertEqual(g2.intervention_status(1)["pending_action"], "mutate")

    def test_status_rejects_invalid_node(self):
        with self.assertRaises(ValueError):
            Gateway2().intervention_status(True)


class H3AgentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.sock = str(Path(self.folder.name) / "h3.sock")
        self.g2 = Gateway2()
        self.g2.ready.set()
        self.unix = create_command_server(self.g2, self.sock)
        self.t1 = threading.Thread(target=self.unix.serve_forever, daemon=True)
        self.t1.start()
        self.http = agent.create_server(host="127.0.0.1", port=0,
                                        allowed_source="127.0.0.1",
                                        socket_path=self.sock)
        self.t2 = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.t2.start()
        self.opener = build_opener(ProxyHandler({}))
        self.url = f"http://127.0.0.1:{self.http.server_port}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.t2.join(2)
        self.unix.shutdown()
        self.unix.server_close()
        self.t1.join(2)
        self.folder.cleanup()

    def test_private_status_tracks_exact_mutation_event(self):
        endpoint = self.url + "/v1/g2/intervention/status?node_id=1"
        with self.opener.open(endpoint, timeout=3) as response:
            self.assertIsNone(json.load(response)["last_mutation"])
        self.g2.stage_mutation(1, offset=40, xor_mask=37)
        with self.opener.open(endpoint, timeout=3) as response:
            self.assertEqual(json.load(response)["pending_action"], "mutate")
        self.g2.on_message(FakeClient(), None, SimpleNamespace(
            topic="transit/g1/1", payload=b"x" * 94))
        with self.opener.open(endpoint, timeout=3) as response:
            event = json.load(response)["last_mutation"]
        self.assertEqual(event["event_id"], 1)
        self.assertEqual(len(event["original_frame_hex"]), 188)
        with self.assertRaises(HTTPError) as error:
            self.opener.open(self.url + "/v1/g2/intervention/status?node_id=01",
                             timeout=3)
        self.assertEqual(error.exception.code, 400)

    def test_status_denied_to_non_control_source(self):
        self.http.shutdown()
        self.http.server_close()
        self.t2.join(2)
        self.http = agent.create_server(host="127.0.0.1", port=0,
                                        allowed_source="10.60.30.10",
                                        socket_path=self.sock)
        self.t2 = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.t2.start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        with self.assertRaises(HTTPError) as error:
            self.opener.open(
                self.url + "/v1/g2/intervention/status?node_id=1", timeout=3)
        self.assertEqual(error.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
