"""Unit and local API tests for eacld-control."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from src.app import create_app
from src.injector import ProcessIntervention
from src.orchestrator import AGENTS, main
from src.plant import PlantParameters, PlantState, advance
from src.private_store import PrivateStore


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "private" / "state.sqlite3"
        self.store = PrivateStore(self.db)

    def test_initial_state_matches_contract(self):
        self.assertEqual(self.store.public_state(), {
            "temperature_mC": 22000, "humidity_mPct": 50000, "step_id": 0})

    def test_deterministic_step_and_persistence_across_instances(self):
        params = PlantParameters(temperature_relaxation_ppm=0,
                                 humidity_relaxation_ppm=0, heater_gain_mC_per_unit=250)
        self.store.reset(PlantState(0, 21000, 40000, 2), params)
        self.assertEqual(self.store.step(3), {
            "temperature_mC": 22500, "humidity_mPct": 40000, "step_id": 3})
        self.assertEqual(PrivateStore(self.db).public_state()["step_id"], 3)

    def test_private_intervention_only_applies_in_range(self):
        self.store.reset(PlantState(0, 20000, 50000),
                         PlantParameters(ambient_temperature_mC=20000,
                                         temperature_relaxation_ppm=0,
                                         humidity_relaxation_ppm=0))
        self.store.schedule(ProcessIntervention(2, 4, 800, -1000))
        self.assertEqual(self.store.step(1)["temperature_mC"], 20000)
        self.assertEqual(self.store.step(2)["temperature_mC"], 21600)
        self.assertEqual(self.store.step(1)["temperature_mC"], 21600)
        snapshot = self.store.private_snapshot()
        self.assertEqual([row["cause_labels"] for row in snapshot["private_steps"]],
                         ["", "H1", "H1", ""])
        self.assertEqual(self.store.public_state()["humidity_mPct"], 48000)
        self.assertNotIn("cause_label", self.store.public_state())

    def test_reject_past_schedule(self):
        self.store.step(2)
        with self.assertRaises(ValueError):
            self.store.schedule(ProcessIntervention(2, 5, 100))
        self.assertEqual(self.store.private_snapshot()["interventions"], [])

    def test_reset_erases_private_history(self):
        self.store.schedule(ProcessIntervention(1, 2, 10))
        self.store.step()
        self.store.reset(PlantState(0, 10000, 30000), PlantParameters())
        state = self.store.private_snapshot()
        self.assertEqual(state["interventions"], [])
        self.assertEqual(state["private_steps"], [])
        self.assertEqual(state["plant"]["temperature_mC"], 10000)

    def test_public_endpoint_only_exposes_three_fields(self):
        self.store.schedule(ProcessIntervention(1, 3, 750))
        self.store.step()
        with TestClient(create_app(str(self.db))) as client:
            response = client.get("/v1/state")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(set(response.json()),
                             {"temperature_mC", "humidity_mPct", "step_id"})
            self.assertEqual(client.get("/healthz").json()["status"], "ok")
            for path in ("/private", "/v1/interventions", "/docs", "/openapi.json"):
                self.assertEqual(client.get(path).status_code, 404)
            self.assertEqual(client.post("/v1/state", json={}).status_code, 405)

    def test_deterministic_repeated_run(self):
        self.store.reset(PlantState(0, 18450, 42000), PlantParameters())
        first = self.store.step(12)
        self.store.reset(PlantState(0, 18450, 42000), PlantParameters())
        second = self.store.step(12)
        self.assertEqual(first, second)

    def test_atomic_concurrent_steps(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.store.step, 5) for _ in range(4)]
            for f in futures:
                f.result()
        self.assertEqual(self.store.public_state()["step_id"], 20)
        self.assertEqual(len(self.store.private_snapshot()["private_steps"]), 20)

    def test_invalid_plant_ranges(self):
        with self.assertRaises(ValueError):
            PlantParameters(temperature_relaxation_ppm=1_000_001)
        with self.assertRaises(ValueError):
            PlantState(0, 22000, 100001)
        with self.assertRaises(ValueError):
            ProcessIntervention(0, 1)
        with self.assertRaises(ValueError):
            ProcessIntervention(2, 3, cause_label="H3")

    def test_integer_signed_rounding(self):
        initial = PlantState(0, -1, 50000)
        config = PlantParameters(ambient_temperature_mC=0,
                                 temperature_relaxation_ppm=500000)
        self.assertEqual(advance(initial, config).temperature_mC, 0)

    def test_cli_operates_private_database(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--db", str(self.db), "inject", "--start-step", "2",
                                   "--end-step", "4", "--temperature-disturbance-mC", "7"]), 0)
            self.assertEqual(main(["--db", str(self.db), "step", "--count", "4"]), 0)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(records[0]["scheduled"])
        self.assertEqual(records[1]["step_id"], 4)

    def test_agent_host_mapping(self):
        self.assertEqual(AGENTS, {
            "nodes": "10.60.30.12", "reference": "10.60.30.13",
            "edge": "10.60.30.14", "core": "10.60.30.15"})


if __name__ == "__main__":
    unittest.main()
