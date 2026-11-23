"""VM-NODES local tests: public observations, frame, APIs and MQTT behavior."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from eacld_contracts.frame import Frame, TELEMETRY_TYPE, CHALLENGE_RESPONSE_TYPE, verify_frame
from src.challenge import create_probe_app
from src.frame import NodeFrameProducer
from src.nodes import (
    MqttPublisher, NodeDefinition, NodesConfiguration, NodesService,
    create_control_app, load_config, publish_loop,
)
from src.sensor import PlantClient, PlantObservation, SensorBias, SensorMeasurement, VirtualSensor


class FixedPlant:
    def __init__(self, state=None):
        self.state = state or PlantObservation(21345, 55789, 4)
        self.calls = 0

    def read(self):
        self.calls += 1
        return self.state


def service_two_nodes():
    config = NodesConfiguration(
        1, 1000,
        (NodeDefinition(1, 7, 2, b"A" * 32, SensorBias(500, -100)),
         NodeDefinition(2, 7, 2, b"B" * 32, SensorBias())),
    )
    plant = FixedPlant()
    return NodesService(config, plant), plant


class SensorTests(unittest.TestCase):
    def test_public_observation_drops_unexpected_private_fields(self):
        obs = PlantObservation.parse({"temperature_mC": 1, "humidity_mPct": 2,
                                      "step_id": 3, "cause_label": "H1", "secret": "x"})
        self.assertEqual(obs, PlantObservation(1, 2, 3))
        self.assertNotIn("cause", repr(obs))

    def test_public_observation_validation(self):
        for payload in ({"temperature_mC": True, "humidity_mPct": 1, "step_id": 1},
                        {"temperature_mC": 1, "humidity_mPct": 100001, "step_id": 1},
                        {"temperature_mC": 1, "humidity_mPct": 1},
                        [1, 2, 3]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                PlantObservation.parse(payload)

    def test_bias_affects_only_local_observation(self):
        upstream = PlantObservation(21_345, 55_789, 4)
        measured = VirtualSensor(SensorBias(500, -100)).measure(upstream)
        self.assertEqual(measured, SensorMeasurement(21_845, 55_689))
        self.assertEqual(upstream.temperature_mC, 21_345)

    def test_sensor_range_violation(self):
        with self.assertRaises(ValueError):
            VirtualSensor(SensorBias(humidity_mPct=100_000)).measure(
                PlantObservation(20_000, 50_000, 4))

    def test_plant_reader_requests_only_public_route(self):
        client = PlantClient()
        response = MagicMock()
        response.status = 200
        stream = io.BytesIO(json.dumps({"temperature_mC": 100, "humidity_mPct": 101,
                                        "step_id": 2, "truth": "hidden"}).encode())
        stream.status = 200
        response.__enter__.return_value = stream
        client._opener = MagicMock()
        client._opener.open.return_value = response
        obs = client.read()
        request = client._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://10.60.40.10:8100/v1/state")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(obs, PlantObservation(100, 101, 2))


class FrameTests(unittest.TestCase):
    def test_batch_uses_one_plant_read_and_raw_topics(self):
        service, plant = service_two_nodes()
        batch = service.telemetry_batch()
        self.assertEqual(plant.calls, 1)
        self.assertEqual([topic for topic, _ in batch], ["raw/1", "raw/2"])
        first, second = [Frame.from_bytes(payload) for _, payload in batch]
        self.assertEqual(len(batch[0][1]), 94)
        self.assertEqual(first.header.type, TELEMETRY_TYPE)
        self.assertEqual(first.header.temperature_mC, 21_845)
        self.assertEqual(second.header.temperature_mC, 21_345)
        self.assertTrue(verify_frame(first, b"A" * 32))
        self.assertTrue(verify_frame(second, b"B" * 32))
        self.assertFalse(verify_frame(first, b"B" * 32))

    def test_challenge_echoes_nonce_and_uses_same_boot_and_sequence(self):
        service, _ = service_two_nodes()
        telemetry = Frame.from_bytes(service.telemetry_batch()[0][1])
        nonce = bytes(range(16))
        response = service.challenge(1, nonce)
        self.assertEqual(response.header.type, CHALLENGE_RESPONSE_TYPE)
        self.assertEqual(response.header.nonce, nonce)
        self.assertEqual(response.header.boot_id, telemetry.header.boot_id)
        self.assertEqual(response.header.sequence_no, telemetry.header.sequence_no + 1)
        self.assertTrue(verify_frame(response, b"A" * 32))

    def test_concurrent_frame_sequence_unique(self):
        producer = NodeFrameProducer(node_id=1, zone_id=1, key_version=1,
                                     key=b"x" * 32, protocol_version=1)
        measurement = SensorMeasurement(25_000, 45_000)
        with ThreadPoolExecutor(max_workers=6) as pool:
            frames = list(pool.map(lambda _: producer.emit(1, measurement), range(100)))
        self.assertEqual(set(f.header.sequence_no for f in frames), set(range(1, 101)))
        self.assertEqual(len(set(f.header.nonce for f in frames)), 100)

    def test_short_key_rejected(self):
        with self.assertRaises(ValueError):
            NodeFrameProducer(node_id=1, zone_id=1, key_version=1,
                              key=b"weak", protocol_version=1)

    def test_invalid_challenge_nonce_not_consumed(self):
        service, _ = service_two_nodes()
        with self.assertRaises(ValueError):
            service.challenge(1, b"short")
        first = Frame.from_bytes(service.telemetry_batch()[0][1])
        self.assertEqual(first.header.sequence_no, 1)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.service, self.plant = service_two_nodes()

    def test_challenge_endpoint_round_trip(self):
        client = TestClient(create_probe_app(self.service))
        response = client.post("/v1/challenge", json={"node_id": 1,
                             "nonce_hex": (b"q" * 16).hex()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"frame_hex"})
        frame = Frame.from_hex(response.json()["frame_hex"])
        self.assertEqual(len(response.json()["frame_hex"]), 188)
        self.assertTrue(verify_frame(frame, b"A" * 32))
        self.assertEqual(frame.header.nonce, b"q" * 16)

    def test_invalid_challenge_and_unknown_node(self):
        client = TestClient(create_probe_app(self.service))
        for body, expected in (({"node_id": 1, "nonce_hex": "xyz"}, 422),
                               ({"node_id": 3, "nonce_hex": "00" * 16}, 404),
                               ({"node_id": True, "nonce_hex": "00" * 16}, 422),
                               ({"node_id": 1, "nonce_hex": "00" * 16, "key": "x"}, 422)):
            with self.subTest(body=body):
                self.assertEqual(client.post("/v1/challenge", json=body).status_code, expected)

    def test_probe_has_no_control_interface(self):
        client = TestClient(create_probe_app(self.service))
        self.assertEqual(client.get("/healthz").status_code, 404)
        self.assertEqual(client.get("/docs").status_code, 404)

    def test_control_health_limited_by_source_and_surface(self):
        app = create_control_app()
        denied = TestClient(app)
        self.assertEqual(denied.get("/healthz").status_code, 403)
        permitted = TestClient(app, client=("10.60.30.10", 12000))
        result = permitted.get("/healthz")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(set(result.json()), {"status", "service_version"})
        self.assertEqual(permitted.post("/v1/challenge", json={}).status_code, 404)
        self.assertEqual(permitted.get("/docs").status_code, 404)

    def test_unavailable_plant_does_not_return_secrets(self):
        self.plant.read = MagicMock(side_effect=OSError("private filesystem: /private/truth.db"))
        client = TestClient(create_probe_app(self.service))
        resp = client.post("/v1/challenge", json={"node_id": 1, "nonce_hex": "00" * 16})
        self.assertEqual(resp.status_code, 503)
        self.assertNotIn("truth", resp.text)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "nodes.json"
        self.keys_path = Path(self.tmp.name) / "keys.json"
        self.config = {
            "protocol_version": 1,
            "telemetry_interval_ms": 1000,
            "nodes": [{"node_id": 1, "zone_id": 3, "key_version": 2}],
        }
        self.keys = {"1": {"2": "01" * 32}}

    def load(self):
        self.config_path.write_text(json.dumps(self.config))
        self.keys_path.write_text(json.dumps(self.keys))
        return load_config(self.config_path, self.keys_path)

    def test_config_and_keys_are_separate(self):
        config = self.load()
        self.assertEqual(config.nodes[0].node_id, 1)
        self.assertEqual(config.nodes[0].key, b"\x01" * 32)
        self.assertNotIn("01" * 32, json.dumps(self.config))

    def test_reject_duplicate_nodes(self):
        self.config["nodes"].append(dict(self.config["nodes"][0]))
        with self.assertRaises(ValueError):
            self.load()

    def test_reject_missing_wrong_short_keys(self):
        for key_versions in ({}, {"2": "zz"}, {"2": "01" * 5}):
            self.keys = {"1": key_versions}
            with self.subTest(key_versions=key_versions), self.assertRaises(ValueError):
                self.load()

    def test_reject_invalid_configuration(self):
        for field, value in (("protocol_version", True), ("telemetry_interval_ms", 0)):
            old = self.config[field]
            self.config[field] = value
            with self.assertRaises(ValueError):
                self.load()
            self.config[field] = old


class PublisherTests(unittest.TestCase):
    def test_exact_raw_topic_and_qos(self):
        publisher = MqttPublisher("/unused")
        publisher.connected.set()
        info = MagicMock(rc=0)
        info.is_published.return_value = True
        publisher.client = MagicMock()
        publisher.client.publish.return_value = info
        publisher.publish("raw/1", bytes(94))
        publisher.client.publish.assert_called_once_with(
            "raw/1", bytes(94), qos=1, retain=False)

    def test_connection_required(self):
        publisher = MqttPublisher("/unused")
        publisher.connected = MagicMock()
        publisher.connected.wait.return_value = False
        with self.assertRaises(RuntimeError):
            publisher.publish("raw/1", bytes(94))


if __name__ == "__main__":
    unittest.main()
