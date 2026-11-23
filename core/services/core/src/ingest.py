"""MQTT accepted/+ ingestion and PostgreSQL persistence without ground truth."""
from __future__ import annotations

from dataclasses import dataclass
import logging

import psycopg
from eacld_contracts.frame import Frame, FRAME_SIZE, TELEMETRY_TYPE

from .sequence_registry import classify_and_advance

LOGGER = logging.getLogger(__name__)


def accepted_node(topic: str) -> int:
    if not isinstance(topic, str):
        raise ValueError("topic must be text")
    parts = topic.split("/")
    if len(parts) != 2 or parts[0] != "accepted":
        raise ValueError("only accepted/<node_id> is allowed")
    text = parts[1]
    if not text or not text.isascii() or not text.isdecimal():
        raise ValueError("invalid node_id")
    node = int(text)
    if node > 65535 or str(node) != text:
        raise ValueError("node_id is not canonical uint16")
    return node


@dataclass(frozen=True)
class Reading:
    id: int
    node_id: int
    payload: bytes
    mac_status: str
    sequence_status: str


class PostgresStore:
    """Postgres is the only runtime store for telemetry, state and decisions."""

    def __init__(self, conninfo: str):
        self.conninfo = conninfo

    def connect(self):
        return psycopg.connect(self.conninfo, connect_timeout=4)

    def initialize(self) -> None:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sequence_watermarks(
                    node_id INTEGER NOT NULL,
                    boot_id BYTEA NOT NULL,
                    max_sequence NUMERIC(20,0) NOT NULL,
                    PRIMARY KEY(node_id, boot_id)
                )""")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS received_telemetry(
                    id BIGSERIAL PRIMARY KEY,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    node_id INTEGER NOT NULL,
                    payload BYTEA NOT NULL,
                    mac_status TEXT NOT NULL,
                    sequence_status TEXT NOT NULL
                )""")
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS received_telemetry_node_idx
                ON received_telemetry(node_id, id DESC)""")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS diagnoses(
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    episode_id TEXT NOT NULL,
                    block_id TEXT NOT NULL,
                    node_id INTEGER NOT NULL,
                    telemetry_id BIGINT NOT NULL REFERENCES received_telemetry(id),
                    result JSONB NOT NULL,
                    UNIQUE(episode_id)
                )""")

    def append(self, topic: str, payload: bytes, verifier) -> Reading:
        node = accepted_node(topic)
        if not isinstance(payload, bytes) or len(payload) > 4096:
            raise ValueError("invalid payload length")
        mac_status = verifier.verify(node, payload)
        sequence_status = "unavailable"
        with self.connect() as conn, conn.cursor() as cursor:
            if mac_status == "valid" and len(payload) == FRAME_SIZE:
                header = Frame.from_bytes(payload).header
                if header.type == TELEMETRY_TYPE and header.node_id == node:
                    sequence_status = classify_and_advance(
                        cursor, node_id=node, boot_id=header.boot_id,
                        sequence_no=header.sequence_no
                    )
            cursor.execute("""
                INSERT INTO received_telemetry
                    (node_id, payload, mac_status, sequence_status)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """, (node, payload, mac_status, sequence_status))
            record_id = cursor.fetchone()[0]
        return Reading(record_id, node, payload, mac_status, sequence_status)

    def latest(self, node_id: int) -> Reading | None:
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, node_id, payload, mac_status, sequence_status
                FROM received_telemetry WHERE node_id=%s
                ORDER BY id DESC LIMIT 1
                """, (node_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        return Reading(int(row[0]), int(row[1]), bytes(row[2]), row[3], row[4])

    def record_diagnosis(self, public_result: dict, *, node_id: int, telemetry_id: int) -> None:
        from psycopg.types.json import Jsonb
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO diagnoses(episode_id, block_id, node_id, telemetry_id, result)
                VALUES (%s, %s, %s, %s, %s)
                """, (public_result["episode_id"], public_result["block_id"],
                      node_id, telemetry_id, Jsonb(public_result)))
