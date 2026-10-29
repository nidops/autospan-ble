"""Common BLE fields use the installed StructWire YAML catalog."""

from importlib.resources import files

import pytest
from structwire import Codec, DecodeError

from autospan_ble import (
    BleCodec,
    codec,
    AdStructure,
    AdvertisingData,
    AdvertisingDataError,
    ManufacturerData,
)


OFFICIAL_ADV = bytes.fromhex("02010603FF5B071107B75C49D204A34071A0B535853EB08307000000000000")


def test_packaged_common_catalog_and_official_advertising_values():
    assert files("autospan_ble.bleuio").joinpath("common-codecs.yaml").is_file()
    assert all(isinstance(codec(item), Codec) for item in BleCodec)
    report = AdvertisingData.parse(OFFICIAL_ADV)
    assert report.flags == {
        "le_limited_discoverable": False,
        "le_general_discoverable": True,
        "br_edr_not_supported": True,
        "simultaneous_le_br_edr_controller": False,
        "unassigned_bits": 0,
    }
    assert report.service_uuids == ("0783b03e-8535-b5a0-7140-a304d2495cb7",)
    assert report.tx_power_dbm is None
    assert report.manufacturer_data()[0].company_id == 0x075B


def test_flags_yaml_preserves_every_first_octet_bit_including_unknown_positions():
    value_codec = codec(BleCodec.ADVERTISING_FLAGS)
    for raw in range(256):
        encoded = bytes([raw])
        values = value_codec.decode(encoded)
        assert values["unassigned_bits"] == raw >> 4
        assert value_codec.encode(values) == encoded


def test_absent_empty_and_extended_flags_keep_their_distinct_meanings():
    assert AdvertisingData.parse(b"").flags is None
    empty = AdvertisingData.build(AdStructure(0x01, b""))
    assert not any(empty.flags.values())
    extended = AdStructure(0x01, b"\x06\x80")
    assert extended.flags["br_edr_not_supported"] is True
    assert extended.data == b"\x06\x80"
    assert AdvertisingData.parse(extended.to_bytes()).structures[0] == extended


@pytest.mark.parametrize(
    "raw,expected", [(b"\x81", -127), (b"\xff", -1), (b"\x00", 0), (b"\x7f", 127)]
)
def test_common_tx_power_interprets_signed_values(raw, expected):
    report = AdvertisingData.build(AdStructure(0x0A, raw))
    assert report.tx_power_dbm == expected
    assert codec(BleCodec.TX_POWER).encode({"tx_power_dbm": expected}) == raw


@pytest.mark.parametrize("raw", [b"", b"\x00\x01", b"\x80"])
def test_invalid_tx_power_fails_semantic_interpretation_without_losing_raw_bytes(raw):
    report = AdvertisingData.build(AdStructure(0x0A, raw))
    assert report.structures[0].data == raw
    with pytest.raises(AdvertisingDataError):
        _ = report.tx_power_dbm


@pytest.mark.parametrize(
    "ad_type,raw,expected",
    [
        (
            2,
            "0f180018",
            ("0000180f-0000-1000-8000-00805f9b34fb", "00001800-0000-1000-8000-00805f9b34fb"),
        ),
        (3, "0f18", ("0000180f-0000-1000-8000-00805f9b34fb",)),
        (4, "78563412", ("12345678-0000-1000-8000-00805f9b34fb",)),
        (5, "78563412", ("12345678-0000-1000-8000-00805f9b34fb",)),
        (6, "ffeeddccbbaa99887766554433221100", ("00112233-4455-6677-8899-aabbccddeeff",)),
        (7, "b75c49d204a34071a0b535853eb08307", ("0783b03e-8535-b5a0-7140-a304d2495cb7",)),
    ],
)
def test_service_uuid_widths_and_byte_order(ad_type, raw, expected):
    item = AdStructure(ad_type, bytes.fromhex(raw))
    assert item.service_uuids == expected
    assert AdvertisingData.parse(item.to_bytes()).service_uuids == expected


@pytest.mark.parametrize("ad_type,width", [(2, 2), (3, 2), (4, 4), (5, 4), (6, 16), (7, 16)])
def test_uuid_lists_reject_partial_entries_and_allow_empty_lists(ad_type, width):
    assert AdStructure(ad_type, b"").service_uuids == ()
    with pytest.raises(AdvertisingDataError, match="whole"):
        _ = AdStructure(ad_type, bytes(width - 1)).service_uuids


def test_duplicate_standard_fields_remain_available_and_helpers_have_explicit_selection():
    data = AdvertisingData.build(
        AdStructure(1, b"\x06"),
        AdStructure(1, b"\x01"),
        AdStructure(0x0A, b"\xff"),
        AdStructure(0x0A, b"\x01"),
        AdStructure(2, bytes.fromhex("0f18")),
        AdStructure(3, bytes.fromhex("0f180018")),
    )
    assert data.flags["le_general_discoverable"] is True
    assert data.tx_power_dbm == -1
    assert len(data.by_type(1)) == 2
    assert data.service_uuids == (
        "0000180f-0000-1000-8000-00805f9b34fb",
        "00001800-0000-1000-8000-00805f9b34fb",
    )


@pytest.mark.parametrize("company_id,prefix", [(0, "0000"), (0x075B, "5b07"), (0xFFFF, "ffff")])
def test_manufacturer_builder_uses_common_little_endian_prefix(company_id, prefix):
    vendor = ManufacturerData(company_id, b"\x01\x80\xff")
    item = vendor.to_ad_structure()
    assert item.data == bytes.fromhex(prefix) + vendor.payload
    assert item.manufacturer == vendor
    assert AdvertisingData.parse(item.to_bytes()).manufacturer_data(company_id) == (vendor,)


def test_maximum_ad_structure_header_is_encodable_and_next_length_is_rejected():
    item = ManufacturerData(0xFFFF, bytes(252)).to_ad_structure()
    assert item.to_bytes()[:2] == b"\xff\xff"
    assert AdvertisingData.parse(item.to_bytes()).structures == (item,)
    with pytest.raises(ValueError, match="too long"):
        ManufacturerData(0xFFFF, bytes(253)).to_ad_structure()


def test_unknown_ad_type_is_opaque_and_known_codec_does_not_guess_its_meaning():
    item = AdStructure(0xEE, b"\x80\xff")
    assert item.flags is None and item.tx_power_dbm is None and item.service_uuids == ()
    assert AdvertisingData.parse(item.to_bytes()).structures == (item,)


def test_qualified_bleuio_device_name_uses_the_same_shared_engine():
    value_codec = codec(BleCodec.DEVICE_NAME)
    assert value_codec.decode(bytes.fromhex("426c6575494f")) == {"name": "BleuIO"}
    assert value_codec.encode({"name": "BleuIO"}) == b"BleuIO"


def test_codec_enum_matches_packaged_yaml_identifiers_exactly():
    from autospan_ble.bleuio.codecs import _CATALOG

    assert {item.value for item in BleCodec} == set(_CATALOG)


@pytest.mark.parametrize(
    "identifier,raw,values",
    [
        (BleCodec.AD_HEADER, "03ff", {"length": 3, "ad_type": 0xFF}),
        (BleCodec.COMPANY_ID, "5b07", {"company_id": 0x075B}),
        (
            BleCodec.ADVERTISING_FLAGS,
            "a6",
            {
                "le_limited_discoverable": False,
                "le_general_discoverable": True,
                "br_edr_not_supported": True,
                "simultaneous_le_br_edr_controller": False,
                "unassigned_bits": 10,
            },
        ),
        (BleCodec.TX_POWER, "ff", {"tx_power_dbm": -1}),
        (BleCodec.UUID16, "0f18", {"uuid": 0x180F}),
        (BleCodec.UUID32, "78563412", {"uuid": 0x12345678}),
        (
            BleCodec.UUID128,
            "ffeeddccbbaa99887766554433221100",
            {"uuid": bytes.fromhex("ffeeddccbbaa99887766554433221100")},
        ),
        (BleCodec.DEVICE_NAME, "426c6575494f", {"name": "BleuIO"}),
    ],
)
def test_every_builtin_codec_has_exact_wire_vectors_and_rejects_wrong_lengths(
    identifier, raw, values
):
    payload = bytes.fromhex(raw)
    value_codec = codec(identifier)
    assert codec(identifier.value) is value_codec
    assert value_codec.decode(payload) == values
    assert value_codec.encode(values) == payload
    for malformed in (payload[:-1], payload + b"\x00"):
        with pytest.raises(DecodeError):
            value_codec.decode(malformed)


def test_unknown_builtin_codec_has_an_actionable_identifier_error():
    with pytest.raises(KeyError, match="unknown BLE codec identifier.*sensor_telemetry"):
        codec("sensor_telemetry")
