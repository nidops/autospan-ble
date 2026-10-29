"""Typed records used by the BleuIO adapter."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from itertools import count
from threading import Lock
from time import monotonic_ns
from typing import Any, Mapping

from autospan_ble.bleuio.address import BleAddress, BleAddressType


_event_order = count()
_event_order_lock = Lock()


def _next_event_order() -> int:
    """Order receipt and operation boundaries even within a single clock tick."""
    with _event_order_lock:
        return next(_event_order)


class DeviceState(str, Enum):
    CLOSED = "closed"
    OPENING = "opening"
    INITIALIZING = "initializing"
    READY = "ready"
    RESETTING = "resetting"
    RECOVERING = "recovering"
    FAILED = "failed"


@dataclass(frozen=True)
class PortInfo:
    device: str
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    location: str | None = None
    manufacturer: str | None = None
    product: str | None = None


@dataclass(frozen=True)
class ParsedRecord:
    raw: str
    payload: Mapping[str, Any] | None
    kind: str
    index: int | None = None


@dataclass(frozen=True)
class CommandResult:
    command: str
    index: int | None
    acknowledgement: Mapping[str, Any] | None
    replies: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class BleEvent:
    kind: str
    payload: Mapping[str, Any] | str
    command_index: int | None = None
    # Time is diagnostic; a separate order marker disambiguates equal clock ticks.
    received_monotonic_ns: int = field(default_factory=monotonic_ns, compare=False)
    _received_order: int = field(
        default_factory=_next_event_order, compare=False, repr=False, kw_only=True
    )


@dataclass(frozen=True)
class ScanResult:
    address: BleAddress
    rssi: int | None = None
    index: int | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)
    name: str | None = None
    manufacturer_id: int | None = None


class ScanResults(tuple[ScanResult, ...]):
    """Immutable scan results with a small test/console-friendly expectation helper."""

    __slots__ = ()

    def __new__(cls, rows: Iterable[ScanResult] = ()) -> ScanResults:
        return super().__new__(cls, rows)

    def expect(
        self,
        expected: str | None = None,
        /,
        *,
        address: BleAddress | str | None = None,
        name: str | None = None,
        manufacturer_id: int | None = None,
        min_rssi: int | None = None,
    ) -> ScanResult:
        """Return the first matching peer or fail with the observed scan table.

        A positional string is a convenience for ``name=...`` unless it looks
        like a Bluetooth address. Explicit keyword criteria are preferred in
        reusable tests because they read like requirements.
        """
        if expected is not None:
            if not isinstance(expected, str):
                raise ValueError("positional expectation must be a name or address string")
            if address is not None or name is not None:
                raise ValueError("positional expectation cannot be combined with address/name")
            fallback = None if expected.strip().startswith("[") else BleAddressType.PUBLIC
            try:
                BleAddress.parse(expected, fallback_type=fallback)
            except ValueError:
                name = expected
            else:
                address = expected
        if manufacturer_id is not None and (
            not isinstance(manufacturer_id, int)
            or isinstance(manufacturer_id, bool)
            or not 0 <= manufacturer_id <= 0xFFFF
        ):
            raise ValueError("manufacturer ID must be between 0x0000 and 0xFFFF")
        candidates = list(self)
        if address is not None:
            if isinstance(address, BleAddress):
                candidates = [row for row in candidates if row.address == address]
            elif isinstance(address, str):
                raw = address.strip()
                if raw.startswith("["):
                    target = BleAddress.parse(raw)
                    candidates = [row for row in candidates if row.address == target]
                else:
                    target = BleAddress.parse(raw, fallback_type=BleAddressType.PUBLIC)
                    candidates = [row for row in candidates if row.address.value == target.value]
            else:
                raise ValueError("expected address must be a BleAddress or string")
        if name is not None:
            if not isinstance(name, str):
                raise ValueError("expected name must be a string")
            candidates = [row for row in candidates if row.name == name]
        if manufacturer_id is not None:
            candidates = [row for row in candidates if row.manufacturer_id == manufacturer_id]
        if min_rssi is not None:
            if not isinstance(min_rssi, int) or isinstance(min_rssi, bool):
                raise ValueError("min_rssi must be an integer")
            candidates = [
                row for row in candidates if row.rssi is not None and row.rssi >= min_rssi
            ]
        if candidates:
            return candidates[0]
        expected_bits: list[str] = []
        if address is not None:
            expected_bits.append(f"address={address}")
        if name is not None:
            expected_bits.append(f"name={name!r}")
        if manufacturer_id is not None:
            expected_bits.append(f"manufacturer_id=0x{manufacturer_id:04X}")
        if min_rssi is not None:
            expected_bits.append(f"rssi>={min_rssi}")
        expectation = ", ".join(expected_bits) if expected_bits else "at least one peer"
        observed = (
            "\n".join(
                f"  {row.address.bleuio} rssi={row.rssi} name={row.name!r} "
                f"manufacturer_id={_hex_or_none(row.manufacturer_id)}"
                for row in self
            )
            or "  <none>"
        )
        raise AssertionError(f"expected BLE peer ({expectation}); observed:\n{observed}")


@dataclass(frozen=True)
class GattValue:
    """One GATT read with raw bytes and optional decoded fields."""

    handle: int
    raw: bytes
    decoded: Mapping[str, Any] | None = None
    codec_id: str | None = None

    def expect(
        self, expected: bytes | bytearray | memoryview | None = None, /, **fields: Any
    ) -> "GattValue":
        """Assert a raw value or decoded fields and return this value for chaining."""
        if expected is not None and fields:
            raise ValueError("use either a raw expected value or decoded fields, not both")
        if expected is not None:
            if not isinstance(expected, (bytes, bytearray, memoryview)):
                raise ValueError("raw expected value must be bytes, bytearray or memoryview")
            wanted = bytes(expected)
            if self.raw != wanted:
                raise AssertionError(
                    f"GATT 0x{self.handle:04X} expected {wanted!r}, got {self.raw!r}"
                )
            return self
        if fields:
            if self.decoded is None:
                raise AssertionError(
                    f"GATT 0x{self.handle:04X} has no decoded fields; raw={self.raw!r}"
                )
            mismatches = {
                key: (value, self.decoded.get(key), key in self.decoded)
                for key, value in fields.items()
                if key not in self.decoded or self.decoded[key] != value
            }
            if mismatches:
                details = ", ".join(
                    f"{key}: expected {wanted!r}, got {actual!r}"
                    if present
                    else f"{key}: expected {wanted!r}, field missing"
                    for key, (wanted, actual, present) in mismatches.items()
                )
                raise AssertionError(f"GATT 0x{self.handle:04X} mismatch: {details}")
            return self
        if self.decoded is None and not self.raw:
            raise AssertionError(f"GATT 0x{self.handle:04X} expected a value, got empty bytes")
        return self


def _hex_or_none(value: int | None) -> str:
    return "None" if value is None else f"0x{value:04X}"
