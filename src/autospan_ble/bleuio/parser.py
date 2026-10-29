"""Parser for BleuIO verbose-mode records."""

from __future__ import annotations

import json
from typing import Any

from autospan_ble.bleuio.types import ParsedRecord

_COMMAND_KEYS = ("C", "A", "R", "E")
_SCAN_KEYS = ("S", "SF", "ST", "SE")


def _index(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_line(line: str) -> ParsedRecord:
    raw = line.strip()
    if not raw:
        return ParsedRecord(raw=line, payload=None, kind="empty")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ParsedRecord(raw=raw, payload=None, kind="text")
    if not isinstance(payload, dict):
        return ParsedRecord(raw=raw, payload={"value": payload}, kind="json")
    for key in _COMMAND_KEYS:
        if key in payload:
            return ParsedRecord(raw=raw, payload=payload, kind=key, index=_index(payload[key]))
    for key in _SCAN_KEYS:
        if key in payload:
            return ParsedRecord(raw=raw, payload=payload, kind=key, index=_index(payload[key]))
    return ParsedRecord(raw=raw, payload=payload, kind="event")
