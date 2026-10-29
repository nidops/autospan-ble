"""Failure paths found during the local release audit; no physical hardware."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, current_thread

import pytest

from autospan_ble.bleuio.client import BleuIOClient
from autospan_ble.bleuio.errors import DeviceDisconnected
from autospan_ble.bleuio.transport import PySerialLineTransport


class ObservedLock:
    def __init__(self, waiting):
        self.lock = Lock()
        self.waiting = waiting

    def acquire(self):
        if current_thread().name.startswith("closer"):
            self.waiting.set()
        return self.lock.acquire()

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


@pytest.mark.parametrize("operation", ["write", "read"])
def test_close_waits_for_active_io_without_closing_its_descriptor(operation):
    entered, release, close_reached = Event(), Event(), Event()

    class Serial:
        closed = False

        def io(self):
            entered.set()
            assert release.wait(3)
            assert not self.closed, "descriptor closed during active I/O"

        def write(self, data):
            self.io()
            return len(data)

        def flush(self):
            pass

        def readline(self):
            self.io()
            return b"reply\n"

        def read_until(self, *, size=None):
            return self.readline()

        def close(self):
            self.closed = True
            close_reached.set()

    transport = PySerialLineTransport("unused")
    serial = Serial()
    transport._serial = serial
    setattr(
        transport, "_tx_lock" if operation == "write" else "_rx_lock", ObservedLock(close_reached)
    )
    with ThreadPoolExecutor(1) as io, ThreadPoolExecutor(1, thread_name_prefix="closer") as closer:
        pending = (
            io.submit(transport.write, b"AT\r\n")
            if operation == "write"
            else io.submit(transport.readline, 0.1)
        )
        try:
            assert entered.wait(2)
            closing = closer.submit(transport.close)
            assert close_reached.wait(2)
            assert not serial.closed
        finally:
            release.set()
        pending.result(2)
        closing.result(2)
    assert serial.closed
    with pytest.raises(RuntimeError, match="not open"):
        transport.write(b"AT\r\n")


def test_write_does_not_use_unbounded_serial_flush():
    class Serial:
        def write(self, data):
            return len(data)

        def flush(self):
            pytest.fail("POSIX tcdrain has no write_timeout")

    transport = PySerialLineTransport("unused")
    transport._serial = Serial()
    transport.write(b"AT\r\n")


def test_reader_failure_notifies_command_even_when_recording_failure_also_raises():
    written, fail_read = Event(), Event()
    primary, recording = OSError("USB disconnected"), OSError("evidence disk full")

    class Transport:
        endpoint = "fake://failure"

        def write(self, data):
            written.set()

        def readline(self, timeout_s):
            assert fail_read.wait(3)
            raise primary

        def close(self):
            pass

    def evidence(kind, fields):
        if kind == "rx_failed":
            raise recording

    client = BleuIOClient(Transport(), evidence=evidence)
    client.start_reader()
    reader = client._reader
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(client.command, "ATI", timeout_s=2)
            assert written.wait(1)
            fail_read.set()
            reader.join(1)
            assert not reader.is_alive()
            with pytest.raises(DeviceDisconnected) as caught:
                pending.result(1)
            assert caught.value.__cause__.exceptions == (primary, recording)
    finally:
        fail_read.set()
        client.close()


def test_device_retains_failed_cleanup_owner_and_retries_before_reopening():
    from autospan_ble.bleuio.types import DeviceState
    from autospan_ble.simulation.fake import FakeBleuIOEnvironment, FakeLineTransport

    allow_close = Event()

    class Transport(FakeLineTransport):
        def close(self):
            if not allow_close.is_set():
                raise OSError("descriptor still owned")
            super().close()

    device, transports = _build(
        FakeBleuIOEnvironment(endpoint="fake://retry", serial_number="X"), Transport
    )
    device.open()
    try:
        with pytest.raises(BaseExceptionGroup):
            device.close()
        assert device.state == DeviceState.FAILED
        assert device._client is not None
        with pytest.raises(BaseExceptionGroup):
            device.open()
        assert len(transports) == 1
    finally:
        allow_close.set()
        device.close()
    assert not transports[0]._open
    device.open()
    device.close()
    assert len(transports) == 2


def test_open_cleanup_still_runs_when_failed_state_evidence_raises():
    from autospan_ble.bleuio.types import DeviceState
    from autospan_ble.simulation.fake import FakeBleuIOEnvironment

    device, transports = _build(
        FakeBleuIOEnvironment(endpoint="fake://evidence", serial_number="X")
    )

    def evidence(kind, fields):
        if kind == "state" and fields["state"] in {"ready", "failed"}:
            raise OSError("evidence failed")

    device._evidence = evidence
    with pytest.raises(BaseException):
        device.open()
    assert device.state == DeviceState.FAILED
    assert all(not transport._open for transport in transports)
    assert device._client is None


def _build(env, transport_type=None):
    from autospan_ble.bleuio.device import BleuIODevice
    from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
    from autospan_ble.simulation.fake import FakePortEnumerator, FakeLineTransport

    transports = []

    def create(endpoint):
        transport = (transport_type or FakeLineTransport)(env, endpoint)
        transports.append(transport)
        return transport

    return BleuIODevice(
        "logical",
        SerialDeviceSelector(serial_number="X"),
        SerialResolver(FakePortEnumerator(env)),
        create,
    ), transports


@pytest.mark.parametrize("operation", ["advertising", "connected"])
def test_completed_command_evidence_failure_does_not_lose_radio_ownership(operation):
    from autospan_ble.simulation.fake import FakeBleuIOEnvironment

    env = FakeBleuIOEnvironment(endpoint="fake://radio", serial_number="X")
    device, _ = _build(env)
    device.open()
    command = "AT+ADVSTART" if operation == "advertising" else "AT+GAPCONNECT="

    def evidence(kind, fields):
        if kind == "serial_command_completed" and fields.get("command", "").startswith(command):
            raise OSError("cannot record completed command")

    device._evidence = evidence
    try:
        scope = (
            device.advertising() if operation == "advertising" else device.connected(env.address)
        )
        with pytest.raises(OSError, match="cannot record"):
            with scope:
                pytest.fail("entry should have failed")
        assert not env.advertising
        assert not env.connected
    finally:
        device._evidence = None
        device.close()
