"""Known-peer RF acceptance, kept separate from synthetic PCAP unit fixtures."""

from threading import enumerate as threads

import pytest

from autospan_ble.checks import check_sniffer

pytestmark = pytest.mark.hardware


def observer_name(bench):
    if not bench.observer_names():
        pytest.skip("optional passive capture requires a configured Nordic observer")
    return bench.observer_names()[0]


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_capture_contains_crc_valid_packets_from_known_advertiser(hardware_bench, runtime, reverse):
    """Capture our advertising dongle and check that its packets have valid CRCs."""
    names = hardware_bench.names()[:2]
    result = check_sniffer(
        hardware_bench,
        runtime.evidence,
        observer_name(hardware_bench),
        *(names[::-1] if reverse else names),
    )
    with runtime.evidence.step("Verify capture packet counts agree with known-peer CRC check"):
        assert result.advertiser_packets > 0
        assert result.packet_count >= result.advertiser_packets


def test_twenty_capture_windows_release_workers_and_allow_endpoint_commands(
    hardware_bench, runtime
):
    """Repeat 20 captures while using both dongles, checking each capture closes cleanly."""
    name = observer_name(hardware_bench)
    advertiser = hardware_bench.device(hardware_bench.names()[0])
    neighbor = hardware_bench.device(hardware_bench.names()[1])
    for cycle in range(20):
        label = f"{name}: capture {cycle + 1}/20"
        with runtime.evidence.step(label):
            with hardware_bench.capture(name, runtime.evidence) as session:
                with advertiser.advertising():
                    with runtime.evidence.step(f"{label}: wait for RF packets"):
                        session.wait_for_packets()
                    with runtime.evidence.step(f"{label}: verify independent endpoint commands"):
                        assert advertiser.raw("ATI").replies
                        assert neighbor.raw("ATI").replies
            with runtime.evidence.step(
                f"{label}: verify packets, observer readiness and worker stop"
            ):
                assert session.result is not None and session.result.packet_count > 0
                assert hardware_bench.observer(name).state.value == "ready"
                assert not any(thread.name == f"nrf-capture-{name}" for thread in threads())
