import json

import pytest

from autospan_ble.bleuio.errors import BleuIOError, CommandRejected
from autospan_ble.bleuio.errors import CommandTimeout
from autospan_ble.bleuio.device import BleuIODevice
from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
from autospan_ble.simulation.fake import (
    FakeBleuIOEnvironment,
    FakeLineTransport,
    FakePortEnumerator,
)


def device_for(env):
    return BleuIODevice(
        "logical",
        SerialDeviceSelector(serial_number="X"),
        SerialResolver(FakePortEnumerator(env)),
        lambda endpoint: FakeLineTransport(env, endpoint),
    )


def test_observed_gatt_read_returns_bytes_after_async_hex_response():
    env = FakeBleuIOEnvironment(endpoint="fake://gatt", serial_number="X")
    device = device_for(env)
    device.open()
    try:
        device.connect("12:34:56:78:9A:BC")
        assert device.read_gatt(3) == b"BleuIO"
        assert device.read_gatt(3) == b"BleuIO"  # deferred ASCII does not satisfy next read
        device.disconnect()
        assert not env.connected
    finally:
        device.close()


def test_connect_waits_for_target_before_gatt_read_can_run():
    class Environment(FakeBleuIOEnvironment):
        target_ready = False

        def handle(self, command):
            lines = super().handle(command)
            if command.startswith("AT+GAPCONNECT="):
                # Real firmware can emit "connected" before selecting its GATT target.
                self.target_ready = False
            if command == "AT+TARGETCONN" and not self.target_ready:
                lines = [line.replace('"target":"0000"', '"target":"FFFF"') for line in lines]
                self.target_ready = True
            if command == "AT+GATTCREAD=0003" and not self.target_ready:
                lines = [line for line in lines if '"775"' not in line]
                lines = [line.replace('"err":0', '"err":6') for line in lines]
            return lines

    env = Environment(endpoint="fake://delayed-target", serial_number="X")
    device = device_for(env)
    device.open()
    try:
        device.connect("12:34:56:78:9A:BC")
        assert env.target_ready
        assert device.read_gatt(3) == b"BleuIO"
    finally:
        device.close()


@pytest.mark.parametrize("handle_keyed", [False, True])
def test_read_failure_is_reported_even_without_a_hex_value(handle_keyed):
    class Environment(FakeBleuIOEnvironment):
        def handle(self, command):
            lines = super().handle(command)
            if command == "AT+GATTCREAD=0003":
                lines = [line for line in lines if '"775"' not in line]
                details = {"handle": "0003", "readStatus": 5}
                lines.append(
                    json.dumps({"775": "0000", "0003" if handle_keyed else "evt": details})
                )
            return lines

    env = Environment(endpoint="fake://gatt", serial_number="X")
    device = device_for(env)
    device.open()
    try:
        device.connect("12:34:56:78:9A:BC")
        with pytest.raises(BleuIOError, match="read 0003 failed: 5"):
            device.read_gatt(3)
    finally:
        device.close()
    assert not env.connected


def test_handle_keyed_gatt_value_is_supported():
    class Environment(FakeBleuIOEnvironment):
        def handle(self, command):
            lines = super().handle(command)
            if command == "AT+GATTCREAD=0003":
                lines = [line for line in lines if '"775"' not in line]
                lines.append(
                    '{"775":"0000","0003":{"readStatus":0,"hex":"0x426C6575494F","len":6}}'
                )
            return lines

    env = Environment(endpoint="fake://handle-keyed", serial_number="X")
    device = device_for(env)
    device.open()
    try:
        device.connect("12:34:56:78:9A:BC")
        assert device.read_gatt(3) == b"BleuIO"
    finally:
        device.close()


def test_read_rejects_mismatched_length_and_command_errors():
    class Environment(FakeBleuIOEnvironment):
        def handle(self, command):
            return [line.replace('"len": 6', '"len": 8') for line in super().handle(command)]

    env = Environment(endpoint="fake://length", serial_number="X")
    device = device_for(env)
    device.open()
    try:
        with pytest.raises(CommandRejected) as error:
            device.read_gatt(3)
        assert error.value.error_code == 6
        device.connect("12:34:56:78:9A:BC")
        with pytest.raises(BleuIOError, match="length mismatch"):
            device.read_gatt(3)
    finally:
        device.close()


@pytest.mark.parametrize("failure", [CommandTimeout, KeyboardInterrupt, OSError])
def test_incomplete_read_prevents_late_value_from_satisfying_next_read(monkeypatch, failure):
    class Environment(FakeBleuIOEnvironment):
        def handle(self, command):
            lines = super().handle(command)
            if command == "AT+GATTCREAD=0003":
                return [line for line in lines if '"775"' not in line]
            return lines

    env = Environment(endpoint="fake://late", serial_number="X")
    device = device_for(env)
    device.open()
    try:
        device.connect("12:34:56:78:9A:BC")

        def interrupted_wait(*args, **kwargs):
            raise KeyboardInterrupt

        def failed_evidence(kind, fields):
            if kind == "serial_command_completed" and fields.get("command") == "AT+GATTCREAD=0003":
                raise OSError("cannot record completed read command")

        with monkeypatch.context() as patch:
            if failure is KeyboardInterrupt:
                patch.setattr(device, "wait_event", interrupted_wait)
            elif failure is OSError:
                patch.setattr(device, "_evidence", failed_evidence)
            with pytest.raises(failure):
                device.read_gatt(3, timeout_s=0.02)
        device._transport._rx.put(
            '{"775":"0000","evt":{"handle":"0003","readStatus":0,"hex":"0x426C6575494F","len":6}}'
        )
        with pytest.raises(BleuIOError, match="reopen before another read"):
            device.read_gatt(3)
        device.disconnect()
        assert not env.connected
    finally:
        device.close()


@pytest.mark.parametrize("operation", ["connect", "disconnect", "stop_advertising", "read_gatt"])
@pytest.mark.parametrize("same_clock_tick", [False, True])
def test_previous_async_event_cannot_complete_new_operation(
    operation, same_clock_tick, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
    from threading import Event

    from autospan_ble.bleuio.types import BleEvent

    if same_clock_tick:
        # Windows Python 3.11 can timestamp both receipt and request in one tick.
        monkeypatch.setattr("autospan_ble.bleuio.client.monotonic_ns", lambda: 123)
        monkeypatch.setattr("autospan_ble.bleuio.device.monotonic_ns", lambda: 123, raising=False)

    command = {
        "connect": "AT+GAPCONNECT=[0]12:34:56:78:9A:BC",
        "disconnect": "AT+GAPDISCONNECT",
        "stop_advertising": "AT+ADVSTOP",
        "read_gatt": "AT+GATTCREAD=0003",
    }[operation]
    payload = {
        "connect": {"256": "0000", "evt": {"action": "connected", "addr": "12:34:56:78:9a:bc"}},
        "disconnect": {"258": "0000", "evt": {"action": "disconnected"}},
        "stop_advertising": {"260": "FFFF", "evt": {"action": "advertising stopped"}},
        "read_gatt": {
            "775": "0000",
            "evt": {"handle": "0003", "readStatus": 0, "hex": "0x426c6575494f", "len": 6},
        },
    }[operation]

    class Environment(FakeBleuIOEnvironment):
        def handle(self, current):
            lines = super().handle(current)
            if current == command:
                return [line for line in lines if str(next(iter(payload))) not in json.loads(line)]
            return lines

    env = Environment(endpoint="fake://stale-completion", serial_number="X")
    device = device_for(env)
    device.open()
    env.connected = operation in {"disconnect", "read_gatt"}
    env.advertising = operation == "stop_advertising"
    # This is the actual passive-event shape left queued when role directions reverse.
    stale = (
        BleEvent("event", payload, received_monotonic_ns=123)
        if same_clock_tick
        else BleEvent("event", payload)
    )
    device._client.requeue_events([stale])
    waiting = Event()
    original_wait = device.wait_event

    def wait(predicate, *, timeout_s):
        waiting.set()
        return original_wait(predicate, timeout_s=timeout_s)

    device.wait_event = wait
    action = {
        "connect": lambda: device.connect("12:34:56:78:9A:BC"),
        "disconnect": device.disconnect,
        "stop_advertising": device.stop_advertising,
        "read_gatt": lambda: device.read_gatt(3),
    }[operation]
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(action)
            assert waiting.wait(1)
            try:
                with pytest.raises(FutureTimeout):
                    future.result(timeout=0.03)
            finally:
                device._transport._rx.put(json.dumps(payload))
            result = future.result(timeout=1)
            if operation == "read_gatt":
                assert result == b"BleuIO"
    finally:
        device.close()


@pytest.mark.parametrize("cancellation_error", [0, 2, 5, 9])
def test_owned_pending_connection_is_cancelled_before_close(cancellation_error):
    from autospan_ble.bleuio.types import DeviceState

    class Environment(FakeBleuIOEnvironment):
        cancelled = 0

        def handle(self, command):
            lines = super().handle(command)
            if command == "AT+CANCELCONNECT":
                self.cancelled += 1
                lines = [line.replace('"err":0', f'"err":{cancellation_error}') for line in lines]
            return lines

    env = Environment(endpoint="fake://pending", serial_number="X")
    device = device_for(env)
    device.open()
    device._owns_connection = True  # Accepted attempt, no completed link yet.
    if cancellation_error == 9:
        with pytest.raises(ExceptionGroup) as error:
            device.close()
        rejected = error.value.exceptions[0]
        assert isinstance(rejected, CommandRejected)
        assert rejected.error_code == 9
    else:
        device.close()
    assert env.cancelled == 1
    assert device.state is (DeviceState.FAILED if cancellation_error == 9 else DeviceState.CLOSED)
    assert device._client is None


def test_disconnect_waits_for_target_release_before_another_connection():
    class Environment(FakeBleuIOEnvironment):
        release_queries = 0
        releasing = False

        def handle(self, command):
            if command.startswith("AT+GAPCONNECT=") and self.releasing:
                raise AssertionError("new link started before the old target was released")
            lines = super().handle(command)
            if command == "AT+GAPDISCONNECT":
                self.releasing = True
            if command == "AT+TARGETCONN" and self.releasing:
                self.release_queries += 1
                if self.release_queries == 1:
                    lines = [line.replace('"target":"FFFF"', '"target":"0000"') for line in lines]
                else:
                    self.releasing = False
            return lines

    env = Environment(endpoint="fake://delayed-release", serial_number="X")
    device = device_for(env)
    device.open()
    try:
        with device.connected("12:34:56:78:9A:BC"):
            assert device.read_gatt(3) == b"BleuIO"
        assert env.release_queries == 2
        with device.connected("12:34:56:78:9A:BC"):
            assert device.read_gatt(3) == b"BleuIO"
    finally:
        device.close()


def test_connection_release_wait_has_a_bounded_deadline(monkeypatch):
    env = FakeBleuIOEnvironment(endpoint="fake://stuck-release", serial_number="X")
    device = device_for(env)
    device.open()
    handle = env.handle

    def stuck_target(command):
        lines = handle(command)
        if command == "AT+TARGETCONN":
            return [line.replace('"target":"FFFF"', '"target":"0000"') for line in lines]
        return lines

    monkeypatch.setattr(env, "handle", stuck_target)
    try:
        with pytest.raises(CommandTimeout, match="connection release not ready"):
            device._wait_connection_released(timeout_s=0.02)
    finally:
        monkeypatch.setattr(env, "handle", handle)
        device.close()


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"readStatus": value}, None)
        for value in (0.5, -0.5, float("inf"), float("nan"), False, True, None, "bad", [], {})
    ]
    + [({"len": value}, None) for value in (1.5, float("inf"), False, True, None, "bad", -1)]
    + [({"hex": value}, None) for value in (None, 41, "0x0X41", "zz", "4")]
    + [
        ({"readStatus": "0", "len": "1", "hex": "0X41"}, b"A"),
        ({"readStatus": "5"}, None),
    ],
)
def test_gatt_payload_validation_preserves_protocol_errors(changes, expected):
    class Environment(FakeBleuIOEnvironment):
        def handle(self, command):
            lines = super().handle(command)
            if command == "AT+GATTCREAD=0003":
                lines = [line for line in lines if '"775"' not in line]
                details = {"handle": "0003", "readStatus": 0, "hex": "41", "len": 1}
                details.update(changes)
                lines.append(json.dumps({"775": "0000", "evt": details}))
            return lines

    device = device_for(Environment(endpoint="fake://validation", serial_number="X"))
    device.open()
    try:
        device.connect("12:34:56:78:9A:BC")
        if expected is None:
            with pytest.raises(BleuIOError):
                device.read_gatt(3)
        else:
            assert device.read_gatt(3) == expected
    finally:
        device.close()
