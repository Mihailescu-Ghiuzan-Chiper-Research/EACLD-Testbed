"""Persisted anti-replay high watermarks for each signed node/boot pair.

Updates are attempted only for valid authenticated telemetry. An invalid
frame must not poison the watermark for subsequent legitimate readings.
"""


def classify_and_advance(cursor, *, node_id: int, boot_id: bytes, sequence_no: int) -> str:
    cursor.execute(
        """
        INSERT INTO sequence_watermarks(node_id, boot_id, max_sequence)
        VALUES (%s, %s, %s)
        ON CONFLICT (node_id, boot_id) DO UPDATE
          SET max_sequence = EXCLUDED.max_sequence
          WHERE sequence_watermarks.max_sequence < EXCLUDED.max_sequence
        RETURNING node_id
        """,
        (node_id, boot_id, sequence_no),
    )
    return "fresh" if cursor.fetchone() is not None else "stale"
