from threading import enumerate as threads
from time import monotonic

import pytest

from autospan_ble.bleuio.device import BleuIODevice
from autospan_ble.bleuio.errors import BleuIOError, CommandRejected
from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
from autospan_ble.bleuio.types import DeviceState
from autospan_ble.facade import BleBench
from autospan_ble.simulation.fake import (
    FakeBleuIOEnvironment,
    FakeLineTransport,
    FakePortEnumerator,
)


def build(env, transport_type=FakeLineTransport):
    transports = []

    def factory(endpoint):
        transport = transport_type(env, endpoint)
        transports.append(transport)
        return transport

    device = BleuIODevice(
        "logical",
        SerialDeviceSelector(serial_number="X"),
        SerialResolver(FakePortEnumerator(env)),
        factory,
        reset_timeout_s=0.5,
    )
    return device, transports


@pytest.mark.parametrize("failure", [OSError("unplugged"), ValueError("bug")])
def test_reset_only_recovers_expected_write_disconnect(failure):
    class Transport(FakeLineTransport):
        def write(self, data):
            if data.strip() == b"ATR":
                if isinstance(failure, OSError):
                    super().write(data)  # reset happened before USB flush failed
                raise failure
            super().write(data)

    env = FakeBleuIOEnvironment(endpoint="fake://old", serial_number="X")
    device, transports = build(env, Transport)
    device.open()
    try:
        if isinstance(failure, OSError):
            device.reset()
            assert device.state == DeviceState.READY
            assert device.endpoint != "fake://old"
            assert len(transports) == 2
        else:
            with pytest.raises(ValueError, match="bug"):
                device.reset()
            assert device.state == DeviceState.FAILED
            assert len(transports) == 1
        assert not transports[0]._open
    finally:
        device.close()


def test_reset_rejection_is_not_swallowed():
    class Environment(FakeBleuIOEnvironment):
        def handle(self, command):
            if command == "ATR":
                return [
                    '{"C":99,"cmd":"ATR"}',
                    '{"A":99,"err":2,"errMsg":"rejected"}',
                    '{"E":99,"nol":3}',
                ]
            return super().handle(command)

    device, transports = build(Environment(endpoint="fake://old", serial_number="X"))
    device.open()
    with pytest.raises(CommandRejected):
        device.reset()
    assert device.state == DeviceState.FAILED
    assert len(transports) == 1
    device.close()


def test_reset_requires_observed_departure_and_is_bounded():
    class Environment(FakeBleuIOEnvironment):
        def handle(self, command):
            lines = super().handle(command)
            if command == "ATR":
                self.endpoint = "fake://old"
            return lines

    device, transports = build(Environment(endpoint="fake://old", serial_number="X"))
    device.open()
    start = monotonic()
    with pytest.raises(BleuIOError, match="departure was not observed"):
        device.reset(timeout_s=0.15)
    assert monotonic() - start < 0.5
    assert len(transports) == 1
    assert all(not transport._open for transport in transports)
    assert device.state == DeviceState.FAILED
    device.close()


def test_failed_initialization_closes_partial_transport():
    class Transport(FakeLineTransport):
        def open(self):
            super().open()
            raise OSError("open partially succeeded")

    device, transports = build(
        FakeBleuIOEnvironment(endpoint="fake://one", serial_number="X"), Transport
    )
    with pytest.raises(OSError, match="partially"):
        device.open()
    assert all(not transport._open for transport in transports)
    assert device.state == DeviceState.FAILED
    assert not any(thread.name.startswith("bleuio-rx:fake://one") for thread in threads())


@pytest.mark.parametrize("phase", ["open", "initialize"])
def test_startup_preserves_original_failure_when_transport_cleanup_also_fails(phase):
    primary = ValueError("original startup failure")
    cleanup = OSError("serial cleanup failure")

    class Transport(FakeLineTransport):
        def open(self):
            super().open()
            if phase == "open":
                raise primary

        def write(self, data):
            if phase == "initialize":
                raise primary
            super().write(data)

        def close(self):
            super().close()
            raise cleanup

    device, transports = build(
        FakeBleuIOEnvironment(endpoint="fake://broken-startup", serial_number="X"), Transport
    )
    with pytest.raises(ExceptionGroup) as caught:
        device.open()
    assert caught.value.exceptions[0] is primary
    assert caught.value.exceptions[1].exceptions == (cleanup,)
    assert device.state == DeviceState.FAILED
    assert device.endpoint == "fake://broken-startup"
    assert all(not transport._open for transport in transports)
    # Keep ownership until an explicit retry confirms cleanup succeeded.
    transports[0].close = lambda: None
    device.close()
    assert device.endpoint is None


def test_two_selector_forms_cannot_open_same_physical_device():
    env = FakeBleuIOEnvironment(endpoint="fake://same", serial_number="X", location="port-1")
    opened = []

    def factory(endpoint):
        opened.append(endpoint)
        return FakeLineTransport(env, endpoint)

    devices = {
        name: BleuIODevice(name, selector, SerialResolver(FakePortEnumerator(env)), factory)
        for name, selector in {
            "serial": SerialDeviceSelector(serial_number="X"),
            "location": SerialDeviceSelector(location="port-1"),
        }.items()
    }
    with pytest.raises(ValueError, match="claimed by both"):
        BleBench(devices).open()
    assert opened == []


def test_bench_cleanup_attempts_every_owner_after_failure():
    closed = []

    class Device:
        def close(self):
            closed.append(self)
            if len(closed) == 1:
                raise OSError("cleanup failed")

    devices = {"one": Device(), "two": Device()}
    with pytest.raises(ExceptionGroup, match="cleanup failed"):
        BleBench(devices).close()
    assert closed == [devices["two"], devices["one"]]
