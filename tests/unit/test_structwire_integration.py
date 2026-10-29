from structwire import Field, Layout

from autospan_ble.bleuio.payloads import AdvertisingData, ManufacturerData


def test_manufacturer_payload_can_be_decoded_by_structwire_without_ble_coupling():
    codec = Layout(
        Field("version", "u8"),
        Field("sensor_id", "u16le"),
        Field("temperature_c", "i16le", scale=0.01),
        Field("battery_pct", "u8"),
        name="sensor-advertisement",
    ).compile()
    application = codec.encode(
        {"version": 1, "sensor_id": 42, "temperature_c": 23.75, "battery_pct": 91}
    )
    ad = AdvertisingData.build(ManufacturerData(0x075B, application).to_ad_structure())
    (manufacturer,) = ad.manufacturer_data(0x075B)
    assert manufacturer.decode(codec) == {
        "version": 1,
        "sensor_id": 42,
        "temperature_c": 23.75,
        "battery_pct": 91,
    }


def test_gatt_value_can_be_decoded_by_structwire_through_device_api():
    from autospan_ble.bleuio.device import BleuIODevice
    from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
    from autospan_ble.simulation.fake import (
        FakeBleuIOEnvironment,
        FakeLineTransport,
        FakePortEnumerator,
    )

    env = FakeBleuIOEnvironment(endpoint="fake://gatt-codec", serial_number="CODEC")
    device = BleuIODevice(
        "codec-peer",
        SerialDeviceSelector(serial_number="CODEC"),
        SerialResolver(FakePortEnumerator(env)),
        lambda endpoint: FakeLineTransport(env, endpoint),
    )
    codec = Layout(Field("name", "ascii", length=6), name="device-name").compile()

    device.open()
    try:
        with device.connected("12:34:56:78:9A:BC"):
            assert device.read_gatt(3) == b"BleuIO"
            assert device.read_gatt_decoded(3, codec) == {"name": "BleuIO"}
            value = device.gatt(3, codec=codec)
            assert value.expect(name="BleuIO").decoded == {"name": "BleuIO"}
    finally:
        device.close()
