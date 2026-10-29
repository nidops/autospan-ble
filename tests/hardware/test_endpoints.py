"""Physical BleuIO qualification using arbitrary configured endpoint names."""

from threading import enumerate as threads

import pytest
from structwire import Field, Layout

from autospan_ble import BleAddressType, BleGatt

pytestmark = pytest.mark.hardware


def assert_idle(device):
    rows = device.raw("AT+GAPSTATUS").replies
    assert any(row.get("advertising") is False for row in rows)
    assert any(row.get("connected") is False for row in rows)
    assert not any(row.get("advertising") is True or row.get("connected") is True for row in rows)


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_discovery_connection_and_project_gatt_codec(endpoint_pair, runtime):
    """Connect the dongles and read the same name as raw bytes and decoded fields."""
    advertiser, scanner = endpoint_pair
    project_codec = Layout(Field("name", "ascii", length=6), name="bench-device-name").compile()
    evidence = runtime.evidence
    with evidence.step(f"{advertiser.name}: advertising lifetime and cleanup"):
        with advertiser.advertising():
            with evidence.step(
                f"{scanner.name}: discover {advertiser.name} and verify address type"
            ):
                peer = scanner.scan(3).expect(address=advertiser.mac_address())
                assert peer.address.address_type is advertiser.address_mode().address_type
            with evidence.step(f"{scanner.name}: connection lifetime and disconnect"):
                with scanner.connected(peer):
                    with evidence.step(f"{scanner.name}: read built-in Device Name"):
                        scanner.gatt(BleGatt.DEVICE_NAME).expect(name="BleuIO")
                    with evidence.step(f"{scanner.name}: verify raw Device Name bytes"):
                        assert scanner.read_gatt(3) == b"BleuIO"
                    with evidence.step(f"{scanner.name}: decode Device Name with project codec"):
                        scanner.gatt(3, codec=project_codec).expect(name="BleuIO")
    with evidence.step("Verify both endpoints idle after GATT lifecycle"):
        for device in endpoint_pair:
            assert_idle(device)


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_target_scan_retains_on_air_payload_and_typed_address(endpoint_pair, runtime):
    """Scan our peer and keep its payload bytes and address type intact."""
    advertiser, scanner = endpoint_pair
    evidence = runtime.evidence
    with evidence.step(f"{advertiser.name}: target-scan advertising lifetime and cleanup"):
        with advertiser.advertising():
            with evidence.step(f"{scanner.name}: discover {advertiser.name} for target scan"):
                peer = scanner.scan(2).expect(address=advertiser.mac_address())
            with evidence.step(f"{scanner.name}: capture and verify target-scan payload bytes"):
                reports = scanner.scan_target(peer, seconds=2)
                assert reports
                for report in reports:
                    assert report.address == peer.address
                    assert report.data.raw == report.payload
                    assert report.payload == bytes.fromhex(report.fields["data"])
                assert any(report.is_advertisement for report in reports)
                if advertiser.address_mode().address_type is BleAddressType.PUBLIC:
                    assert peer.address.address_type is BleAddressType.PUBLIC
    with evidence.step("Verify both endpoints idle after target scan"):
        for device in endpoint_pair:
            assert_idle(device)


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_assertion_failure_releases_connection_and_advertising(endpoint_pair, runtime):
    """Fail on purpose inside a connection and check both dongles return to idle."""
    advertiser, scanner = endpoint_pair
    with runtime.evidence.step("Exercise radio cleanup after an expected assertion failure"):
        # Catch inside the step: this expected failure is a successful acceptance check.
        with pytest.raises(AssertionError, match="deliberate scope failure"):
            with advertiser.advertising():
                peer = scanner.scan(2).expect(address=advertiser.mac_address())
                with scanner.connected(peer):
                    scanner.gatt(BleGatt.DEVICE_NAME).expect(name="BleuIO")
                    raise AssertionError("deliberate scope failure")
    with runtime.evidence.step("Verify both endpoints idle after expected assertion failure"):
        for device in endpoint_pair:
            assert_idle(device)


@pytest.mark.parametrize("reverse", [False, True], ids=["first-endpoint", "second-endpoint"])
def test_twenty_resets_preserve_identity_and_release_old_reader(endpoint_pair, runtime):
    """Reset a dongle 20 times; each time it must return without leaving an old reader."""
    device, neighbor = endpoint_pair
    with runtime.evidence.step(f"{device.name}: record USB identity and MAC before resets"):
        identity = device.resolve().serial_number
        address = device.mac_address()
    for cycle in range(20):
        label = f"{device.name}: reset {cycle + 1}/20"
        with runtime.evidence.step(label):
            old_readers = tuple(
                thread for thread in threads() if thread.name == f"bleuio-rx:{device.endpoint}"
            )
            assert len(old_readers) == 1
            device.reset()
            with runtime.evidence.step(
                f"{label}: verify identity, old reader and neighbor readiness"
            ):
                assert all(not thread.is_alive() for thread in old_readers)
                assert device.resolve().serial_number == identity
                assert device.mac_address() == address
                assert device.state.value == "ready"
                assert device.raw("ATI").replies
                assert neighbor.raw("ATI").replies
                assert_idle(device)


def test_bench_close_and_reopen_release_all_endpoint_workers(hardware_bench, runtime):
    """Close and reopen the bench; the same dongles should be ready with no old readers."""
    with runtime.evidence.step("Record endpoint identities and RX workers before closing bench"):
        readers = tuple(thread for thread in threads() if thread.name.startswith("bleuio-rx:"))
        addresses = {
            name: hardware_bench.device(name).mac_address() for name in hardware_bench.names()
        }
    with runtime.evidence.step("Close bench and verify all endpoint workers stopped"):
        hardware_bench.close()
        assert all(not thread.is_alive() for thread in readers)
        assert all(row["state"] == "closed" for row in hardware_bench.status().values())
    with runtime.evidence.step(
        "Reopen bench and verify identities, readiness and idle radio state"
    ):
        hardware_bench.open()
        assert all(row["state"] == "ready" for row in hardware_bench.status().values())
        for name, address in addresses.items():
            assert hardware_bench.device(name).mac_address() == address
            assert_idle(hardware_bench.device(name))
