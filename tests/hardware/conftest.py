"""Hardware owners come from AutoSpan; no physical identities are embedded here."""

from pathlib import Path
from threading import enumerate as threads
from os.path import realpath

import pytest
import yaml

from autospan_ble import BleBench
from autospan_ble.config import parse_config


@pytest.fixture
def hardware_bench(runtime, autospan_config_path):
    with runtime.evidence.step("Validate physical BLE bench configuration"):
        if autospan_config_path is None:
            pytest.fail("hardware acceptance requires a physical bench configuration file")
        config = yaml.safe_load(Path(autospan_config_path).read_text())
        parsed = parse_config(config["ble"])
        if any(device.backend != "pyserial" for device in parsed.devices):
            pytest.fail("hardware acceptance requires physical endpoints, not fake backends")
        bench = BleBench.from_runtime(runtime)
        if len(bench.names()) < 2:
            pytest.fail("hardware acceptance requires at least two physical BLE endpoints")
    yield bench
    with runtime.evidence.step(
        "Close BLE bench and verify workers and serial descriptors released"
    ):
        owners = tuple(
            thread for thread in threads() if thread.name.startswith(("bleuio-rx:", "nrf-capture-"))
        )
        endpoints = {
            realpath(row["endpoint"]) for row in bench.status().values() if row["endpoint"]
        }
        bench.close()
        assert all(not thread.is_alive() for thread in owners)
        assert all(status["state"] == "closed" for status in bench.status().values())
        # Linux acceptance additionally proves that the process released its tty descriptors.
        descriptors = Path("/proc/self/fd")
        if descriptors.is_dir():
            open_endpoints = set()
            for descriptor in descriptors.iterdir():
                try:
                    open_endpoints.add(realpath(descriptor.readlink()))
                except FileNotFoundError:
                    continue
            assert not endpoints & open_endpoints


@pytest.fixture
def endpoint_pair(hardware_bench, reverse):
    names = hardware_bench.names()[:2]
    return tuple(hardware_bench.device(name) for name in (names[::-1] if reverse else names))
