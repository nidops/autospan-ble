"""Stable logical serial-device selection.

A tty/COM path is treated as a transient endpoint, never as the preferred device
identity. Selection is deliberately independent from BleuIO protocol semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from os.path import realpath
from time import monotonic, sleep
from typing import Iterable, Protocol

from autospan_ble.bleuio.errors import AmbiguousDevice, DeviceNotFound, PortInventoryChanged
from autospan_ble.bleuio.types import PortInfo


@dataclass(frozen=True)
class SerialDeviceSelector:
    vid: int | None = None
    product_ids: tuple[int, ...] = ()
    serial_number: str | None = None
    location: str | None = None
    path: str | None = None

    def __post_init__(self) -> None:
        ids = self.product_ids if self.vid is None else (self.vid, *self.product_ids)
        if any(type(value) is not int or not 0 <= value <= 0xFFFF for value in ids):
            raise ValueError("USB VID/PID must be integers between 0x0000 and 0xFFFF")
        if not any((self.serial_number, self.location, self.path)):
            raise ValueError(
                "selector needs serial_number, location, or explicit path; VID/PID alone is not a stable identity"
            )

    def matches(self, port: PortInfo) -> bool:
        if self.path is not None and realpath(port.device) != realpath(self.path):
            return False
        if self.vid is not None and port.vid != self.vid:
            return False
        if self.product_ids and port.pid not in self.product_ids:
            return False
        if self.serial_number is not None and port.serial_number != self.serial_number:
            return False
        if self.location is not None:
            # A USB port identity can omit the interface suffix (e.g. 3-2.1).
            # Firmware may change interfaces while remaining on that physical port.
            location = port.location or ""
            if ":" not in self.location:
                location = location.partition(":")[0]
            if location != self.location:
                return False
        return True

    @property
    def claim_key(self) -> str:
        vendor = f"vid={self.vid:04x}:" if self.vid is not None else ""
        if self.serial_number:
            return f"serial:{vendor}sn={self.serial_number}"
        if self.location:
            return f"serial:{vendor}loc={self.location}"
        assert self.path is not None
        return f"serial:path={self.path}"


class PortEnumerator(Protocol):
    def ports(self) -> Iterable[PortInfo]: ...


class PySerialPortEnumerator:
    """Enumerate serial endpoints through pyserial without importing it at module import."""

    def ports(self) -> Iterable[PortInfo]:
        try:
            from serial.tools import list_ports
        except ImportError as exc:  # pragma: no cover - depends on bench environment
            raise RuntimeError("pyserial is required for real BleuIO hardware") from exc
        try:
            ports = list_ports.comports()
        except TypeError as exc:
            # pyserial 3.5's Linux SysFS reader returns None on vanished files,
            # then passes it to int(..., 16). Only that known race is retryable.
            tb = exc.__traceback__
            while tb is not None:
                if (
                    tb.tb_frame.f_globals.get("__name__") == "serial.tools.list_ports_linux"
                    and str(exc) == "int() can't convert non-string with explicit base"
                ):
                    raise PortInventoryChanged(
                        "USB metadata vanished during serial enumeration"
                    ) from exc
                tb = tb.tb_next
            raise
        for port in ports:
            yield PortInfo(
                device=port.device,
                vid=port.vid,
                pid=port.pid,
                serial_number=port.serial_number,
                location=port.location,
                manufacturer=port.manufacturer,
                product=port.product,
            )


class SerialResolver:
    def __init__(self, enumerator: PortEnumerator) -> None:
        self._enumerator = enumerator

    def candidates(self, selector: SerialDeviceSelector) -> tuple[PortInfo, ...]:
        return tuple(port for port in self._enumerator.ports() if selector.matches(port))

    def resolve(self, selector: SerialDeviceSelector) -> PortInfo:
        matches = self.candidates(selector)
        if not matches:
            raise DeviceNotFound(f"no serial device matches {selector!r}")
        if len(matches) > 1:
            names = ", ".join(port.device for port in matches)
            raise AmbiguousDevice(f"selector {selector!r} matches multiple endpoints: {names}")
        return matches[0]

    def wait(
        self, selector: SerialDeviceSelector, *, timeout_s: float, poll_s: float = 0.10
    ) -> PortInfo:
        deadline = monotonic() + timeout_s
        last_error: Exception | None = None
        while monotonic() < deadline:
            try:
                return self.resolve(selector)
            except (DeviceNotFound, PortInventoryChanged) as exc:
                last_error = exc
                sleep(min(poll_s, max(0.0, deadline - monotonic())))
        raise DeviceNotFound(
            f"device did not appear within {timeout_s:.3f}s: {selector!r}"
        ) from last_error
