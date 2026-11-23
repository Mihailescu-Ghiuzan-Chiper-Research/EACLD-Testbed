"""Controlled NET-PROBE faults must affect actual HTTP requests and remain restricted."""
from __future__ import annotations

from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient

from eacld_contracts.probe_condition import ProbeCondition
from src.nodes import create_control_app
from src.challenge import create_probe_app


class ProbeConditionTests(unittest.TestCase):
    def test_condition_revision_delay_and_strict_validation(self):
        calls = []
        condition = ProbeCondition(sleep=calls.append)
        self.assertEqual(condition.snapshot()["revision"], 0)
        condition.configure(unavailable=False, latency_ms=120)
        condition.apply()
        self.assertEqual(calls, [.12])
        condition.configure(unavailable=True, latency_ms=0)
        with self.assertRaises(RuntimeError):
            condition.apply()
        self.assertEqual(condition.snapshot()["revision"], 2)
        for bad in ({"unavailable": 1, "latency_ms": 1},
                    {"unavailable": False, "latency_ms": 2001}):
            with self.assertRaises(ValueError):
                condition.configure(**bad)

    def test_node_challenge_really_returns_503_and_control_restores(self):
        calls = []
        def challenge(node_id, nonce):
            calls.append(node_id)
            return SimpleNamespace(to_hex=lambda: "signed-frame")
        service = SimpleNamespace(producers={1: object()}, challenge=challenge)
        condition = ProbeCondition()
        control = TestClient(create_control_app(service, condition=condition),
                             client=("10.60.30.10", 10000))
        probe = TestClient(create_probe_app(service, condition))
        url = "/v1/probe-condition"
        update = {"unavailable": True, "latency_ms": 0}
        self.assertEqual(control.post(url, json=update).status_code, 200)
        response = probe.post("/v1/challenge", json={
            "node_id": 1, "nonce_hex": "a1" * 16})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(calls, [])
        self.assertTrue(control.get(url).json()["unavailable"])
        self.assertEqual(control.post(url, json={"unavailable": False,
                              "latency_ms": 0}).status_code, 200)
        self.assertEqual(probe.post("/v1/challenge", json={
            "node_id": 1, "nonce_hex": "a1" * 16}).status_code, 200)
        self.assertEqual(calls, [1])
        self.assertEqual(control.post(url, json={"unavailable": False,
                              "latency_ms": 2500}).status_code, 422)
        denied = TestClient(create_control_app(service, condition=condition))
        self.assertEqual(denied.get(url).status_code, 403)


if __name__ == "__main__":
    unittest.main()
