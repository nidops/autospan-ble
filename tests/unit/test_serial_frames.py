"""Bounded BLE framing and command bootstrap regressions."""

import json
from collections import deque
from itertools import count
from threading import Event

import pytest

from autospan_ble.bleuio.client import BleuIOClient
from autospan_ble.bleuio.errors import BleuIOError, CommandTimeout
from autospan_ble.bleuio.transport import (
    DEFAULT_MAX_RX_LINE_BYTES,
    MAX_RX_LINE_BYTES,
    PySerialLineTransport,
)
from autospan_ble.config import parse_config


class FragmentedSerial:
    def __init__(self, chunks):
        self.chunks = deque(chunks)
        self.timeout = 0.1
        self.closed = False

    def read_until(self, *, size):
        if not self.chunks:
            return b""
        chunk = self.chunks.popleft()
        newline = chunk.find(b"\n", 0, size)
        end = newline + 1 if newline >= 0 else size
        data, leftover = chunk[:end], chunk[end:]
        if leftover:
            self.chunks.appendleft(leftover)
        return data

    def close(self):
        self.closed = True


def transport(chunks, *, limit=8):
    result = PySerialLineTransport("synthetic", max_rx_line_bytes=limit)
    result._serial = FragmentedSerial(chunks)
    return result


def test_fragmented_frame_at_exact_limit_is_accepted():
    port = transport([b"123", b"456", b"7\n"])
    try:
        assert port.readline(0.1) is None
        assert port.readline(0.1) is None
        assert port.readline(0.1) == "1234567"
    finally:
        port.close()


def test_coalesced_frames_are_bounded_individually():
    port = transport([b"ab\ncd\n"], limit=3)
    try:
        assert port.readline(0.1) == "ab"
        assert port.readline(0.1) == "cd"
    finally:
        port.close()


@pytest.mark.parametrize(
    "chunks",
    [
        [b"123456789\n"],
        [b"12345678", b"9\n"],
        [b"123", b"456", b"789"],
    ],
)
def test_oversized_frame_fails_closed(chunks):
    port = transport(chunks)
    try:
        with pytest.raises(BleuIOError, match="max_rx_line_bytes"):
            for _ in range(len(chunks)):
                port.readline(0.1)
        with pytest.raises(BleuIOError, match="reopen"):
            port.readline(0.1)
    finally:
        port.close()


@pytest.mark.parametrize("value", [0, -1, True, "1024", 1.5, MAX_RX_LINE_BYTES + 1])
def test_invalid_transport_limit_is_rejected(value):
    with pytest.raises(ValueError, match="max_rx_line_bytes"):
        PySerialLineTransport("synthetic", max_rx_line_bytes=value)


def test_configuration_default_and_override():
    def config(**fields):
        return {
            "devices": {
                "scanner": {
                    "backend": "pyserial",
                    "selector": {"serial_number": "SERIAL-1"},
                    **fields,
                }
            }
        }

    assert parse_config(config()).devices[0].max_rx_line_bytes == DEFAULT_MAX_RX_LINE_BYTES
    assert parse_config(config(max_rx_line_bytes=4096)).devices[0].max_rx_line_bytes == 4096
    with pytest.raises(ValueError, match="max_rx_line_bytes"):
        parse_config(config(max_rx_line_bytes="invalid"))


class BootstrapTransport:
    endpoint = "synthetic"

    def __init__(self, lines):
        self.lines = iter(lines)

    def write(self, data):
        assert data.endswith(b"\r\n")

    def readline(self, timeout_s):
        return next(self.lines, None)


@pytest.fixture
def bootstrap_clock(monkeypatch):
    import autospan_ble.bleuio.client as module

    ticks = count()
    monkeypatch.setattr(module, "monotonic", lambda: next(ticks) / 1000)


@pytest.mark.parametrize("ending", [[], [json.dumps({"E": 8})]])
def test_bootstrap_rejects_ack_without_matching_end(bootstrap_clock, ending):
    client = BleuIOClient(
        BootstrapTransport(
            [json.dumps({"C": 7, "cmd": "AT"}), json.dumps({"A": 7, "err": 0}), *ending]
        )
    )
    with pytest.raises(CommandTimeout, match="did not complete"):
        client._bootstrap("AT", timeout_s=0.1)


def test_bootstrap_accepts_complete_matching_frame(bootstrap_clock):
    client = BleuIOClient(
        BootstrapTransport(
            [
                json.dumps({"C": 7, "cmd": "AT"}),
                json.dumps({"A": 7, "err": 0}),
                json.dumps({"E": 7, "nol": 2}),
            ]
        )
    )
    client._bootstrap("AT", timeout_s=0.1)


@pytest.mark.parametrize(
    ("command", "reply"),
    [("AT", "OK"), ("ATE0", "ECHO OFF"), ("ATV1", "VERBOSE ON")],
)
def test_legacy_bootstrap_responses_remain_supported(bootstrap_clock, command, reply):
    BleuIOClient(BootstrapTransport([reply]))._bootstrap(command, timeout_s=0.1)


class SingleEventTransport:
    endpoint = "synthetic"

    def __init__(self):
        self.sent = False
        self.closed = Event()

    def readline(self, timeout_s):
        if not self.sent:
            self.sent = True
            return json.dumps({"258": "0000", "evt": {"action": "disconnected"}})
        self.closed.wait(timeout_s)
        return None

    def close(self):
        self.closed.set()


def test_event_order_precedes_slow_evidence_callback_with_equal_timestamps(monkeypatch):
    from autospan_ble.bleuio.types import _next_event_order

    monkeypatch.setattr("autospan_ble.bleuio.client.monotonic_ns", lambda: 123)
    entered = Event()
    release = Event()

    def evidence(kind, fields):
        if kind == "rx":
            entered.set()
            assert release.wait(2)

    client = BleuIOClient(SingleEventTransport(), evidence=evidence)
    client.start_reader()
    try:
        assert entered.wait(1)
        cutoff = _next_event_order()
        release.set()
        event = client.next_event(timeout_s=1)
        assert event.received_monotonic_ns == 123
        assert event._received_order < cutoff
    finally:
        release.set()
        client.close()
