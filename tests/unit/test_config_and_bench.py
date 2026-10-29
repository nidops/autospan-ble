import pytest

from autospan_ble.config import parse_config
from autospan_ble.facade import BleBench
from autospan_ble.bleuio.device import BleuIODevice
from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
from autospan_ble.simulation.fake import (
    FakeBleuIOEnvironment,
    FakeLineTransport,
    FakePortEnumerator,
)


def test_duplicate_physical_claim_is_rejected():
    cfg = {
        "devices": {
            "one": {"backend": "fake", "selector": {"serial_number": "SAME"}},
            "two": {"backend": "fake", "selector": {"serial_number": "SAME"}},
        }
    }
    with pytest.raises(ValueError, match="claimed more than once"):
        parse_config(cfg)


def test_arbitrary_device_count_is_registry_data_not_branching():
    devices = {}
    for index in range(5):
        name = f"ble_{index}"
        env = FakeBleuIOEnvironment(endpoint=f"fake://{name}", serial_number=name)
        devices[name] = BleuIODevice(
            name,
            SerialDeviceSelector(serial_number=name),
            SerialResolver(FakePortEnumerator(env)),
            lambda endpoint, env=env: FakeLineTransport(env, endpoint),
        )
    bench = BleBench(devices)
    bench.open()
    try:
        assert bench.names() == tuple(f"ble_{index}" for index in range(5))
        assert all(item["state"] == "ready" for item in bench.status().values())
    finally:
        bench.close()
