import json

import pytest

from autospan_ble import BleAddress, BleCodec, BleGatt, ScanResult, codec
from autospan_ble.bleuio.address import BleAddressKind, BleAddressType
from autospan_ble.bleuio.device import BleuIODevice
from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
from autospan_ble.bleuio.types import GattValue, ScanResults
from autospan_ble.simulation.fake import (
    FakeBleuIOEnvironment,
    FakeLineTransport,
    FakePortEnumerator,
)


def _device(*rows):
    env = FakeBleuIOEnvironment(
        endpoint="fake://ergonomics",
        serial_number="ERGONOMICS",
        scan_results=rows,
    )
    device = BleuIODevice(
        "scanner",
        SerialDeviceSelector(serial_number="ERGONOMICS"),
        SerialResolver(FakePortEnumerator(env)),
        lambda endpoint: FakeLineTransport(env, endpoint),
    )
    return env, device


def _record_commands(env, monkeypatch):
    commands = []
    handle = env.handle

    def record(command):
        commands.append(command)
        return handle(command)

    monkeypatch.setattr(env, "handle", record)
    return commands


def test_scan_result_expect_is_collection_compatible_and_returns_matching_peer():
    _, device = _device(
        {"addr": "[0]12:34:56:78:9A:BC", "rssi": -42, "name": "BleuIO"},
        {"addr": "[1]4C:C1:EE:7B:71:96", "rssi": -70, "name": "Other"},
    )
    device.open()
    try:
        rows = device.scan(1)
        assert isinstance(rows, ScanResults)
        assert len(rows) == 2
        assert rows.expect("BleuIO") == rows[0]
        assert rows.expect(address="12:34:56:78:9A:BC", min_rssi=-50) == rows[0]
        assert rows[1].address.address_type is BleAddressType.RANDOM
        assert rows[1].address.kind is BleAddressKind.RESOLVABLE_PRIVATE
    finally:
        device.close()


def test_scan_expect_failure_lists_observed_peers():
    _, device = _device({"addr": "[0]12:34:56:78:9A:BC", "rssi": -42, "name": "BleuIO"})
    device.open()
    try:
        with pytest.raises(AssertionError, match="observed") as error:
            device.scan(1).expect(name="missing")
        assert "12:34:56:78:9A:BC" in str(error.value)
        assert "BleuIO" in str(error.value)
    finally:
        device.close()


def test_scan_result_can_be_passed_directly_to_connection_scope():
    env, device = _device({"addr": "[0]12:34:56:78:9A:BC", "rssi": -42})
    device.open()
    try:
        peer = device.scan(1).expect(address="12:34:56:78:9A:BC")
        with device.connected(peer):
            assert env.connected
        assert not env.connected
    finally:
        device.close()


def test_builtin_gatt_identifier_matches_yaml_codec_and_supports_expect():
    assert BleGatt.DEVICE_NAME.value == BleCodec.DEVICE_NAME.value
    assert BleGatt.DEVICE_NAME.codec is codec(BleCodec.DEVICE_NAME)

    _, device = _device()
    device.open()
    try:
        with device.connected("12:34:56:78:9A:BC"):
            value = device.gatt(BleGatt.DEVICE_NAME)
        assert isinstance(value, GattValue)
        assert value.handle == 0x0003
        assert value.raw == b"BleuIO"
        assert value.decoded == {"name": "BleuIO"}
        assert value.codec_id == BleCodec.DEVICE_NAME.value
        assert value.expect(name="BleuIO") is value
    finally:
        device.close()


def test_raw_gatt_remains_available_and_expect_can_check_bytes():
    _, device = _device()
    device.open()
    try:
        with device.connected("12:34:56:78:9A:BC"):
            value = device.gatt(0x0003)
        assert value.decoded is None
        assert value.expect(b"BleuIO") is value
        with pytest.raises(AssertionError, match="expected"):
            value.expect(b"wrong")
    finally:
        device.close()


def test_gatt_expect_distinguishes_missing_field_from_none_value():
    value = GattValue(handle=3, raw=b"", decoded={"present": None})
    assert value.expect(present=None) is value
    with pytest.raises(AssertionError, match="field missing"):
        value.expect(missing=None)


def test_scan_expect_preserves_address_type_and_combines_criteria_on_one_row():
    public = BleAddress.parse("[0]4C:C1:EE:7B:71:96")
    private = BleAddress.parse("[1]4C:C1:EE:7B:71:96")
    rows = ScanResults(
        (
            ScanResult(public, rssi=-30, name="Sensor:42", manufacturer_id=0x075B),
            ScanResult(private, rssi=-80, name="Other", manufacturer_id=0xFFFF),
        )
    )
    assert rows.expect("Sensor:42") is rows[0]
    assert rows.expect(" [1]4c:c1:ee:7b:71:96 ") is rows[1]
    assert rows.expect(address=private) is rows[1]
    assert rows.expect(address="4c:c1:ee:7b:71:96") is rows[0]
    assert rows.expect(name="Sensor:42", manufacturer_id=0x075B, min_rssi=-30) is rows[0]
    with pytest.raises(AssertionError, match="rssi>=-70"):
        rows.expect(address=private, min_rssi=-70)
    with pytest.raises(AssertionError, match="manufacturer_id=0xFFFF"):
        rows.expect(name="Sensor:42", manufacturer_id=0xFFFF)


@pytest.mark.parametrize(
    "criteria",
    [
        {"address": "invalid"},
        {"address": "[2]12:34:56:78:9A:BC"},
        {"address": 3},
        {"name": 3},
        {"manufacturer_id": True},
        {"manufacturer_id": -1},
        {"manufacturer_id": 0x10000},
        {"min_rssi": True},
        {"min_rssi": -42.5},
    ],
)
def test_scan_expect_rejects_invalid_criteria_even_without_observations(criteria):
    with pytest.raises(ValueError):
        ScanResults().expect(**criteria)


def test_scan_expect_rejects_conflicting_or_non_string_positional_criteria():
    with pytest.raises(ValueError, match="cannot be combined"):
        ScanResults().expect("BleuIO", name="Other")
    with pytest.raises(ValueError, match="string"):
        ScanResults().expect(3)
    with pytest.raises(
        AssertionError,
        match="at least one peer.*observed",
    ):
        ScanResults().expect()


@pytest.mark.parametrize("expected", [3, True, [0, 0, 0], "abc"])
def test_raw_gatt_expect_rejects_values_that_bytes_would_silently_coerce(expected):
    with pytest.raises(ValueError, match="raw expected value"):
        GattValue(handle=3, raw=b"\x00\x00\x00").expect(expected)


@pytest.mark.parametrize("expected", [b"BleuIO", bytearray(b"BleuIO"), memoryview(b"BleuIO")])
def test_raw_gatt_expect_accepts_bytes_like_values(expected):
    value = GattValue(handle=3, raw=b"BleuIO")
    assert value.expect(expected) is value


def test_gatt_expect_reports_wrong_fields_and_requires_decoding():
    with pytest.raises(AssertionError, match="name: expected 'wrong', got 'BleuIO'"):
        GattValue(handle=3, raw=b"BleuIO", decoded={"name": "BleuIO"}).expect(name="wrong")
    with pytest.raises(AssertionError, match="no decoded fields"):
        GattValue(handle=3, raw=b"BleuIO").expect(name="BleuIO")
    with pytest.raises(ValueError, match="either"):
        GattValue(handle=3, raw=b"BleuIO").expect(b"BleuIO", name="BleuIO")


def test_scan_result_keeps_random_type_for_target_scan_and_connection(monkeypatch):
    env, device = _device(
        {"addr": "[1]4C:C1:EE:7B:71:96", "rssi": -42, "type": 0, "data": "020106"}
    )
    commands = _record_commands(env, monkeypatch)
    device.open()
    try:
        peer = device.scan(1).expect(address="[1]4C:C1:EE:7B:71:96")
        assert device.scan_target(peer, seconds=1).expect().address == peer.address
        with device.connected(peer):
            assert (
                device.gatt(3, codec=BleCodec.DEVICE_NAME).expect(name="BleuIO").codec_id
                == BleCodec.DEVICE_NAME.value
            )
        assert "AT+SCANTARGET=[1]4C:C1:EE:7B:71:96=1" in commands
        assert "AT+GAPCONNECT=[1]4C:C1:EE:7B:71:96" in commands
        for operation in (device.connect, device.scan_target):
            before = tuple(commands)
            with pytest.raises(ValueError, match="conflicts"):
                operation(peer, address_type=BleAddressType.PUBLIC)
            assert tuple(commands) == before
    finally:
        device.close()


def test_gatt_rejects_codec_override_and_invalid_decoder_before_sending_commands(monkeypatch):
    env, device = _device()
    commands = _record_commands(env, monkeypatch)
    device.open()
    try:
        before = tuple(commands)
        with pytest.raises(ValueError, match="already define"):
            device.gatt(BleGatt.DEVICE_NAME, codec=BleCodec.DEVICE_NAME)
        with pytest.raises(ValueError, match="payload decoder"):
            device.gatt(3, codec=object())
        assert tuple(commands) == before
    finally:
        device.close()


def test_project_gatt_decode_failure_releases_connection_and_allows_another_read():
    from structwire import DecodeError, Field, Layout

    env, device = _device()
    device.open()
    try:
        wrong_size = Layout(Field("name", "ascii", length=7)).compile()
        with pytest.raises(DecodeError):
            with device.connected("12:34:56:78:9A:BC"):
                device.gatt(3, codec=wrong_size)
        assert not env.connected
        with device.connected("12:34:56:78:9A:BC"):
            assert device.gatt(BleGatt.DEVICE_NAME).expect(name="BleuIO").raw == b"BleuIO"
    finally:
        device.close()


def test_failed_link_gatt_completion_propagates_and_cancels_owned_attempt(monkeypatch):
    from autospan_ble.bleuio.errors import BleuIOError

    env, device = _device()
    commands = _record_commands(env, monkeypatch)
    handle = env.handle

    def failed_link(command):
        lines = handle(command)
        if command == "AT+GATTCREAD=0003":
            env.connected = False
            lines = [line for line in lines if '"775"' not in line]
            lines.append(json.dumps({"775": "0000", "evt": {"handle": "0003", "readStatus": 67}}))
        return lines

    monkeypatch.setattr(env, "handle", failed_link)
    device.open()
    try:
        with pytest.raises(BleuIOError, match="GATT read 0003 failed: 67"):
            with device.connected("12:34:56:78:9A:BC"):
                device.gatt(BleGatt.DEVICE_NAME)
        assert "AT+CANCELCONNECT" in commands
        assert not env.connected
    finally:
        device.close()
