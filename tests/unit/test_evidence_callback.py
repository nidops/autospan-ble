from autospan_ble.bleuio.device import BleuIODevice
from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
from autospan_ble.simulation.fake import (
    FakeBleuIOEnvironment,
    FakeLineTransport,
    FakePortEnumerator,
)


def test_device_emits_logical_identity_not_only_endpoint():
    captured = []
    env = FakeBleuIOEnvironment(endpoint="fake://ble", serial_number="X")
    device = BleuIODevice(
        "logical_ble",
        SerialDeviceSelector(serial_number="X"),
        SerialResolver(FakePortEnumerator(env)),
        lambda endpoint: FakeLineTransport(env, endpoint),
        evidence=lambda kind, fields: captured.append((kind, dict(fields))),
    )
    device.open()
    device.close()
    assert any(fields.get("device") == "logical_ble" for _, fields in captured)
    assert any(kind == "state" and fields.get("state") == "ready" for kind, fields in captured)
