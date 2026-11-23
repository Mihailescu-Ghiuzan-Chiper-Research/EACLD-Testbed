"""Local unit and HTTP contract tests for the VM-REFERENCE service."""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from eacld_contracts.interfaces import PLANT_STATE, REFERENCE_READ
from src.reference import (
    MAX_PLANT_RESPONSE_BYTES,
    PlantReferenceClient,
    PublicPlantReading,
    ReferenceAcquisition,
    create_control_app,
    create_probe_app,
)


class FakePlant:
    def __init__(self):
        self.reads = 0
        self.temperature = 21750

    def read(self):
        self.reads += 1
        return PublicPlantReading(self.temperature, 45000, self.reads)


def make_acquisition(plant=None):
    return ReferenceAcquisition(
        plant or FakePlant(),
        clock=lambda: datetime(2026, 11, 10, 14, 23, 45, 123456, tzinfo=timezone.utc),
    )


class PublicPlantTests(unittest.TestCase):
    def test_parse_public_fields_drops_private_extra(self):
        payload = {"temperature_mC": 21550, "humidity_mPct": 50000,
                   "step_id": 25, "cause_label": "H1", "ground_truth": "private"}
        sample = PublicPlantReading.from_public_json(payload)
        self.assertEqual(sample, PublicPlantReading(21550, 50000, 25))
        self.assertNotIn("ground_truth", repr(sample))
        self.assertNotIn("H1", repr(sample))

    def test_invalid_public_fields_rejected(self):
        invalid = [
            {"temperature_mC": True, "humidity_mPct": 1, "step_id": 0},
            {"temperature_mC": 1, "humidity_mPct": -1, "step_id": 0},
            {"temperature_mC": 1, "humidity_mPct": 100001, "step_id": 0},
            {"temperature_mC": 1, "humidity_mPct": 1, "step_id": -1},
            {"temperature_mC": 1, "humidity_mPct": 1},
            {"temperature_mC": 2**31, "humidity_mPct": 1, "step_id": 0},
            [1, 2, 3],
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                PublicPlantReading.from_public_json(payload)

    def test_negative_temperature_is_valid(self):
        sample = PublicPlantReading.from_public_json({
            "temperature_mC": -27000, "humidity_mPct": 0, "step_id": 0})
        self.assertEqual(sample.temperature_mC, -27000)


class ClientTests(unittest.TestCase):
    def make_response(self, payload, status=200):
        stream = io.BytesIO(json.dumps(payload).encode())
        stream.status = status
        return stream

    def test_only_public_process_get_on_net_plant(self):
        client = PlantReferenceClient()
        response = self.make_response({"temperature_mC": 20001, "humidity_mPct": 40000,
                                       "step_id": 10, "private_truth": "must-not-escape"})
        client._opener = MagicMock()
        client._opener.open.return_value.__enter__.return_value = response
        sample = client.read()
        request = client._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, PLANT_STATE.url)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(sample, PublicPlantReading(20001, 40000, 10))

    def test_http_non_success_rejected(self):
        client = PlantReferenceClient()
        client._opener = MagicMock()
        client._opener.open.return_value.__enter__.return_value = self.make_response({}, 500)
        with self.assertRaises(RuntimeError):
            client.read()

    def test_overlarge_upstream_body_rejected(self):
        client = PlantReferenceClient()
        client._opener = MagicMock()
        stream = io.BytesIO(b"x" * (MAX_PLANT_RESPONSE_BYTES + 1))
        stream.status = 200
        client._opener.open.return_value.__enter__.return_value = stream
        with self.assertRaises(ValueError):
            client.read()

    def test_malformed_upstream_json_rejected(self):
        client = PlantReferenceClient()
        client._opener = MagicMock()
        stream = io.BytesIO(b"not-json")
        stream.status = 200
        client._opener.open.return_value.__enter__.return_value = stream
        with self.assertRaises(ValueError):
            client.read()

    def test_nonpositive_timeout_rejected(self):
        with self.assertRaises(ValueError):
            PlantReferenceClient(timeout=0)


class ReferenceApiTests(unittest.TestCase):
    def test_exact_fields_and_timestamp(self):
        client = TestClient(create_probe_app(make_acquisition()))
        response = client.post("/v1/reference/read")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "temperature_mC": 21750,
            "status": "ok",
            "timestamp": "2026-11-10T14:23:45.123Z",
        })
        self.assertEqual(REFERENCE_READ.url,
                         "http://10.60.20.13:8201/v1/reference/read")

    def test_each_post_reads_current_process_without_cache(self):
        plant = FakePlant()
        client = TestClient(create_probe_app(make_acquisition(plant)))
        self.assertEqual(client.post(REFERENCE_READ.path).json()["temperature_mC"], 21750)
        plant.temperature = 23123
        self.assertEqual(client.post(REFERENCE_READ.path).json()["temperature_mC"], 23123)
        self.assertEqual(plant.reads, 2)

    def test_probe_has_no_private_or_control_routes(self):
        client = TestClient(create_probe_app(make_acquisition()))
        for path in ("/healthz", "/v1/state", "/private", "/docs", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 404)
        self.assertEqual(client.get("/v1/reference/read").status_code, 405)

    def test_unreachable_upstream_returns_503_without_leaking_error(self):
        plant = FakePlant()
        plant.read = MagicMock(side_effect=OSError("secret=private-truth"))
        client = TestClient(create_probe_app(make_acquisition(plant)))
        response = client.post(REFERENCE_READ.path)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-truth", response.text)
        self.assertEqual(response.json()["detail"], "reference observation unavailable")

    def test_malformed_upstream_returns_503_without_inventing_reading(self):
        plant = FakePlant()
        plant.read = MagicMock(side_effect=ValueError("invalid field"))
        client = TestClient(create_probe_app(make_acquisition(plant)))
        response = client.post(REFERENCE_READ.path)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("temperature_mC", response.text)

    def test_timezone_aware_timestamp_is_normalized_to_utc(self):
        from datetime import timedelta
        acquisition = ReferenceAcquisition(FakePlant(), clock=lambda: datetime(
            2026, 11, 10, 16, 23, 45, tzinfo=timezone(timedelta(hours=2))))
        self.assertEqual(acquisition.read().timestamp, "2026-11-10T14:23:45.000Z")

    def test_naive_clock_rejected(self):
        acquisition = ReferenceAcquisition(FakePlant(), clock=lambda: datetime(2026, 11, 10))
        with self.assertRaises(ValueError):
            acquisition.read()


class ControlApiTests(unittest.TestCase):
    def test_unknown_source_denied(self):
        client = TestClient(create_control_app())
        self.assertEqual(client.get("/healthz").status_code, 403)

    def test_vm_control_source_allowed(self):
        client = TestClient(create_control_app(), client=("10.60.30.10", 13456))
        result = client.get("/healthz")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {"status": "ok", "service_version": "0.1.0"})

    def test_local_health_allowed_for_container_check(self):
        client = TestClient(create_control_app(), client=("127.0.0.1", 54321))
        self.assertEqual(client.get("/healthz").status_code, 200)

    def test_control_app_has_no_reference_or_private_routes(self):
        client = TestClient(create_control_app(), client=("10.60.30.10", 12345))
        self.assertEqual(client.post(REFERENCE_READ.path).status_code, 404)
        self.assertEqual(client.get("/private").status_code, 404)
        self.assertEqual(client.get("/docs").status_code, 404)


if __name__ == "__main__":
    unittest.main()
