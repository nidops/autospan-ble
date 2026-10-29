from autospan_ble.bleuio.address import BleAddressKind, BleAddressType, BleuIOAddressMode
from autospan_ble.bleuio.device import BleuIODevice
import pytest
from autospan_ble.bleuio.errors import BleuIOError, CommandRejected
from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
from autospan_ble.bleuio.types import DeviceState
from autospan_ble.simulation.fake import (
    FakeBleuIOEnvironment,
    FakeLineTransport,
    FakePortEnumerator,
)


def make_device(scan_results=()):
    env = FakeBleuIOEnvironment(
        endpoint="fake://ble#0",
        serial_number="BLE-001",
        scan_results=scan_results,
    )
    selector = SerialDeviceSelector(serial_number="BLE-001")
    resolver = SerialResolver(FakePortEnumerator(env))
    device = BleuIODevice(
        "ble_1",
        selector,
        resolver,
        lambda endpoint: FakeLineTransport(env, endpoint),
        reset_timeout_s=2,
    )
    return env, device


def test_failed_close_does_not_report_ready():
    _, device = make_device()
    device.open()

    device.close(failed=True)

    assert device.state is DeviceState.FAILED
    assert device.endpoint is None

    device.close()
    assert device.state is DeviceState.CLOSED


def test_scan_returns_typed_results():
    _, device = make_device(({"addr": "[0]12:34:56:78:9A:BC", "rssi": -50},))
    device.open()
    try:
        rows = device.scan(seconds=1)
        assert len(rows) == 1
        assert rows[0].address.value == "12:34:56:78:9A:BC"
        assert rows[0].address.address_type is BleAddressType.PUBLIC
        assert rows[0].address.kind is BleAddressKind.PUBLIC
        assert rows[0].rssi == -50
    finally:
        device.close()


def test_role_convergence_and_repeated_scan():
    env, device = make_device()
    device.open()
    try:
        device.set_role("central")
        device.set_role("central")
        assert env.role == "central"
        assert device.scan(seconds=1) == ()
        assert device.scan(seconds=1) == ()
    finally:
        device.close()


def test_reset_rebinds_changed_endpoint_without_changing_logical_identity():
    env, device = make_device()
    device.open()
    try:
        old_endpoint = device.endpoint
        device.reset(timeout_s=2)
        assert device.name == "ble_1"
        assert device.state == DeviceState.READY
        assert device.endpoint != old_endpoint
        assert device.endpoint == env.endpoint
        assert env.reset_count == 1
        assert device.raw("ATI").replies[0]["fw"] == "fake-1.0"
    finally:
        device.close()


def test_close_stops_advertising_owned_by_endpoint():
    env, device = make_device()
    device.open()
    device.start_advertising()
    assert env.advertising
    device.close()
    assert not env.advertising
    assert device.identity is None
    assert device.endpoint is None


def test_advertising_start_and_stop_converge():
    env, device = make_device()
    device.open()
    try:
        device.start_advertising()
        device.start_advertising()
        assert env.advertising
        device.stop_advertising()
        device.stop_advertising()
        assert not env.advertising
    finally:
        device.close()


def test_owned_advertising_stop_survives_idle_status_before_peer_resumes():
    env, device = make_device()
    handle = env.handle
    pending_resume = False

    def respond(command):
        nonlocal pending_resume
        if command == "AT+GAPSTATUS" and pending_resume:
            env.advertising = False
            lines = handle(command)
            env.advertising = True
            pending_resume = False
            return lines
        return handle(command)

    env.handle = respond
    device.open()
    try:
        with device.advertising():
            pending_resume = True
        assert not env.advertising
        assert device.scan(seconds=1) == ()
    finally:
        device.close()


@pytest.mark.parametrize("still_advertising", [False, True])
def test_advertising_stop_accepts_not_allowed_only_after_verifying_idle(still_advertising):
    env, device = make_device()
    device.open()
    device.start_advertising()
    env.advertising = still_advertising
    handle = env.handle

    def respond(command):
        if command == "AT+ADVSTOP":
            return [
                '{"C":100,"cmd":"AT+ADVSTOP"}',
                '{"A":100,"err":5,"errMsg":"not allowed"}',
                '{"E":100,"nol":3}',
            ]
        return handle(command)

    env.handle = respond
    try:
        if still_advertising:
            with pytest.raises(CommandRejected, match="not allowed"):
                device.stop_advertising()
        else:
            device.stop_advertising()
    finally:
        env.handle = handle
        device.close()


def test_rx_failure_does_not_report_ready_and_open_reinitializes():
    from threading import Event

    env, device = make_device()
    failed = Event()
    device._evidence = lambda kind, fields: failed.set() if kind == "serial_rx_failed" else None
    device.open()
    device._transport.close()
    assert failed.wait(1)
    assert device.state == DeviceState.FAILED
    device.open()
    try:
        assert device.state == DeviceState.READY
        assert device.identity is not None
    finally:
        device.close()


def test_advertising_context_owns_lifetime():
    env, device = make_device()
    device.open()
    try:
        with device.advertising():
            assert env.advertising
        assert not env.advertising
    finally:
        device.close()


def test_advertising_context_stops_on_body_failure():
    env, device = make_device()
    device.open()
    try:
        with pytest.raises(RuntimeError, match="body failed"):
            with device.advertising():
                assert env.advertising
                raise RuntimeError("body failed")
        assert not env.advertising
    finally:
        device.close()


def test_connection_context_owns_lifetime():
    env, device = make_device()
    device.open()
    try:
        with device.connected("12:34:56:78:9A:BC"):
            assert env.connected
        assert not env.connected
    finally:
        device.close()


@pytest.mark.parametrize("operation", ["advertising", "connected"])
@pytest.mark.parametrize("body_error", [RuntimeError("body failed"), KeyboardInterrupt()])
def test_endpoint_context_preserves_body_and_cleanup_errors(monkeypatch, operation, body_error):
    env, device = make_device()
    device.open()
    cleanup_error = OSError("cleanup failed")
    cleanup = "stop_advertising" if operation == "advertising" else "disconnect"
    original_cleanup = getattr(device, cleanup)

    def fail_cleanup():
        original_cleanup()
        raise cleanup_error

    monkeypatch.setattr(device, cleanup, fail_cleanup)
    scope = device.advertising() if operation == "advertising" else device.connected(env.address)
    try:
        with pytest.raises(BaseExceptionGroup) as failure:
            with scope:
                raise body_error
        assert failure.value.exceptions == (body_error, cleanup_error)
        assert not env.advertising and not env.connected
    finally:
        device.close()


@pytest.mark.parametrize(
    "operation,phase",
    [("advertising", "evidence"), ("connected", "evidence"), ("connected", "wait")],
)
def test_endpoint_context_cleans_up_failed_entry(monkeypatch, operation, phase):
    env, device = make_device()
    device.open()
    entry_error = RuntimeError("entry failed after acquisition")
    event_name = "advertising_started" if operation == "advertising" else "connect_requested"

    def fail_entry(event, **fields):
        if event == event_name:
            raise entry_error

    # Connection ownership is established before waiting for its completion event.
    if phase == "wait":
        original_wait = device.wait_event

        def fail_wait(predicate, *, timeout_s=5.0):
            if env.connected:
                raise entry_error
            return original_wait(predicate, timeout_s=timeout_s)

        monkeypatch.setattr(device, "wait_event", fail_wait)
    else:
        monkeypatch.setattr(device, "_emit", fail_entry)
    scope = device.advertising() if operation == "advertising" else device.connected(env.address)
    try:
        with pytest.raises(RuntimeError) as failure:
            with scope:
                pytest.fail("body ran after failed entry")
        assert failure.value is entry_error
        assert not env.advertising and not env.connected
    finally:
        device.close()


@pytest.mark.parametrize("operation", ["advertising", "connected"])
def test_endpoint_context_rejects_overlapping_owned_lifetime(operation):
    env, device = make_device()
    device.open()

    def scope():
        return device.advertising() if operation == "advertising" else device.connected(env.address)

    try:
        with scope():
            with pytest.raises(BleuIOError, match="already owns"):
                with scope():
                    pytest.fail("nested scope stole its outer lifetime")
            assert env.advertising if operation == "advertising" else env.connected
        assert not env.advertising and not env.connected
    finally:
        device.close()


def test_scan_decodes_rpa_name_and_manufacturer_id():
    _, device = make_device(
        (
            {
                "addr": "[1]4C:C1:EE:7B:71:96",
                "rssi": -61,
                "name": "sensor",
                "mfsid": "075B",
            },
        )
    )
    device.open()
    try:
        (row,) = device.scan(seconds=1)
        assert row.address.address_type is BleAddressType.RANDOM
        assert row.address.kind is BleAddressKind.RESOLVABLE_PRIVATE
        assert row.name == "sensor"
        assert row.manufacturer_id == 0x075B
    finally:
        device.close()


def test_address_mode_query_and_convergence():
    _, device = make_device()
    device.open()
    try:
        assert device.address_mode() is BleuIOAddressMode.PUBLIC_STATIC
        assert (
            device.set_address_mode(BleuIOAddressMode.PRIVATE_RANDOM_RESOLVABLE)
            is BleuIOAddressMode.PRIVATE_RANDOM_RESOLVABLE
        )
        assert device.address_mode() is BleuIOAddressMode.PRIVATE_RANDOM_RESOLVABLE
    finally:
        device.close()


def test_scan_target_returns_parsed_advertising_and_scan_response():
    _, device = make_device(
        (
            {
                "addr": "[1]4C:C1:EE:7B:71:96",
                "type": 0,
                "rssi": -55,
                "mfsid": "075B",
                "data": "02010603FF5B071107B75C49D204A34071A0B535853EB08307000000000000",
            },
            {
                "addr": "[1]4C:C1:EE:7B:71:96",
                "type": 4,
                "data": "09094175746F5370616E",
            },
        )
    )
    device.open()
    try:
        peer = device.scan(seconds=1)[0].address
        reports = device.scan_target(peer, seconds=1)
        assert len(reports) == 2
        assert reports[0].is_advertisement
        assert reports[0].address.kind is BleAddressKind.RESOLVABLE_PRIVATE
        assert reports[0].manufacturer_id == 0x075B
        assert reports[0].data.manufacturer_data(0x075B)[0].company_id == 0x075B
        assert reports[1].is_scan_response
        assert reports[1].data.local_name == "AutoSpan"
    finally:
        device.close()


def test_scan_target_uses_typed_target_when_st_record_omits_address_type():
    _, device = make_device(
        (
            {
                "addr": "4C:C1:EE:7B:71:96",
                "type": 0,
                "data": "020106",
            },
        )
    )
    device.open()
    try:
        # GAPSCAN officially carries the address type, so create the target explicitly
        # to model SCANTARGET with ATSAT disabled.
        from autospan_ble.bleuio.address import BleAddress

        target = BleAddress.parse("[1]4C:C1:EE:7B:71:96")
        (report,) = device.scan_target(target, seconds=1)
        assert report.address.address_type is BleAddressType.RANDOM
        assert report.address.value == target.value
    finally:
        device.close()


def test_advertising_accepts_binary_ad_data_without_manual_hex_formatting():
    env, device = make_device()
    device.open()
    try:
        with device.advertising(adv_data=bytes.fromhex("09094175746F5370616E")):
            assert env.advertising_data == "09094175746F5370616E"
    finally:
        device.close()


def test_typed_random_address_connect_preserves_type_and_target_readiness():
    from autospan_ble.bleuio.address import BleAddress

    env, device = make_device()
    device.open()
    target = BleAddress.parse("[1]4C:C1:EE:7B:71:96")
    try:
        with device.connected(target):
            assert env.connected
        assert not env.connected
    finally:
        device.close()


@pytest.mark.parametrize("length", [29, 30, 31, 32])
def test_binary_advertising_reserves_space_for_firmware_flags(length):
    _, device = make_device()
    device.open()
    try:
        with pytest.raises(ValueError, match="at most 28 bytes"):
            device.start_advertising(adv_data=b"x" * length)
    finally:
        device.close()
