import pytest

from autospan_ble.bleuio.payloads import (
    AdStructure,
    AdvertisingData,
    AdvertisingDataError,
    AdvertisingReport,
    AdvertisingReports,
)
from autospan_ble import BleAddress, ManufacturerData


# From BleuIO's official AT+SCANTARGET verbose example:
# flags + Smart Sensor Devices (0x075B) manufacturer data + 128-bit UUID.
OFFICIAL_ADV = bytes.fromhex("02010603FF5B071107B75C49D204A34071A0B535853EB08307000000000000")


def test_official_bleuio_advertising_example_is_split_into_ad_structures():
    data = AdvertisingData.parse(OFFICIAL_ADV)
    assert [item.ad_type for item in data.structures] == [0x01, 0xFF, 0x07]
    assert data.structures[0].data == b"\x06"
    (manufacturer,) = data.manufacturer_data()
    assert manufacturer.company_id == 0x075B
    assert manufacturer.payload == b""


def test_complete_local_name_is_decoded_and_zero_padding_terminates_payload():
    data = AdvertisingData.parse(bytes.fromhex("09094175746F5370616E000000"))
    assert data.local_name == "AutoSpan"


def test_malformed_ad_structure_is_rejected():
    with pytest.raises(AdvertisingDataError, match="declares"):
        AdvertisingData.parse(bytes.fromhex("05FF0102"))
    with pytest.raises(AdvertisingDataError, match="non-zero data follows"):
        AdvertisingData.parse(bytes.fromhex("0001"))


def test_ad_structure_round_trip_builder():
    built = AdvertisingData.build(
        AdStructure(0x01, b"\x06"),
        AdStructure(0x09, b"AutoSpan"),
    )
    assert AdvertisingData.parse(built.raw) == built


def test_payload_decoder_is_not_structwire_specific():
    class TinyCodec:
        def decode(self, payload):
            return {"size": len(payload)}

    data = AdvertisingData.build(AdStructure(0xFF, b"\x5b\x07abc"))
    (manufacturer,) = data.manufacturer_data(0x075B)
    assert manufacturer.decode(TinyCodec()) == {"size": 3}


def test_invalid_company_identifier_is_rejected():
    data = AdvertisingData.build(AdStructure(0xFF, b"\x5b\x07abc"))
    with pytest.raises(ValueError, match="company ID"):
        data.manufacturer_data(0x1_0000)


def test_advertising_reports_expect_keeps_target_scan_console_friendly():
    from autospan_ble.bleuio.address import BleAddress, BleAddressType
    from autospan_ble.bleuio.payloads import AdvertisingReport, AdvertisingReports

    address = BleAddress.parse("[0]12:34:56:78:9A:BC")
    reports = AdvertisingReports(
        (
            AdvertisingReport(
                address=address,
                data_type=0,
                payload=bytes.fromhex("02010603FF5B07"),
                data=AdvertisingData.parse(bytes.fromhex("02010603FF5B07")),
                rssi=-42,
                manufacturer_id=0x075B,
            ),
        )
    )
    assert reports.expect(manufacturer_id=0x075B).address.address_type is BleAddressType.PUBLIC
    parsed_only = AdvertisingReports(
        (
            AdvertisingReport(
                address=address,
                data_type=0,
                payload=bytes.fromhex("02010603FF5B07"),
                data=AdvertisingData.parse(bytes.fromhex("02010603FF5B07")),
                rssi=-42,
                manufacturer_id=None,
            ),
        )
    )
    assert parsed_only.expect(manufacturer_id=0x075B).address == address
    with pytest.raises(AssertionError, match="not observed"):
        reports.expect(local_name="missing")


def test_report_expect_combines_criteria_and_shows_requested_and_observed_payloads():
    address = BleAddress.parse("[0]12:34:56:78:9A:BC")
    ad = AdvertisingData.build(ManufacturerData(0x075B, b"\x01").to_ad_structure())
    response = AdvertisingData.build(AdStructure(0x09, b"Sensor:42"))
    reports = AdvertisingReports(
        (
            AdvertisingReport(address, 0, ad.raw, ad),
            AdvertisingReport(address, 4, response.raw, response),
        )
    )
    assert reports.expect(manufacturer_id=0x075B, data_type=0) is reports[0]
    assert reports.expect(data_type=4, local_name="Sensor:42") is reports[1]
    with pytest.raises(AssertionError) as error:
        reports.expect(manufacturer_id=0x075B, data_type=4)
    message = str(error.value)
    assert "manufacturer_id=0x075B, data_type=4" in message
    assert f"payload={ad.raw.hex()}" in message
    assert "Sensor:42" in message


@pytest.mark.parametrize(
    "criteria",
    [
        {"manufacturer_id": True},
        {"manufacturer_id": -1},
        {"manufacturer_id": 0x10000},
        {"data_type": True},
        {"data_type": -1},
        {"data_type": 256},
        {"local_name": 3},
    ],
)
def test_report_expect_rejects_invalid_criteria_without_observations(criteria):
    with pytest.raises(ValueError):
        AdvertisingReports().expect(**criteria)


def test_empty_report_expect_has_clear_failure():
    with pytest.raises(AssertionError, match="at least one report.*not observed") as error:
        AdvertisingReports().expect()
    assert "<none>" in str(error.value)
