"""Failure paths across radio operations and caller-owned payload decoding."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json

import pytest

from autospan_ble import AdStructure, AdvertisingData, BleAddress, BleAddressKind, BleAddressType
from autospan_ble.bleuio.device import BleuIODevice
from autospan_ble.bleuio.errors import (
    BleuIOError,
    CommandRejected,
    CommandTimeout,
    DeviceDisconnected,
)
from autospan_ble.bleuio.payloads import AdvertisingReport, ManufacturerData
from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
from autospan_ble.simulation.fake import (
    FakeBleuIOEnvironment,
    FakeLineTransport,
    FakePortEnumerator,
)


@pytest.fixture
def endpoint():
    env = FakeBleuIOEnvironment(endpoint="fake://payload", serial_number="PAYLOAD")
    device = BleuIODevice(
        "payload",
        SerialDeviceSelector(serial_number="PAYLOAD"),
        SerialResolver(FakePortEnumerator(env)),
        lambda path: FakeLineTransport(env, path),
    )
    device.open()
    try:
        yield env, device
    finally:
        device.close()


@pytest.mark.parametrize("scan", ["scan", "scan_target"])
@pytest.mark.parametrize("failure", ["timeout", "malformed", "different-peer"])
def test_failed_scan_stops_radio_and_preserves_unrelated_records(endpoint, scan, failure):
    env, device = endpoint
    original = env.handle
    commands = []

    def respond(command):
        commands.append(command)
        lines = original(command)
        if command.startswith(("AT+GAPSCAN=", "AT+SCANTARGET=")):
            lines = [line for line in lines if '"SE"' not in line]
            kind = "S" if scan == "scan" else "ST"
            # Same index, different kind must survive the scan operation as well.
            lines.append(json.dumps({"SF": env._index, "data": "020106"}))
            if failure != "timeout":
                row = {kind: env._index, "addr": "[0]12:34:56:78:9A:BC"}
                if failure == "malformed":
                    row.update(addr="bad", data="ZZ", type=0)
                elif scan == "scan_target":
                    row.update(addr="[0]22:34:56:78:9A:BC", data="020106", type=0)
                else:
                    row["rssi"] = 1.5
                lines.append(json.dumps(row))
        return lines

    env.handle = respond
    operation = (
        device.scan
        if scan == "scan"
        else lambda **kw: device.scan_target("12:34:56:78:9A:BC", **kw)
    )
    error = CommandTimeout if failure == "timeout" else BleuIOError
    with pytest.raises(error):
        operation(seconds=10, timeout_s=0.03)
    assert "\x03" in commands
    assert device.wait_event(lambda e: e.kind == "SF", timeout_s=0.1).payload["data"] == "020106"
    assert device.raw("ATI").replies[0]["fw"] == "fake-1.0"
    env.handle = original
    assert device.scan(seconds=1) == ()


def test_scan_and_stop_failure_preserve_both_errors_and_invalidate_session(endpoint):
    env, device = endpoint
    original = env.handle

    def respond(command):
        if command == "\x03":
            raise OSError("interrupt write failed")
        lines = original(command)
        if command.startswith("AT+GAPSCAN="):
            lines = [line for line in lines if '"SE"' not in line]
        return lines

    env.handle = respond
    with pytest.raises(ExceptionGroup) as caught:
        device.scan(seconds=10, timeout_s=0.02)
    assert isinstance(caught.value.exceptions[0], CommandTimeout)
    assert isinstance(caught.value.exceptions[1], DeviceDisconnected)
    with pytest.raises(DeviceDisconnected):
        device.raw("ATI")


@pytest.mark.parametrize("bad", [True, 0.0, 1.0, 2, "1"])
def test_invalid_address_type_never_changes_radio(endpoint, bad):
    env, device = endpoint
    with pytest.raises(ValueError):
        device.connect("12:34:56:78:9A:BC", address_type=bad)
    assert env.role == "dual" and not env.connected


@pytest.mark.parametrize("bad", [True, 1.5, "1", -1, 0x10000])
def test_company_id_requires_integer(bad):
    with pytest.raises(ValueError, match="company ID"):
        ManufacturerData(bad, b"abc")
    with pytest.raises(ValueError, match="company ID"):
        AdvertisingData.parse(b"").manufacturer_data(bad)


@pytest.mark.parametrize("bad", [True, 1.5, "1", -1, 256])
def test_ad_type_requires_integer(bad):
    with pytest.raises(ValueError, match="AD type"):
        AdStructure(bad, b"abc")


def test_mutable_payload_inputs_are_copied_and_integer_payload_is_rejected():
    buffer = bytearray(b"abc")
    item = AdStructure(0x09, buffer)
    manufacturer = ManufacturerData(0xFFFF, buffer)
    buffer[:] = b"xyz"
    assert item.data == manufacturer.payload == b"abc"
    with pytest.raises(ValueError, match="payload must be"):
        AdvertisingData.parse(31)


@pytest.mark.parametrize("data_type", range(6))
def test_report_classifies_all_legacy_advertising_types(data_type):
    report = AdvertisingReport(
        BleAddress.parse("[0]12:34:56:78:9A:BC"), data_type, b"", AdvertisingData.parse(b"")
    )
    assert report.is_advertisement is (data_type < 4)
    assert report.is_scan_response is (data_type == 4)
    assert report.data_type == data_type


def test_direct_address_construction_cannot_forge_command_or_kind():
    with pytest.raises(ValueError, match="invalid Bluetooth address"):
        BleAddress("12:34:56:78:9A:BC=1;[0]22:34:56:78:9A:BC", 0, BleAddressKind.PUBLIC)
    with pytest.raises(ValueError, match="kind conflicts"):
        BleAddress("4C:C1:EE:7B:71:96", 1, BleAddressKind.PUBLIC)
    address = BleAddress("4c:c1:ee:7b:71:96", 1, BleAddressKind.RESOLVABLE_PRIVATE)
    assert address.address_type is BleAddressType.RANDOM
    assert address.value == "4C:C1:EE:7B:71:96"


@pytest.mark.parametrize("data", [b"x" * 32, b"", "", "bad\r\nAT+ADVSTART", bytearray(b"abc")])
def test_invalid_advertising_input_is_rejected_before_role_change(endpoint, data):
    env, device = endpoint
    with pytest.raises(ValueError):
        device.start_advertising(adv_data=data)
    assert env.role == "dual" and not env.advertising


def test_codec_failure_releases_connection_and_next_read_works(endpoint):
    env, device = endpoint

    class BrokenCodec:
        def decode(self, payload):
            assert payload == b"BleuIO"
            raise ValueError("application schema mismatch")

    with pytest.raises(ValueError, match="application schema mismatch"):
        with device.connected(env.address):
            device.read_gatt_decoded(3, BrokenCodec())
    assert not env.connected
    with device.connected(env.address):
        assert device.read_gatt(3) == b"BleuIO"


def test_address_mode_rejection_preserves_existing_mode(endpoint):
    env, device = endpoint
    with device.advertising():
        with pytest.raises(CommandRejected):
            device.set_address_mode(3)
    assert env.address_mode == "PUBLIC_STATIC_ADDRESS"


def test_address_mode_convergence_cannot_interleave_another_device_operation(endpoint):
    env, device = endpoint
    reached_set = Event()
    release_set = Event()
    second_started = Event()
    raw_entered = Event()
    original = env.handle

    def respond(command):
        if command.startswith("AT+GAPADDRTYPE="):
            reached_set.set()
            assert release_set.wait(1)
        if command == "AT+GETMAC":
            raw_entered.set()
        return original(command)

    def other_operation():
        second_started.set()
        return device.mac_address()

    env.handle = respond
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(device.set_address_mode, 3)
        assert reached_set.wait(1)
        second = workers.submit(other_operation)
        assert second_started.wait(1)
        try:
            assert not raw_entered.wait(0.05)
        finally:
            release_set.set()
        first.result(timeout=1)
        assert second.result(timeout=1) == env.address


def test_interrupt_evidence_failure_still_stops_active_radio(endpoint):
    env, device = endpoint
    original = env.handle
    interrupted = []

    def respond(command):
        if command == "\x03":
            interrupted.append(command)
        lines = original(command)
        if command.startswith("AT+GAPSCAN="):
            lines = [line for line in lines if '"SE"' not in line]
        return lines

    def record(kind, fields):
        if fields.get("command") == "CTRL+C":
            raise OSError("evidence disk unavailable")

    env.handle = respond
    device._evidence = record
    with pytest.raises(ExceptionGroup) as caught:
        device.scan(seconds=10, timeout_s=0.02)
    assert isinstance(caught.value.exceptions[0], CommandTimeout)
    assert str(caught.value.exceptions[1]) == "evidence disk unavailable"
    assert interrupted == ["\x03"]
    assert device.raw("ATI").replies


def test_unconfirmed_scan_interrupt_invalidates_lane(endpoint):
    env, device = endpoint
    original = env.handle

    def respond(command):
        if command == "\x03":
            return []
        lines = original(command)
        if command.startswith("AT+GAPSCAN="):
            lines = [line for line in lines if '"SE"' not in line]
        return lines

    env.handle = respond
    client = device._require_client()
    result = device.raw("AT+GAPSCAN=10")
    with pytest.raises(CommandTimeout):
        client.stop_scan(result.index, timeout_s=0.02)
    with pytest.raises(DeviceDisconnected):
        device.raw("ATI")


@pytest.mark.parametrize(
    "fields",
    [
        {"type": True},
        {"type": 1.5},
        {"type": None},
        {"data": None},
        {"data": "ZZ"},
        {"data": "05FF0102"},
        {"rssi": False},
        {"mfsid": 1.5},
        {"addr": "[1]12:34:56:78:9A:BC"},
        {"addr": None},
    ],
)
def test_invalid_target_record_stops_scan_and_keeps_raw_command_usable(endpoint, fields):
    env, device = endpoint
    original = env.handle
    commands = []

    def respond(command):
        commands.append(command)
        lines = original(command)
        if command.startswith("AT+SCANTARGET="):
            lines = [line for line in lines if '"SE"' not in line]
            row = {"ST": env._index, "addr": "[0]12:34:56:78:9A:BC", "type": 0, "data": "020106"}
            lines.append(json.dumps({**row, **fields}))
        return lines

    env.handle = respond
    with pytest.raises(BleuIOError):
        device.scan_target("12:34:56:78:9A:BC", seconds=10, timeout_s=0.1)
    assert "\x03" in commands
    assert device.raw("ATI").replies
