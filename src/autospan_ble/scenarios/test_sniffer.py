"""Explicit firmware maintenance and an independent RF capture assertion."""

import pytest

from autospan.api import Runtime
from autospan_ble.checks import check_sniffer
from autospan_ble.facade import BleBench
from autospan_ble.plugin import COMPONENT, SETUP_COMPONENT
from autospan_ble.raytac.setup import SnifferSetup


@pytest.mark.destructive
def test_sniffer_setup(runtime: Runtime) -> None:
    """Prepare the sniffer firmware and check it is ready for captures (maintenance only)."""
    if SETUP_COMPONENT not in runtime.names():
        pytest.skip("requires a separate sniffer_setup maintenance configuration")
    with runtime.evidence.step("Prepare sniffer and verify application firmware and endpoint"):
        result = runtime.require(SETUP_COMPONENT, SnifferSetup).prepare(runtime.evidence)
        assert result.firmware_version
        assert result.endpoint


@pytest.mark.hardware
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_sniffer_captures_known_advertiser(runtime: Runtime, reverse: bool) -> None:
    """Listen on air and check that valid packets came from our advertising dongle."""
    if COMPONENT not in runtime.names():
        pytest.skip("requires a BLE bench configuration")
    bench = runtime.require(COMPONENT, BleBench)
    if not bench.observer_names() or len(bench.names()) < 2:
        pytest.skip("requires a passive observer and two BLE endpoints")
    names = bench.names()[:2]
    result = check_sniffer(
        bench, runtime.evidence, bench.observer_names()[0], *(names[::-1] if reverse else names)
    )
    with runtime.evidence.step("Verify known advertiser has CRC-valid packets"):
        assert result.advertiser_packets > 0
