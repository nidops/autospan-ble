"""Line transport used by the BleuIO command engine."""

from __future__ import annotations

import os
from math import isfinite
from threading import Lock
from typing import TYPE_CHECKING, Protocol

from autospan_ble.bleuio.errors import BleuIOError, DeviceDisconnected

DEFAULT_MAX_RX_LINE_BYTES = 64 * 1024
MAX_RX_LINE_BYTES = 1024 * 1024

if TYPE_CHECKING:
    from serial import Serial


class LineTransport(Protocol):
    @property
    def endpoint(self) -> str: ...
    def open(self) -> None: ...
    def close(self) -> None: ...
    def write(self, data: bytes) -> None: ...
    def readline(self, timeout_s: float) -> str | None: ...


class PySerialLineTransport:
    def __init__(
        self,
        endpoint: str,
        *,
        baud: int = 57600,
        max_rx_line_bytes: int = DEFAULT_MAX_RX_LINE_BYTES,
    ) -> None:
        if type(baud) is not int or baud <= 0:
            raise ValueError("baud must be a positive integer")
        if type(max_rx_line_bytes) is not int or not 1 <= max_rx_line_bytes <= MAX_RX_LINE_BYTES:
            raise ValueError(
                f"max_rx_line_bytes must be an integer between 1 and {MAX_RX_LINE_BYTES}"
            )
        self._endpoint = endpoint
        self._baud = baud
        self._max_rx_line_bytes = max_rx_line_bytes
        self._serial: Serial | None = None
        self._state_lock = Lock()
        self._tx_lock = Lock()
        self._rx_lock = Lock()
        self._close_failed = False
        self._rx_buffer = bytearray()
        self._rx_failed = False

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def open(self) -> None:
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - bench dependency
            raise RuntimeError("pyserial is required for real BleuIO hardware") from exc
        with self._state_lock, self._tx_lock, self._rx_lock:
            if self._close_failed:
                raise RuntimeError("serial transport cleanup failed; close before reopening")
            if self._serial is not None:
                return
            self._rx_buffer.clear()
            self._rx_failed = False
            self._serial = serial.Serial(
                self._endpoint,
                baudrate=self._baud,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=0.1,
                write_timeout=1.0,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
                **({"exclusive": True} if os.name == "posix" else {}),
            )

    def close(self) -> None:
        # Stop new I/O before waiting for the bounded operations already in flight.
        # The RX worker never needs the state lock to finish a read.
        with self._state_lock, self._tx_lock, self._rx_lock:
            if self._serial is not None:
                self._close_failed = True
                self._serial.close()
                self._serial = None
            self._close_failed = False
            self._rx_buffer.clear()
            self._rx_failed = False

    def write(self, data: bytes) -> None:
        with self._state_lock:
            self._tx_lock.acquire()
        try:
            serial_obj = self._serial
            if serial_obj is None or self._close_failed:
                raise DeviceDisconnected("serial transport is not open")
            if serial_obj.write(data) != len(data):
                raise OSError("incomplete serial write")
            # Serial.write is unbuffered and has a finite write_timeout. flush()
            # invokes unbounded tcdrain on POSIX; a reply confirms transmission.
        finally:
            self._tx_lock.release()

    def readline(self, timeout_s: float) -> str | None:
        if not isfinite(timeout_s):
            raise ValueError("serial read timeout must be finite")
        with self._state_lock:
            self._rx_lock.acquire()
        try:
            serial_obj = self._serial
            if serial_obj is None or self._close_failed:
                raise RuntimeError("serial transport is not open")
            if self._rx_failed:
                raise BleuIOError("serial receive framing failed; reopen the transport")
            serial_obj.timeout = min(0.1, max(0.0, timeout_s))
            # Unlike IOBase.readline(), pyserial's read_until has a total read
            # deadline even when bytes keep arriving without a newline.
            # Read at most one byte beyond the remaining allowance. A single
            # unterminated vendor frame can never allocate an unbounded buffer.
            remaining = self._max_rx_line_bytes - len(self._rx_buffer)
            data = serial_obj.read_until(size=remaining + 1)
            if not data:
                return None
            if len(self._rx_buffer) + len(data) > self._max_rx_line_bytes:
                self._rx_failed = True
                self._rx_buffer.clear()
                raise BleuIOError(
                    f"serial frame exceeds max_rx_line_bytes={self._max_rx_line_bytes}; "
                    "reopen before continuing"
                )
            self._rx_buffer.extend(data)
            if not self._rx_buffer.endswith(b"\n"):
                return None
            line = self._rx_buffer.decode("utf-8", errors="replace").rstrip("\r\n")
            self._rx_buffer.clear()
            return line
        finally:
            self._rx_lock.release()
