"""Two BleuIO endpoints: discovery, connection and built-in Device Name read.

Fake scan/read rows are fixtures, not simulated RF or an independent peer model.
The dedicated sniffer scenario owns passive RF validation.
"""

import pytest

from autospan.api import Runtime
from autospan_ble import BleGatt
from autospan_ble.facade import BleBench


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_advertiser_is_observed(runtime: Runtime, reverse: bool) -> None:
    """Find the other dongle, connect, and read its name; then swap roles."""
    if BleBench.COMPONENT not in runtime.names():
        pytest.skip("requires a BLE bench configuration")
    bench = BleBench.from_runtime(runtime)
    if len(bench.names()) < 2:
        pytest.skip("discovery scenario requires two configured logical endpoints")

    names = bench.names()[:2]
    advertiser_name, scanner_name = names[::-1] if reverse else names
    advertiser = bench.device(advertiser_name)
    scanner = bench.device(scanner_name)

    with (
        runtime.evidence.step(f"{advertiser_name}: advertising lifetime and cleanup"),
        advertiser.advertising(),
    ):
        with runtime.evidence.step(f"{scanner_name}: discover {advertiser_name}"):
            peer = scanner.scan(3).expect(address=advertiser.mac_address())

        with (
            runtime.evidence.step(f"{scanner_name}: connection lifetime and disconnect"),
            scanner.connected(peer),
        ):
            with runtime.evidence.step(f"{scanner_name}: read built-in Device Name"):
                scanner.gatt(BleGatt.DEVICE_NAME).expect(name="BleuIO")
