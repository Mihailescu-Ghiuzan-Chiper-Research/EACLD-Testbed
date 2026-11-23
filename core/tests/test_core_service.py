"""VM-CORE contract, preserved E0 model, probe adapter and control tests."""
from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace

import numpy as np

from eacld_contracts.frame import FrameHeader, sign_frame
from src.core import CoreMqtt, _valid_request, create_control_server
from src.ingest import Reading, accepted_node
from src.live_env import LiveEnvironment
from src.runner import load_engine
from src.verifier import FrameVerifier, load_keyring

E0 = Path(__file__).resolve().parents[1] / "vendor/e0/EACLD_E0_Experimental_Package"
KEY = b"T" * 32


def signed(node=12, seq=4, kind=1, nonce=b"0" * 16):
    return sign_frame(FrameHeader(
        version=1, type=kind, node_id=node, zone_id=1, key_version=1,
        boot_id=b"b" * 16, sequence_no=seq, uptime_ms=100,
        temperature_mC=24000, humidity_mPct=50000,
        nonce=nonce,
    ), KEY).to_bytes()


class VerifierTests(unittest.TestCase):
    def test_valid_and_modified_frames(self):
        v = FrameVerifier({12: {1: KEY}})
        original = signed()
        self.assertEqual(v.verify(12, original), "valid")
        invalid = bytearray(original)
        invalid[34] ^= 1
        self.assertEqual(v.verify(12, bytes(invalid)), "invalid")
        self.assertEqual(v.verify(13, original), "invalid")
        self.assertEqual(v.verify(12, original[:-1]), "invalid")
        self.assertEqual(FrameVerifier({}).verify(12, original), "unavailable")

    def test_node_compatible_keyring_and_no_short_keys(self):
        with tempfile.TemporaryDirectory() as d:
            filename = Path(d) / "node_keys.json"
            filename.write_text(json.dumps({"12": {"1": KEY.hex()}}))
            self.assertEqual(load_keyring(filename)[12][1], KEY)
            filename.write_text(json.dumps({"12": {"1": "ab"}}))
            with self.assertRaises(ValueError):
                load_keyring(filename)

    def test_accepted_topic_validation(self):
        self.assertEqual(accepted_node("accepted/12"), 12)
        for topic in ("raw/12", "accepted/012", "accepted/65536", "accepted/-1", "accepted/1/a"):
            with self.assertRaises(ValueError):
                accepted_node(topic)


class FakeResponse:
    status = 200

    def __init__(self, body):
        self.body = BytesIO(body)

    def read(self, n):
        return self.body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeOpener:
    def __init__(self):
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.full_url, request.data))
        if request.full_url.endswith("/v1/reference/read"):
            return FakeResponse(json.dumps({"temperature_mC": 27000,
                                            "status": "ok", "timestamp": "2026-11-10T00:00:00Z"}).encode())
        if request.full_url.endswith("/v1/challenge"):
            body = json.loads(request.data)
            packet = signed(node=body["node_id"], kind=2, nonce=bytes.fromhex(body["nonce_hex"]))
            return FakeResponse(json.dumps({"frame_hex": packet.hex()}).encode())
        raise AssertionError("unexpected request")


class LiveAdapterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((E0 / "config/e0_config.json").read_text())
        self.reading = Reading(1, 12, signed(), "valid", "fresh")
        self.opener = FakeOpener()
        self.env = LiveEnvironment(
            reading=self.reading, verifier=FrameVerifier({12: {1: KEY}}),
            cfg=self.cfg, episode_id="test-1", block_id="block-1",
            mode="nominal", baseline_temperature_mC=22000,
            high_delta_mC=3000, opener=self.opener,
        )

    def test_all_four_real_probes(self):
        self.assertEqual(self.env.execute("message_verify")[0], "valid")
        self.assertEqual(self.env.execute("sequence_check")[0], "fresh")
        self.assertEqual(self.env.execute("reference_read")[0], "high")
        self.assertEqual(self.env.execute("node_challenge")[0], "normal")
        self.assertEqual(len(self.opener.calls), 2)
        with self.assertRaises(ValueError):
            self.env.execute("node_challenge")

    def test_invalid_or_unavailable_is_not_assumed_normal(self):
        no_keys = LiveEnvironment(
            reading=self.reading, verifier=FrameVerifier({}),
            cfg=self.cfg, episode_id="test-2", block_id="block-1",
            mode="nominal", baseline_temperature_mC=22000,
            high_delta_mC=3000, opener=self.opener,
        )
        self.assertEqual(no_keys.execute("node_challenge")[0], "unavailable")

    def test_preserved_e0_engine_and_calibration(self):
        engine, cfg = load_engine(config=E0 / "config/e0_config.json",
                                  fitted=E0 / "results/fitted_model.json")
        env = LiveEnvironment(
            reading=self.reading, verifier=FrameVerifier({12: {1: KEY}}),
            cfg=cfg, episode_id="test-3", block_id="block-1",
            mode="nominal", baseline_temperature_mC=22000,
            high_delta_mC=3000, opener=FakeOpener())
        result = engine.run("B6_EACLD", env, np.random.default_rng(0))
        self.assertEqual(result["policy"], "B6_EACLD")
        self.assertEqual(len(result["probabilities_h"]), 4)
        self.assertLessEqual(result["probe_count"], 4)
        self.assertNotIn("cause", result)
        self.assertNotIn("ground_truth", result)


class FakeStore:
    def __init__(self):
        self.saved = []

    def append(self, topic, payload, verifier):
        self.saved.append((topic, payload))


class MqttTests(unittest.TestCase):
    def test_only_accepted_topics_and_ready_subscriptions(self):
        store = FakeStore()
        service = CoreMqtt(store, FrameVerifier({12: {1: KEY}}))
        class FakeClient:
            def subscribe(self, topic, qos):
                self.topic = topic
                self.qos = qos
                return (0, 21)
        client = FakeClient()
        service.on_connect(client, None, None, 0, None)
        self.assertEqual((client.topic, client.qos), ("accepted/+", 1))
        service.on_subscribe(client, None, 21, [SimpleNamespace(is_failure=False)], None)
        self.assertTrue(service.ready.is_set())
        service.on_message(client, None, SimpleNamespace(topic="accepted/12", payload=signed()))
        self.assertEqual(len(store.saved), 1)
        service.on_disconnect(client, None, None, 0, None)
        self.assertFalse(service.ready.is_set())


class ControlApiTests(unittest.TestCase):
    def test_input_requires_explicit_research_parameters(self):
        valid = {
            "node_id": 12, "mode": "nominal", "episode_id": "E1",
            "block_id": "B1", "baseline_temperature_mC": 22000,
            "high_delta_mC": 2000
        }
        self.assertEqual(_valid_request(valid, ["nominal", "degraded"]), valid)
        for change in ({"high_delta_mC": 0}, {"node_id": True}, {"mode": "other"}):
            with self.assertRaises(ValueError):
                _valid_request({**valid, **change}, ["nominal", "degraded"])

    def test_control_endpoint_and_source_restriction(self):
        class Runner:
            config = {"modes": ["nominal", "degraded"]}
            def diagnose(self, **kwargs):
                return {"episode_id": kwargs["episode_id"], "decision": "unresolved"}
        gateway = SimpleNamespace(ready=threading.Event())
        gateway.ready.set()
        httpd = create_control_server(Runner(), gateway, host="127.0.0.1",
                                      port=0, allowed_source="127.0.0.1")
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(base + "/healthz", timeout=2) as response:
                self.assertEqual(json.load(response)["status"], "ok")
            body = {
                "node_id": 12, "mode": "nominal", "episode_id": "T-1",
                "block_id": "B", "baseline_temperature_mC": 22000,
                "high_delta_mC": 2000
            }
            req = urllib.request.Request(base + "/v1/diagnose",
                                         data=json.dumps(body).encode(),
                                         method="POST")
            with opener.open(req, timeout=2) as response:
                self.assertEqual(json.load(response)["decision"], "unresolved")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
