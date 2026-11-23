"""Controlled probe-service availability and latency for distributed experiments."""
from __future__ import annotations

from threading import RLock
import time


class ProbeCondition:
    """Shared state between the NET-CONTROL and NET-PROBE HTTP listeners."""

    def __init__(self, *, sleep=time.sleep):
        self._lock = RLock()
        self._unavailable = False
        self._latency_ms = 0
        self._revision = 0
        self._sleep = sleep

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "unavailable": self._unavailable,
                "latency_ms": self._latency_ms,
                "revision": self._revision,
            }

    def configure(self, *, unavailable: bool, latency_ms: int) -> dict:
        if type(unavailable) is not bool or type(latency_ms) is not int:
            raise ValueError("invalid probe-condition type")
        if not 0 <= latency_ms <= 2000:
            raise ValueError("probe delay is outside 0..2000 ms")
        with self._lock:
            self._unavailable = unavailable
            self._latency_ms = latency_ms
            self._revision += 1
            return self.snapshot()

    def apply(self) -> None:
        with self._lock:
            unavailable, delay = self._unavailable, self._latency_ms
        if delay:
            self._sleep(delay / 1000)
        if unavailable:
            raise RuntimeError("controlled probe unavailable")
