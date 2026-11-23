"""Transactional private VM-CONTROL state and ground-truth store.

The SQLite database lives exclusively under control/volumes/private on
VM-CONTROL. Only public_state() is used by the plant observation HTTP API.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import os
from pathlib import Path
import sqlite3
from typing import Iterator

from .injector import ProcessIntervention
from .plant import PlantParameters, PlantState, advance

DEFAULT_DB_PATH = "/var/lib/eacld/private/state.sqlite3"


class PrivateStore:
    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        previous = os.umask(0o077)
        try:
            conn = sqlite3.connect(self.path, timeout=30.0)
        finally:
            os.umask(previous)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create the schema and initial state once, without resetting a prior run."""
        with self._connection() as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS plant (
                id INTEGER PRIMARY KEY CHECK (id=1),
                step_id INTEGER NOT NULL,
                temperature_mC INTEGER NOT NULL,
                humidity_mPct INTEGER NOT NULL,
                heater_input_units INTEGER NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS parameters (
                id INTEGER PRIMARY KEY CHECK (id=1),
                ambient_temperature_mC INTEGER NOT NULL,
                ambient_humidity_mPct INTEGER NOT NULL,
                temperature_relaxation_ppm INTEGER NOT NULL,
                humidity_relaxation_ppm INTEGER NOT NULL,
                heater_gain_mC_per_unit INTEGER NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS interventions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_step INTEGER NOT NULL,
                end_step INTEGER NOT NULL,
                temperature_disturbance_mC INTEGER NOT NULL,
                humidity_disturbance_mPct INTEGER NOT NULL,
                cause_label TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS private_steps (
                step_id INTEGER PRIMARY KEY,
                temperature_mC INTEGER NOT NULL,
                humidity_mPct INTEGER NOT NULL,
                temperature_disturbance_mC INTEGER NOT NULL,
                humidity_disturbance_mPct INTEGER NOT NULL,
                cause_labels TEXT NOT NULL
            )""")
            initial = PlantParameters()
            conn.execute("""INSERT OR IGNORE INTO plant VALUES (1,0,?,?,0)""",
                         (initial.ambient_temperature_mC, initial.ambient_humidity_mPct))
            conn.execute("""INSERT OR IGNORE INTO parameters VALUES (1,?,?,?,?,?)""",
                         tuple(asdict(initial).values()))
        self.path.chmod(0o600)

    @staticmethod
    def _state(conn: sqlite3.Connection) -> PlantState:
        row = conn.execute("SELECT * FROM plant WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("virtual process state missing")
        return PlantState(row["step_id"], row["temperature_mC"],
                          row["humidity_mPct"], row["heater_input_units"])

    @staticmethod
    def _parameters(conn: sqlite3.Connection) -> PlantParameters:
        row = conn.execute("SELECT * FROM parameters WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("virtual process parameters missing")
        return PlantParameters(**{name: row[name] for name in
                                  PlantParameters.__dataclass_fields__})

    def public_state(self) -> dict[str, int]:
        self.initialize()
        with self._connection() as conn:
            return self._state(conn).public()

    def reset(self, state: PlantState, parameters: PlantParameters) -> None:
        """Start a new private run, clearing prior interventions and its audit trail."""
        if not isinstance(state, PlantState) or not isinstance(parameters, PlantParameters):
            raise TypeError("reset requires PlantState and PlantParameters")
        if state.step_id != 0:
            raise ValueError("a new run must begin at step 0")
        self.initialize()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM interventions")
                conn.execute("DELETE FROM private_steps")
                conn.execute("""UPDATE plant SET step_id=?,temperature_mC=?,
                    humidity_mPct=?,heater_input_units=? WHERE id=1""",
                             tuple(asdict(state).values()))
                conn.execute("""UPDATE parameters SET ambient_temperature_mC=?,
                    ambient_humidity_mPct=?,temperature_relaxation_ppm=?,
                    humidity_relaxation_ppm=?,heater_gain_mC_per_unit=? WHERE id=1""",
                             tuple(asdict(parameters).values()))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def set_heater(self, units: int) -> None:
        if type(units) is not int or not -100 <= units <= 100:
            raise ValueError("heater input must be an integer in [-100, 100]")
        self.initialize()
        with self._connection() as conn, conn:
            conn.execute("UPDATE plant SET heater_input_units=? WHERE id=1", (units,))

    def schedule(self, intervention: ProcessIntervention) -> None:
        if not isinstance(intervention, ProcessIntervention):
            raise TypeError("schedule requires a ProcessIntervention")
        self.initialize()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                next_step = self._state(conn).step_id + 1
                if intervention.start_step < next_step:
                    raise ValueError("intervention must begin at a future step")
                conn.execute("""INSERT INTO interventions (
                    start_step,end_step,temperature_disturbance_mC,
                    humidity_disturbance_mPct,cause_label
                ) VALUES (?,?,?,?,?)""", tuple(asdict(intervention).values()))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def step(self, count: int = 1) -> dict[str, int]:
        if type(count) is not int or not 1 <= count <= 100_000:
            raise ValueError("count must be an integer in [1, 100000]")
        self.initialize()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                state = self._state(conn)
                parameters = self._parameters(conn)
                for _ in range(count):
                    next_step = state.step_id + 1
                    active = conn.execute("""SELECT * FROM interventions
                        WHERE start_step <= ? AND end_step > ? ORDER BY id""",
                        (next_step, next_step)).fetchall()
                    temp_offset = sum(row["temperature_disturbance_mC"] for row in active)
                    humidity_offset = sum(row["humidity_disturbance_mPct"] for row in active)
                    state = advance(state, parameters,
                        temperature_disturbance_mC=temp_offset,
                        humidity_disturbance_mPct=humidity_offset)
                    labels = ",".join(sorted({row["cause_label"] for row in active}))
                    conn.execute("""INSERT INTO private_steps VALUES (?,?,?,?,?,?)""",
                        (state.step_id, state.temperature_mC, state.humidity_mPct,
                         temp_offset, humidity_offset, labels))
                conn.execute("""UPDATE plant SET step_id=?,temperature_mC=?,
                    humidity_mPct=?,heater_input_units=? WHERE id=1""",
                    tuple(asdict(state).values()))
                conn.commit()
                return state.public()
            except BaseException:
                conn.rollback()
                raise

    def private_snapshot(self) -> dict[str, object]:
        """For local operator tooling only; must never be used by the HTTP API."""
        self.initialize()
        with self._connection() as conn:
            return {
                "plant": asdict(self._state(conn)),
                "parameters": asdict(self._parameters(conn)),
                "interventions": [dict(row) for row in conn.execute(
                    "SELECT * FROM interventions ORDER BY id")],
                "private_steps": [dict(row) for row in conn.execute(
                    "SELECT * FROM private_steps ORDER BY step_id")],
            }
