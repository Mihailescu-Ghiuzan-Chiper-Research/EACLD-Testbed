"""H4 G2 replay evidence and VM-CONTROL-only operator interfaces."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from eacld_contracts.frame import Frame, FrameHeader, sign_frame, verify_frame
from src.g2 import Gateway2, create_command_server

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "h4_edge_agent", ROOT / "services/edge-control/src/agent.py"
)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def signed(sequence: int, node: int = 1, nonce: bytes = b"n" * 16) -> bytes:
    return sign_frame(FrameHeader(
        version=1, type=1, node_id=node, zone_id=1, key_version=1,
        boot_id=b"b" * 16, sequence_no=sequence, uptime_ms=sequence * 1000,
        temperature_mC=22000, humidity_mPct=50000, nonce=nonce,
    ), b"K" * 32).to_bytes()


class FakeClient:
    def __init__(self, codes=None):
        self.published = []
        self.codes = list(codes or [])

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=self.codes.pop(0) if self.codes else 0)


def emit(gateway: Gateway2, client: FakeClient, frame: bytes, node=1):
    gateway.on_message(client, None, SimpleNamespace(
        topic=f"transit/g1/{node}", payload=frame))


class H4GatewayTests(unittest.TestCase):
    def test_replay_earlier_selected_signed_frame_with_evidence(self):
        gateway = Gateway2()
        client = FakeClient()
        first, second, third = signed(11), signed(12), signed(13)
        emit(gateway, client, first)
        emit(gateway, client, second)
        gateway.stage_replay(1, frame_hex=first.hex())
        self.assertEqual(gateway.intervention_status(1)["pending_action"], "replay")
        emit(gateway, client, third)
        transmitted = [p[1] for p in client.published]
        self.assertEqual(transmitted, [first, second, third, first])
        event = gateway.intervention_status(1)["last_replay"]
        self.assertEqual(event["event_id"], 1)
        self.assertEqual(event["reference_frame_hex"], first.hex())
        self.assertEqual(event["trigger_frame_hex"], third.hex())
        self.assertEqual(event["replayed_frame_hex"], first.hex())
        self.assertIsNone(gateway.intervention_status(1)["pending_action"])
        self.assertTrue(verify_frame(Frame.from_bytes(first), b"K" * 32))
        self.assertEqual(Frame.from_bytes(first).header.sequence_no, 11)
        self.assertEqual(Frame.from_bytes(third).header.sequence_no, 13)
        emit(gateway, client, signed(14))
        self.assertEqual([p[1] for p in client.published][-1], signed(14))
        self.assertEqual(gateway.intervention_status(1)["last_replay"], event)

    def test_strict_selection_requires_real_prior_94_byte_frame(self):
        gateway = Gateway2()
        client = FakeClient()
        first = signed(5)
        emit(gateway, client, first)
        with self.assertRaises(ValueError):
            gateway.stage_replay(1, frame_hex=signed(6).hex())
        with self.assertRaises(ValueError):
            gateway.stage_replay(1, frame_hex=first.hex().upper())
        gateway.stage_replay(1, frame_hex=first.hex())
        with self.assertRaises(ValueError):
            gateway.stage_replay(1, frame_hex=first.hex())
        gateway.clear_intervention(1)
        self.assertIsNone(gateway.intervention_status(1)["pending_action"])
        with self.assertRaises(ValueError):
            gateway.stage_replay(2, frame_hex=first.hex())

    def test_failed_second_publication_keeps_replay_armed(self):
        gateway = Gateway2()
        first, second, third = signed(10), signed(11), signed(12)
        emit(gateway, FakeClient(), first)
        gateway.stage_replay(1, frame_hex=first.hex())
        failed = FakeClient(codes=[0, 5])
        with self.assertLogs("src.g2", level="ERROR"):
            emit(gateway, failed, second)
        self.assertEqual(gateway.intervention_status(1)["pending_action"], "replay")
        self.assertIsNone(gateway.intervention_status(1)["last_replay"])
        good = FakeClient()
        emit(gateway, good, third)
        self.assertEqual([p[1] for p in good.published], [third, first])
        self.assertEqual(gateway.intervention_status(1)["last_replay"]["event_id"], 1)

    def test_does_not_replay_on_invalid_trigger_under_strict_mode(self):
        gateway = Gateway2()
        first = signed(10)
        emit(gateway, FakeClient(), first)
        gateway.stage_replay(1, frame_hex=first.hex())
        output = FakeClient()
        emit(gateway, output, b"invalid")
        emit(gateway, output, signed(10))
        self.assertEqual([p[1] for p in output.published], [b"invalid", signed(10)])
        self.assertEqual(gateway.intervention_status(1)["pending_action"], "replay")
        emit(gateway, output, signed(11))
        self.assertEqual([p[1] for p in output.published][-2:], [signed(11), first])

    def test_bounded_history_rejects_expired_capture(self):
        gateway = Gateway2()
        initial = signed(1)
        client = FakeClient()
        emit(gateway, client, initial)
        for i in range(2, 68):
            emit(gateway, client, signed(i))
        with self.assertRaises(ValueError):
            gateway.stage_replay(1, frame_hex=initial.hex())


class H4ControlAgentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.sock = str(Path(self.directory.name) / "g2.sock")
        self.gateway = Gateway2()
        self.gateway.ready.set()
        self.unix = create_command_server(self.gateway, self.sock)
        self.unix_thread = threading.Thread(target=self.unix.serve_forever, daemon=True)
        self.unix_thread.start()
        self.http = agent.create_server(
            host="127.0.0.1", port=0, allowed_source="127.0.0.1",
            socket_path=self.sock)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(2)
        self.unix.shutdown()
        self.unix.server_close()
        self.unix_thread.join(2)
        self.directory.cleanup()

    def post(self, body):
        return self.opener.open(Request(
            self.base + "/v1/g2/intervention",
            data=json.dumps(body).encode(), method="POST",
        ), timeout=3)

    def status(self):
        with self.opener.open(
            self.base + "/v1/g2/intervention/status?node_id=1", timeout=3
        ) as response:
            return json.load(response)

    def test_post_exact_replay_and_fetch_large_evidence(self):
        first, trigger = signed(1), signed(3)
        emit(self.gateway, FakeClient(), first)
        self.gateway.stage_mutation(1, offset=40, xor_mask=37)
        emit(self.gateway, FakeClient(), signed(2))
        with self.post({
            "action": "replay", "node_id": 1, "frame_hex": first.hex()
        }) as response:
            self.assertEqual(json.load(response)["status"], "ok")
        self.assertEqual(self.status()["pending_action"], "replay")
        emit(self.gateway, FakeClient(), trigger)
        evidence = self.status()
        self.assertEqual(evidence["last_replay"]["replayed_frame_hex"], first.hex())
        self.assertIsNone(evidence["pending_action"])
        self.assertIsNotNone(evidence["last_mutation"])
        self.assertGreater(len(json.dumps(evidence)), 1024)
        with self.post({"action": "clear", "node_id": 1}) as response:
            self.assertEqual(json.load(response)["status"], "ok")

    def test_unauthorized_status_and_replay_request_rejected(self):
        with self.assertRaises(HTTPError) as cm:
            self.post({"action": "replay", "node_id": 1, "frame_hex": "x"})
        self.assertEqual(cm.exception.code, 400)
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(2)
        self.http = agent.create_server(
            host="127.0.0.1", port=0, allowed_source="10.60.30.10",
            socket_path=self.sock)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"
        with self.assertRaises(HTTPError) as cm:
            self.opener.open(
                self.base + "/v1/g2/intervention/status?node_id=1", timeout=3)
        self.assertEqual(cm.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
