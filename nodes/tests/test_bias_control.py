"""VM-NODES restricted H2 bias control and authenticated measurement tests."""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from eacld_contracts.frame import Frame, verify_frame
from src.nodes import (
    NodeDefinition, NodesConfiguration, NodesService, create_control_app,
)
from src.sensor import PlantObservation, SensorBias


class FixedPlant:
    def __init__(self):
        self.state = PlantObservation(22000, 50000, 0)

    def read(self):
        return self.state


class BiasControlTests(unittest.TestCase):
    def setUp(self):
        self.plant = FixedPlant()
        config = NodesConfiguration(
            protocol_version=1, telemetry_interval_ms=1000,
            nodes=(
                NodeDefinition(1, 1, 1, b"A" * 32, SensorBias()),
                NodeDefinition(2, 1, 1, b"B" * 32, SensorBias()),
            ),
        )
        self.service = NodesService(config, self.plant)
        self.app = create_control_app(self.service)
        self.authorized = TestClient(self.app, client=("10.60.30.10", 12345))
        self.denied = TestClient(self.app, client=("10.60.20.15", 12345))
        self.url = "/v1/nodes/bias"

    def test_runtime_bias_affects_node_only_and_restores_without_restart(self):
        before = self.authorized.get(self.url, params={"node_id": 1})
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.json(), {
            "node_id": 1, "bias_temperature_mC": 0,
            "bias_humidity_mPct": 0, "revision": 0,
        })
        updated = self.authorized.post(self.url, json={
            "node_id": 1, "bias_temperature_mC": 6912,
            "bias_humidity_mPct": 0,
        })
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["revision"], 1)
        self.assertEqual(self.authorized.get(self.url, params={"node_id": 1}).json(),
                         updated.json())
        batch = self.service.telemetry_batch()
        changed, unchanged = (Frame.from_bytes(frame) for _, frame in batch)
        self.assertEqual(changed.header.temperature_mC, 28912)
        self.assertEqual(unchanged.header.temperature_mC, 22000)
        self.assertTrue(verify_frame(changed, b"A" * 32))
        self.assertTrue(verify_frame(unchanged, b"B" * 32))
        challenge = self.service.challenge(1, b"n" * 16)
        self.assertEqual(challenge.header.temperature_mC, 28912)
        self.assertEqual(self.plant.state.temperature_mC, 22000)

        restored = self.authorized.post(self.url, json={
            "node_id": 1, "bias_temperature_mC": 0,
            "bias_humidity_mPct": 0,
        })
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["revision"], 2)
        self.assertEqual(Frame.from_bytes(self.service.telemetry_batch()[0][1]).header.temperature_mC, 22000)

    def test_restricted_source_and_validated_input(self):
        self.assertEqual(self.denied.get(self.url, params={"node_id": 1}).status_code, 403)
        self.assertEqual(self.denied.post(self.url, json={
            "node_id": 1, "bias_temperature_mC": 10,
            "bias_humidity_mPct": 0,
        }).status_code, 403)
        for command in (
            {"node_id": True, "bias_temperature_mC": 1, "bias_humidity_mPct": 0},
            {"node_id": 1, "bias_temperature_mC": "1", "bias_humidity_mPct": 0},
            {"node_id": 1, "bias_temperature_mC": 2**31, "bias_humidity_mPct": 0},
            {"node_id": 1, "bias_temperature_mC": 2, "bias_humidity_mPct": 0, "cause": "H2"},
        ):
            with self.subTest(command=command):
                self.assertEqual(self.authorized.post(self.url, json=command).status_code, 422)
        self.assertEqual(self.authorized.post(self.url, json={
            "node_id": 3, "bias_temperature_mC": 1,
            "bias_humidity_mPct": 0,
        }).status_code, 404)
        self.assertEqual(self.authorized.get(self.url, params={"node_id": 3}).status_code, 404)
        self.assertEqual(self.authorized.get("/docs").status_code, 404)
        self.assertEqual(self.service.get_bias(1)["revision"], 0)
        self.assertEqual(self.service.get_bias(2)["revision"], 0)

    def test_bias_cannot_be_read_from_probe_or_public_observation(self):
        from src.challenge import create_probe_app
        probe = TestClient(create_probe_app(self.service))
        self.authorized.post(self.url, json={
            "node_id": 1, "bias_temperature_mC": 3500,
            "bias_humidity_mPct": 0,
        })
        self.assertEqual(probe.get("/v1/nodes/bias?node_id=1").status_code, 404)
        self.assertEqual(probe.post("/v1/nodes/bias", json={}).status_code, 404)
        self.assertFalse(hasattr(self.plant.state, "bias_temperature_mC"))


if __name__ == "__main__":
    unittest.main()
