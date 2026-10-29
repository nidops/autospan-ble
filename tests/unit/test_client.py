from autospan_ble.bleuio.client import BleuIOClient
from autospan_ble.simulation.fake import FakeBleuIOEnvironment, FakeLineTransport
from autospan_ble.bleuio.errors import CommandRejected, CommandTimeout, DeviceDisconnected
import pytest


def test_initialization_accepts_observed_echo_off_reply():
    captured = []
    env = FakeBleuIOEnvironment(endpoint="fake://one")
    transport = FakeLineTransport(env, env.endpoint)
    transport.open()
    client = BleuIOClient(transport, evidence=lambda kind, fields: captured.append((kind, fields)))
    try:
        client.initialize()
        assert any(kind == "rx" and fields.get("line") == "ECHO OFF" for kind, fields in captured)
    finally:
        client.close()


def test_client_initializes_and_correlates_command():
    env = FakeBleuIOEnvironment(endpoint="fake://one")
    transport = FakeLineTransport(env, "fake://one")
    transport.open()
    client = BleuIOClient(transport)
    try:
        identity = client.initialize()
        assert identity.command == "ATI"
        assert identity.replies[0]["fw"] == "fake-1.0"
        result = client.command("AT+CENTRAL")
        assert result.acknowledgement["err"] == 0
        assert result.replies[0]["role"] == "central"
    finally:
        client.close()


def test_fake_rejects_unknown_commands_without_invalidating_command_lane():
    env = FakeBleuIOEnvironment(endpoint="fake://one")
    transport = FakeLineTransport(env, env.endpoint)
    transport.open()
    client = BleuIOClient(transport)
    try:
        client.initialize()
        with pytest.raises(CommandRejected, match="unknown command"):
            client.command("AT+UNSUPPORTED")
        assert client.command("ATI").replies[0]["fw"] == "fake-1.0"
    finally:
        client.close()


def test_command_and_scan_events_are_separate():
    env = FakeBleuIOEnvironment(
        endpoint="fake://one",
        scan_results=({"addr": "[0]12:34:56:78:9A:BC", "rssi": -42},),
    )
    transport = FakeLineTransport(env, "fake://one")
    transport.open()
    client = BleuIOClient(transport)
    try:
        client.initialize()
        result = client.command("AT+GAPSCAN=1")
        assert result.replies[0]["action"] == "scanning"
        scan = client.wait_event(
            lambda e: e.kind == "S" and e.command_index == result.index, timeout_s=1
        )
        done = client.wait_event(
            lambda e: e.kind == "SE" and e.command_index == result.index, timeout_s=1
        )
        assert scan.payload["rssi"] == -42
        assert done.kind == "SE"
    finally:
        client.close()


def test_stale_command_frame_cannot_complete_next_command():
    class Transport(FakeLineTransport):
        inject = False

        def write(self, data):
            if self.inject:
                for line in ['{"C":999,"cmd":"ATI"}', '{"A":999,"err":0}', '{"E":999}']:
                    self._rx.put(line)
            super().write(data)

    env = FakeBleuIOEnvironment(endpoint="fake://one")
    transport = Transport(env, env.endpoint)
    transport.open()
    client = BleuIOClient(transport)
    try:
        client.initialize()
        transport.inject = True
        result = client.command("AT+CENTRAL")
        assert result.index != 999
        assert result.replies[0]["role"] == "central"
    finally:
        client.close()


def test_command_timeout_invalidates_lane_until_reinitialized():
    class Transport(FakeLineTransport):
        def write(self, data):
            if data.strip() != b"AT+MISSING":
                super().write(data)

    env = FakeBleuIOEnvironment(endpoint="fake://one")
    transport = Transport(env, env.endpoint)
    transport.open()
    client = BleuIOClient(transport)
    try:
        client.initialize()
        with pytest.raises(CommandTimeout):
            client.command("AT+MISSING", timeout_s=0.02)
        with pytest.raises(DeviceDisconnected) as error:
            client.command("ATI")
        assert isinstance(error.value.__cause__, CommandTimeout)
    finally:
        client.close()


@pytest.mark.parametrize("phase", ["write", "wait"])
def test_interrupted_command_invalidates_lane(monkeypatch, phase):
    env = FakeBleuIOEnvironment(endpoint="fake://interrupted")
    transport = FakeLineTransport(env, env.endpoint)
    transport.open()
    client = BleuIOClient(transport)
    try:
        client.initialize()
        write = transport.write

        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        def interrupted_write(data):
            if phase == "write":
                interrupt()
            monkeypatch.setattr(client._pending.done, "wait", interrupt)

        monkeypatch.setattr(transport, "write", interrupted_write)
        with pytest.raises(KeyboardInterrupt):
            client.command("ATI")
        monkeypatch.setattr(transport, "write", write)
        with pytest.raises(DeviceDisconnected) as error:
            client.command("ATI")
        assert isinstance(error.value.__cause__, KeyboardInterrupt)
    finally:
        client.close()


def test_independent_devices_have_independent_command_lanes():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    blocked = Event()
    release = Event()

    class Transport(FakeLineTransport):
        def write(self, data):
            if data.strip() == b"AT+CENTRAL":
                blocked.set()
                assert release.wait(1)
            super().write(data)

    clients = []
    for number, transport_type in enumerate([Transport, FakeLineTransport]):
        env = FakeBleuIOEnvironment(endpoint=f"fake://{number}")
        transport = transport_type(env, env.endpoint)
        transport.open()
        client = BleuIOClient(transport)
        client.initialize()
        clients.append(client)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(clients[0].command, "AT+CENTRAL")
            assert blocked.wait(1)
            try:
                second = executor.submit(clients[1].command, "AT+CENTRAL")
                assert second.result(timeout=0.5).replies[0]["role"] == "central"
                assert not first.done()
            finally:
                release.set()
            assert first.result(timeout=1).replies[0]["role"] == "central"
    finally:
        for client in clients:
            client.close()


def test_waiting_for_busy_command_lane_has_its_own_deadline():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    blocked = Event()
    release = Event()

    class Transport(FakeLineTransport):
        def write(self, data):
            if data.strip() == b"AT+CENTRAL":
                blocked.set()
                assert release.wait(1)
            super().write(data)

    env = FakeBleuIOEnvironment(endpoint="fake://busy")
    transport = Transport(env, env.endpoint)
    transport.open()
    client = BleuIOClient(transport)
    try:
        client.initialize()
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(client.command, "AT+CENTRAL")
            assert blocked.wait(1)
            try:
                with pytest.raises(CommandTimeout, match="lane busy"):
                    client.command("ATI", timeout_s=0.02)
            finally:
                release.set()
            assert first.result(timeout=1).index is not None
        assert client.command("ATI").replies
    finally:
        client.close()


@pytest.mark.parametrize("release_on_close", [True, False])
def test_transport_close_failure_still_joins_reader_and_preserves_all_errors(release_on_close):
    from threading import Event
    from time import sleep

    from autospan_ble.bleuio.errors import BleuIOError

    reading = Event()
    release = Event()
    cleanup = OSError("transport close failed")

    class Transport(FakeLineTransport):
        def readline(self, timeout_s):
            reading.set()
            release.wait(3)
            sleep(0.02)  # A close can release a read before its worker has exited.
            return None

        def close(self):
            super().close()
            if release_on_close:
                release.set()
            raise cleanup

    env = FakeBleuIOEnvironment(endpoint="fake://blocked-reader")
    transport = Transport(env, env.endpoint)
    transport.open()
    client = BleuIOClient(transport)
    client.start_reader()
    reader = client._reader
    try:
        assert reading.wait(1)
        if release_on_close:
            with pytest.raises(OSError) as caught:
                client.close()
            assert caught.value is cleanup
            assert not reader.is_alive()
        else:
            with pytest.raises(ExceptionGroup) as caught:
                client.close()
            assert caught.value.exceptions[0] is cleanup
            assert isinstance(caught.value.exceptions[1], BleuIOError)
            assert "RX worker did not stop" in str(caught.value.exceptions[1])
    finally:
        release.set()
        reader.join(1)
        assert not reader.is_alive()


@pytest.mark.parametrize(
    "command, frames, outcome",
    [
        ("AT", ['{"C":99,"cmd":"ATI"}', '{"A":99,"err":0}', '{"E":99}'], CommandTimeout),
        ("AT", ['{"A":1,"err":0}', '{"E":1}'], CommandTimeout),
        ("AT", ['{"C":1,"cmd":"AT"}', '{"A":1,"err":0}'], CommandTimeout),
        ("AT", ['{"C":1,"cmd":"AT"}', '{"E":1}'], CommandTimeout),
        ("AT", ['{"C":1,"cmd":"AT"}', '{"A":1,"err":0}', '{"E":2}'], CommandTimeout),
        (
            "AT",
            ['{"C":1,"cmd":"AT"}', '{"A":1,"err":0}', '{"A":99,"err":0}', '{"E":99}'],
            CommandTimeout,
        ),
        (
            "AT",
            ['{"C":1,"cmd":"AT"}', '{"A":1,"err":0}', '{"A":99,"err":5}', '{"E":99}', '{"E":1}'],
            None,
        ),
        ("AT", ['{"C":1,"cmd":"AT"}', '{"A":1,"err":5}'], CommandRejected),
        ("AT", ['{"C":1,"cmd":"AT"}', "OK"], CommandTimeout),
        ("ATV1", ["OK"], CommandTimeout),
        ("AT", ["OK"], None),
        ("ATE0", ["ECHO OFF"], None),
        ("ATE0", ["OK"], None),
        ("ATV1", ["VERBOSE ON"], None),
    ],
)
def test_bootstrap_correlates_complete_frames_and_legacy_replies(
    monkeypatch, command, frames, outcome
):
    from itertools import count
    import autospan_ble.bleuio.client as module

    clock = count()
    monkeypatch.setattr(module, "monotonic", lambda: next(clock) / 100)

    class Transport:
        endpoint = "fake://bootstrap"

        def write(self, data):
            assert data == (command + "\r\n").encode("ascii")

        def readline(self, timeout_s):
            return frames.pop(0) if frames else None

    client = BleuIOClient(Transport())
    if outcome is None:
        client._bootstrap(command, timeout_s=0.5)
    else:
        with pytest.raises(outcome):
            client._bootstrap(command, timeout_s=0.5)


@pytest.mark.parametrize("later_ns", [100, 200])
def test_event_receive_order_precedes_delayed_evidence(monkeypatch, later_ns):
    from threading import Event
    from autospan_ble.bleuio.types import _next_event_order

    entered, release = Event(), Event()
    clock = [100]
    monkeypatch.setattr("autospan_ble.bleuio.client.monotonic_ns", lambda: clock[0])

    class Transport:
        endpoint = "fake://timestamp"

        def readline(self, timeout_s):
            return '{"775":"0000"}'

        def close(self):
            pass

    def evidence(kind, fields):
        entered.set()
        assert release.wait(2)
        client._stop.set()

    client = BleuIOClient(Transport(), evidence=evidence)
    client.start_reader()
    try:
        assert entered.wait(2)
        operation_order = _next_event_order()
        clock[0] = later_ns
    finally:
        release.set()
        client.close()
    assert client.failure is None
    event = client._events.get_nowait()
    assert event.received_monotonic_ns == 100
    assert event._received_order < operation_order
