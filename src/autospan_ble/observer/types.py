"""Vendor-neutral passive BLE observation records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ObserverState(str, Enum):
    CLOSED = "closed"
    READY = "ready"
    CAPTURING = "capturing"
    FAILED = "failed"


@dataclass(frozen=True)
class CaptureResult:
    """Completed capture statistics; evidence may move the path when committing."""

    path: Path
    byte_count: int
    started_monotonic_ns: int
    stopped_monotonic_ns: int
    packet_count: int = 0

    @property
    def duration_s(self) -> float:
        return max(0, self.stopped_monotonic_ns - self.started_monotonic_ns) / 1_000_000_000
