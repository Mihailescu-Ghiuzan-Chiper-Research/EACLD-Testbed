"""Reference probe outage is observable over the existing NET-PROBE interface."""
from __future__ import annotations

import unittest
from fastapi.testclient import TestClient

from eacld_contracts.probe_condition import ProbeCondition
from src.reference import ReferenceReadResponse, create_control_app, create_probe_app


class ReferenceDegradationTests(unittest.TestCase):
    def test_reference_probe_outage_and_restoration(self):
        class Acquisition:
            def __init__(self):
                self.calls = 0
            def read(self):
                self.calls += 1
                return ReferenceReadResponse(
                    temperature_mC=22000, status="ok",
                    timestamp="2026-11-18T00:00:00Z")
        condition = ProbeCondition()
        acquisition = Acquisition()
        control = TestClient(create_control_app(condition=condition),
                             client=("10.60.30.10", 10000))
        probe = TestClient(create_probe_app(acquisition, condition))
        self.assertEqual(control.post("/v1/probe-condition", json={
            "unavailable": True, "latency_ms": 0}).status_code, 200)
        self.assertEqual(probe.post("/v1/reference/read").status_code, 503)
        self.assertEqual(acquisition.calls, 0)
        self.assertEqual(control.post("/v1/probe-condition", json={
            "unavailable": False, "latency_ms": 0}).status_code, 200)
        self.assertEqual(probe.post("/v1/reference/read").status_code, 200)
        self.assertEqual(acquisition.calls, 1)
        self.assertEqual(control.get("/v1/probe-condition").json()["revision"], 2)
        self.assertEqual(control.post("/v1/probe-condition", json={
            "unavailable": "true", "latency_ms": 0}).status_code, 422)
        denied = TestClient(create_control_app(condition=condition))
        self.assertEqual(denied.get("/v1/probe-condition").status_code, 403)


if __name__ == "__main__":
    unittest.main()
