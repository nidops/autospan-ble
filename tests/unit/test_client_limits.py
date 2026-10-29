"""Bounded event retention and shutdown regressions; no physical hardware."""

from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from threading import Event

import pytest

import autospan_ble.bleuio.client as client_module
from autospan_ble.bleuio.client import BleuIOClient
from autospan_ble.bleuio.device import BleuIODevice
from autospan_ble.bleuio.errors import BleuIOError, CommandTimeout, DeviceDisconnected
from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
from autospan_ble.bleuio.transport import PySerialLineTransport
from autospan_ble.bleuio.types import BleEvent
from autospan_ble.simulation.fake import (
    FakeBleuIOEnvironment,
    FakeLineTransport,
    FakePortEnumerator,
)


class InputTransport:
    endpoint = "fake://event-limit"

    def __init__(self):
        self.lines = Queue()
        self.written = Event()

    def write(self, data):
        self.written.set()

    def readline(self, timeout_s):
        try:
            return self.lines.get(timeout=timeout_s)
        except Empty:
            return None

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(client_module, "_MAX_EVENTS", 4)
    value = BleuIOClient(InputTransport())
    yield value
    value.close()


def test_fifo_requeue_and_matching_release_capacity(client):
    events = [BleEvent("text", str(index)) for index in range(4)]
    client.requeue_events(events)
    assert client.wait_event(lambda event: event is events[2], timeout_s=1) is events[2]
    # Preserve the documented tail restoration of unmatched records.
    expected = [events[3], events[0], events[1]]
    assert [client.next_event(timeout_s=1) for _ in expected] == expected
    client.requeue_events(events)
    assert [client.next_event(timeout_s=1) for _ in events] == events


def test_deferred_events_still_count_toward_limit(client):
    events = [BleEvent("text", str(index)) for index in range(4)]
    client.requeue_events(events)

    def predicate(event):
        client.requeue_events([BleEvent("text", "overflow")])
        return False

    with pytest.raises(DeviceDisconnected, match="overflow"):
        client.wait_event(predicate, timeout_s=1)
    assert client._events.qsize() == 4
    with pytest.raises(DeviceDisconnected):
        client.next_event(timeout_s=1)
    with pytest.raises(DeviceDisconnected):
        client.command("ATI")


def test_predicate_failure_retains_the_current_event(client):
    events = [BleEvent("text", str(index)) for index in range(4)]
    client.requeue_events(events)
    failure = ValueError("predicate failed")

    def predicate(event):
        raise failure

    with pytest.raises(ValueError) as caught:
        client.wait_event(predicate, timeout_s=1)
    assert caught.value is failure
    expected = events[1:] + events[:1]
    assert [client.next_event(timeout_s=1) for _ in expected] == expected


def test_timeout_restores_events_without_reacquiring_capacity(client, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(client_module, "monotonic", lambda: clock[0])
    event = BleEvent("text", "deferred")
    client.requeue_events([event])

    def predicate(record):
        clock[0] = 2.0
        return False

    with pytest.raises(CommandTimeout):
        client.wait_event(predicate, timeout_s=1)
    assert client.next_event(timeout_s=1) is event


@pytest.mark.parametrize("failure", ["overflow", "close"])
def test_failure_during_dequeue_does_not_return_an_event(client, monkeypatch, failure):
    events = [BleEvent("text", str(index)) for index in range(4)]
    client.requeue_events(events)
    original_get = client._events.get

    def get(*, timeout):
        event = original_get(timeout=timeout)
        if failure == "overflow":
            with pytest.raises(DeviceDisconnected, match="overflow"):
                client.requeue_events([BleEvent("text", "overflow")])
        else:
            client.close()
        return event

    monkeypatch.setattr(client._events, "get", get)
    with pytest.raises(DeviceDisconnected):
        client.next_event(timeout_s=1)
    assert client._events.qsize() == len(events)


def test_failure_at_event_deadline_does_not_become_timeout(client, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(client_module, "monotonic", lambda: clock[0])

    def get(*, timeout):
        client.close()
        clock[0] = 2.0
        raise Empty

    monkeypatch.setattr(client._events, "get", get)
    with pytest.raises(DeviceDisconnected):
        client.next_event(timeout_s=1)


@pytest.mark.parametrize("failure", ["timeout", "overflow", "close"])
@pytest.mark.parametrize("restart", ["start_reader", "initialize"])
def test_failed_session_cannot_restart_and_accept_stale_replies(client, failure, restart):
    client.start_reader()
    reader = client._reader
    if failure == "timeout":
        with pytest.raises(CommandTimeout):
            client.command("ATI", timeout_s=0.01)
    elif failure == "overflow":
        for index in range(5):
            client._transport.lines.put(f"unsolicited {index}")
    else:
        client.close()
    reader.join(1)
    assert not reader.is_alive()
    original_failure = client.failure

    # A late frame from the previous session must never complete a new ATI.
    stale_lines = [
        '{"C":1,"cmd":"ATI"}',
        '{"A":1,"err":0}',
        '{"R":1,"fw":"stale"}',
        '{"E":1}',
    ]
    for line in stale_lines:
        client._transport.lines.put(line)
    client._transport.written.clear()
    with pytest.raises(DeviceDisconnected):
        getattr(client, restart)()
    with pytest.raises(DeviceDisconnected):
        client.command("ATI", timeout_s=0.5)
    assert client.failure is original_failure
    assert not client._transport.written.is_set()
    assert client._transport.lines.qsize() == len(stale_lines)


def test_start_reader_is_idempotent_for_healthy_session(client):
    client.start_reader()
    reader = client._reader
    client.start_reader()
    assert client._reader is reader
    assert reader.is_alive()


@pytest.mark.parametrize("recording_fails", [False, True])
def test_rx_overflow_notifies_command_and_waiter(client, monkeypatch, recording_fails):
    waiting, release = Event(), Event()
    recording = OSError("evidence full")

    def evidence(kind, fields):
        if kind == "rx_failed" and recording_fails:
            raise recording

    def blocked_get(*, timeout):
        waiting.set()
        assert release.wait(3)
        raise Empty

    client._evidence = evidence
    monkeypatch.setattr(client._events, "get", blocked_get)
    client.start_reader()
    reader = client._reader
    with ThreadPoolExecutor(2) as pool:
        command = pool.submit(client.command, "ATI", timeout_s=3)
        waiter = pool.submit(client.next_event, timeout_s=3)
        try:
            assert client._transport.written.wait(1)
            assert waiting.wait(1)
            for index in range(5):
                client._transport.lines.put(f"unsolicited {index}")
            reader.join(1)
            assert not reader.is_alive(), "overflow blocked the RX worker"
        finally:
            release.set()
        for future in (command, waiter):
            with pytest.raises(DeviceDisconnected) as caught:
                future.result(1)
            cause = caught.value.__cause__
            if recording_fails:
                assert isinstance(cause, BaseExceptionGroup)
                assert cause.exceptions[1] is recording
                cause = cause.exceptions[0]
            assert isinstance(cause, BleuIOError)
            assert "overflow" in str(cause)
    assert client._events.qsize() == 4


@pytest.mark.parametrize("operation", ["command", "stop_scan"])
def test_close_between_session_check_and_write(operation):
    entered, release, closed = Event(), Event(), Event()

    class Serial:
        def write(self, data):
            pytest.fail("closed descriptor must not be written")

        def close(self):
            closed.set()

    def evidence(kind, fields):
        if kind == "tx":
            entered.set()
            assert release.wait(3)

    transport = PySerialLineTransport("unused")
    transport._serial = Serial()
    value = BleuIOClient(transport, evidence=evidence)
    with ThreadPoolExecutor(1) as pool:
        action = value.command if operation == "command" else value.stop_scan
        future = pool.submit(action, "ATI" if operation == "command" else 1)
        try:
            assert entered.wait(1)
            value.close()
            assert closed.is_set()
        finally:
            release.set()
        with pytest.raises(DeviceDisconnected):
            future.result(1)


@pytest.mark.parametrize("source", ["transport", "evidence"])
@pytest.mark.parametrize("failure", [OSError("I/O failure"), RuntimeError("programming error")])
def test_write_failure_causes_and_evidence_errors_are_preserved(source, failure):
    class Serial:
        def write(self, data):
            assert source == "transport", "evidence failure must prevent transmission"
            raise failure

        def close(self):
            pass

    def evidence(kind, fields):
        if source == "evidence" and kind == "tx":
            raise failure

    transport = PySerialLineTransport("unused")
    transport._serial = Serial()
    value = BleuIOClient(transport, evidence=evidence)
    expected = (
        DeviceDisconnected
        if source == "transport" and isinstance(failure, OSError)
        else type(failure)
    )
    try:
        with pytest.raises(expected) as caught:
            value.command("ATI")
        if expected is DeviceDisconnected:
            assert caught.value.__cause__ is failure
        else:
            assert caught.value is failure
    finally:
        value.close()


def test_normal_scan_burst_preserves_unrelated_events():
    env = FakeBleuIOEnvironment(
        endpoint="fake://burst",
        serial_number="BURST",
        scan_results=tuple(
            {"addr": "[0]12:34:56:78:9A:BC", "rssi": -index} for index in range(128)
        ),
    )
    device = BleuIODevice(
        "burst",
        SerialDeviceSelector(serial_number="BURST"),
        SerialResolver(FakePortEnumerator(env)),
        lambda endpoint: FakeLineTransport(env, endpoint),
    )
    device.open()
    try:
        events = [BleEvent("text", "first"), BleEvent("text", "second")]
        device._client.requeue_events(events)
        rows = device.scan(seconds=1)
        assert [row.rssi for row in rows] == [-index for index in range(128)]
        assert [device._client.next_event(timeout_s=1) for _ in events] == events
    finally:
        device.close()
