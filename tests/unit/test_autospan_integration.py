import json
from pathlib import Path
import subprocess
import sys

import pytest

from autospan.api import GateError
from autospan._core.evidence_store import LocalEvidenceStore
from autospan._core.host import build_host
from autospan._core.runtime import RuntimeSession
from autospan_ble.facade import BleBench


def fake_host():
    config = {
        "ble": {
            "devices": {
                "one": {
                    "backend": "fake",
                    "selector": {"serial_number": "ONE"},
                    "fake_scan_results": [{"addr": "[0]12:34:56:78:9A:BC", "rssi": -42}],
                }
            }
        }
    }
    return build_host(plugin_names=["ble"], config=config, config_dir=Path.cwd())


@pytest.mark.parametrize("vid", [None, 0, 65535])
def test_fake_bench_preserves_explicit_usb_vendor_id(tmp_path, vid):
    host = build_host(
        plugin_names=["ble"],
        config={
            "ble": {
                "devices": {
                    "one": {
                        "backend": "fake",
                        "selector": {"serial_number": "ONE", "vid": vid},
                    }
                }
            }
        },
        config_dir=tmp_path,
    )
    evidence = LocalEvidenceStore(tmp_path)
    try:
        with RuntimeSession(plan=host.plan, factories=host.factories, evidence=evidence) as runtime:
            device = BleBench.from_runtime(runtime).device("one")
            assert device.resolve().vid == (0x2DCF if vid is None else vid)
            assert device.raw("ATI").replies
    finally:
        evidence.close()


def test_operation_evidence_has_step_but_raw_rx_is_session_artifact(tmp_path):
    host = fake_host()
    evidence = LocalEvidenceStore(tmp_path)
    with evidence.scope(test_id="test-correlation"):
        with RuntimeSession(plan=host.plan, factories=host.factories, evidence=evidence) as runtime:
            device = runtime.require("ble.bench", BleBench).device("one")
            with evidence.step("scan"):
                assert device.scan(seconds=1)[0].rssi == -42
    evidence.close(outcome="passed")
    records = [
        json.loads(line) for line in (evidence.root / "events.jsonl").read_text().splitlines()
    ]
    commands = [
        row
        for row in records
        if row["attributes"].get("event") == "serial_command_completed"
        and row["attributes"].get("command") == "AT+GAPSCAN=1"
    ]
    assert len(commands) == 1
    assert commands[0]["test_id"] == "test-correlation"
    assert commands[0]["step_id"] == "scan"
    assert commands[0]["source"] == "ble.one"
    assert not any(row["attributes"].get("event") == "serial_rx" for row in records)
    (artifact,) = evidence.artifacts()
    assert artifact.context.test_id is None
    assert artifact.context.step_id is None
    assert artifact.complete
    raw = [json.loads(line) for line in artifact.path.read_text().splitlines()]
    assert any(row["event"] == "serial_rx" and '"S"' in row.get("line", "") for row in raw)
    assert all(row["logical_device"] == "one" for row in raw)


def test_hardware_gate_precedes_even_physical_resolution(tmp_path, monkeypatch):
    from autospan_ble.bleuio.selector import PySerialPortEnumerator

    def forbidden(self):
        raise AssertionError("gate allowed hardware enumeration")

    monkeypatch.setattr(PySerialPortEnumerator, "ports", forbidden)
    host = build_host(
        plugin_names=["ble"],
        config={
            "ble": {
                "devices": {
                    "one": {"selector": {"serial_number": "ONE"}},
                }
            }
        },
        config_dir=tmp_path,
    )
    evidence = LocalEvidenceStore(tmp_path)
    try:
        with pytest.raises(GateError):
            with RuntimeSession(
                plan=host.plan, factories=host.factories, evidence=evidence, enabled_gates=[]
            ):
                pass
    finally:
        evidence.close()


def test_shared_bench_retains_final_teardown_failure_in_core_evidence(tmp_path, installed_plugin):
    """The host keeps a shared BLE owner alive for finalizer reports."""
    package, command = installed_plugin("ble-regression")
    scenario = package / "test_shared_ble.py"
    scenario.write_text(
        "import pytest\n"
        "from autospan_ble import BleBench\n"
        "@pytest.fixture(scope='session')\n"
        "def bench_with_failing_teardown(session_runtime):\n"
        "    bench = BleBench.from_runtime(session_runtime)\n"
        "    yield bench\n"
        "    assert bench.device('scanner').raw('ATI').replies\n"
        "    raise RuntimeError('final BLE fixture failure')\n"
        "def test_shared_ble(bench_with_failing_teardown):\n"
        "    assert bench_with_failing_teardown.device('scanner').raw('ATI').replies\n"
    )
    evidence_dir = tmp_path / "evidence"
    config = Path(__file__).resolve().parents[2] / "autospan.fake.yaml"
    result = subprocess.run(
        [
            *command,
            "test",
            "--config",
            str(config),
            "--set",
            "autospan.plugins=[ble, ble-regression]",
            "--evidence-dir",
            str(evidence_dir),
            "--",
            str(scenario),
            "-k",
            "test_shared_ble",
            "-q",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "final BLE fixture failure" in result.stdout
    (manifest,) = evidence_dir.glob("*/manifest.json")
    assert json.loads(manifest.read_text())["outcome"] == "failed"
    events = [
        json.loads(line) for line in manifest.with_name("events.jsonl").read_text().splitlines()
    ]
    (failed_teardown,) = (
        event
        for event in events
        if event["name"] == "pytest_phase_finished"
        and event["attributes"]["phase"] == "teardown"
        and event["attributes"]["outcome"] == "failed"
    )
    closed = [event for event in events if event["name"] == "ble_bench_closed"]
    assert len(closed) == 1
    assert events.index(failed_teardown) < events.index(closed[0])
    assert any(
        event["name"] == "runtime_stopping" and event["attributes"].get("outcome") == "failed"
        for event in events
    )


def test_installed_plugin_contributes_fake_scenarios(tmp_path):
    config = Path(__file__).resolve().parents[2] / "autospan.fake.yaml"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "autospan",
            "test",
            "--config",
            str(config),
            "--evidence-dir",
            str(tmp_path),
            "--",
            "-q",
        ],
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    (manifest,) = tmp_path.glob("*/manifest.json")
    metadata = json.loads(manifest.read_text())
    assert metadata["outcome"] == "passed"


@pytest.mark.parametrize("reverse", [False, True])
def test_discovery_keeps_peer_advertising_until_connection_and_cleans_up(
    tmp_path, monkeypatch, reverse
):
    import yaml
    from autospan_ble.scenarios.test_discovery import test_advertiser_is_observed

    config = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "autospan.fake.yaml").read_text()
    )
    host = build_host(plugin_names=["ble"], config=config, config_dir=tmp_path)
    evidence = LocalEvidenceStore(tmp_path)
    try:
        with RuntimeSession(plan=host.plan, factories=host.factories, evidence=evidence) as runtime:
            bench = BleBench.from_runtime(runtime)
            assert bench is runtime.require(BleBench.COMPONENT, BleBench)
            names = bench.names()[:2]
            advertiser, scanner = (
                bench.device(name) for name in (names[::-1] if reverse else names)
            )
            connect = scanner.connect
            connected_while_advertising = []

            def require_advertising(address, **kwargs):
                assert any(
                    row.get("advertising") is True for row in advertiser.raw("AT+GAPSTATUS").replies
                )
                connected_while_advertising.append(address)
                return connect(address, **kwargs)

            monkeypatch.setattr(scanner, "connect", require_advertising)
            test_advertiser_is_observed(runtime, reverse)
            assert len(connected_while_advertising) == 1
            for name in names:
                status = bench.device(name).raw("AT+GAPSTATUS")
                assert any(row.get("advertising") is False for row in status.replies)
                assert any(row.get("connected") is False for row in status.replies)
    finally:
        evidence.close()
