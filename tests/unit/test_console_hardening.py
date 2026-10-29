"""Plugin API 1 console integration with the fake BLE device backend."""

from __future__ import annotations

import json
import asyncio
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from autospan.api import AUTOSPAN_PLUGIN_API_VERSION, CommandShell, GateError, Runtime
from autospan._core.evidence_store import LocalEvidenceStore
from autospan._core.host import build_host
from autospan.frontends.console import run_console
from autospan_ble.facade import BleBench
from autospan_ble.plugin import PLUGIN


def fake_host():
    config_path = Path(__file__).resolve().parents[2] / "autospan.fake.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return build_host(plugin_names=("ble",), config=config, config_dir=config_path.parent)


def test_fake_console_status_scan_reset_and_correlated_evidence(tmp_path, capsys):
    assert PLUGIN.descriptor.api_version == AUTOSPAN_PLUGIN_API_VERSION == 1
    host = fake_host()
    code = run_console(
        host=host,
        evidence_root=tmp_path,
        commands=("ble status", "ble scan scanner 1", "ble reset scanner"),
        plain=True,
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "scanner:" in output and "rssi=" in output and " -> " in output
    (manifest,) = tmp_path.glob("*/manifest.json")
    assert json.loads(manifest.read_text())["outcome"] == "passed"
    events = [
        json.loads(line) for line in manifest.with_name("events.jsonl").read_text().splitlines()
    ]
    finished = [e for e in events if e["name"] == "step_finished"]
    assert [e["attributes"]["step"] for e in finished] == [
        "console: ble status",
        "console: ble scan",
        "console: ble reset",
    ]
    assert all(e["attributes"]["outcome"] == "passed" for e in finished)
    scans = [e for e in events if e["attributes"].get("event") == "scan_complete"]
    assert scans and all(e["step_id"] == "console: ble scan" for e in scans)


@pytest.mark.parametrize(
    "command",
    ["ble capture missing 0.01", "ble sniffer setup", "ble sniffer check missing one two"],
)
def test_observer_and_setup_commands_report_missing_prerequisites(tmp_path, command):
    # This is a prerequisite-failure test, not fake RF/sniffer qualification.
    assert (
        run_console(
            host=fake_host(),
            evidence_root=tmp_path,
            commands=(command, "ble status"),
            keep_going=True,
            plain=True,
        )
        == 2
    )
    (manifest,) = tmp_path.glob("*/manifest.json")
    assert json.loads(manifest.read_text())["outcome"] == "failed"


def test_capture_acknowledges_cancellation_only_after_capture_context_closes(tmp_path, monkeypatch):
    bench = BleBench({})
    started = threading.Event()
    closed = []

    @contextmanager
    def capture(name, evidence):
        try:
            started.set()
            yield SimpleNamespace(result=SimpleNamespace(packet_count=0, byte_count=24))
        finally:
            closed.append(True)

    monkeypatch.setattr(bench, "capture", capture)
    host = fake_host()
    command = next(c for c in host.commands if c.path == ("ble", "capture"))
    evidence = LocalEvidenceStore(tmp_path)
    shell = CommandShell(Runtime({BleBench.COMPONENT: bench}, evidence), (command,))
    outcomes = []
    thread = threading.Thread(
        target=lambda: outcomes.append(shell.execute("ble capture air 30", capture=True))
    )
    thread.start()
    try:
        assert started.wait(3)
        shell.cancel()
        thread.join(3)
        assert not thread.is_alive()
        assert closed == [True] and outcomes[0].status == 130
    finally:
        shell.cancel()
        thread.join(3)
        evidence.close(outcome="failed")


def test_console_hardware_gate_blocks_physical_enumeration(tmp_path, monkeypatch):
    from autospan_ble.bleuio.selector import PySerialPortEnumerator

    def forbidden(self):
        raise AssertionError("hardware enumeration occurred before the gate")

    monkeypatch.setattr(PySerialPortEnumerator, "ports", forbidden)
    host = build_host(
        plugin_names=("ble",),
        config={
            "ble": {"devices": {"one": {"selector": {"serial_number": "ONE"}}}},
        },
        config_dir=tmp_path,
    )
    with pytest.raises(GateError, match="hardware"):
        run_console(host=host, evidence_root=tmp_path, commands=("ble status",), plain=True)
    (manifest,) = tmp_path.glob("*/manifest.json")
    assert json.loads(manifest.read_text())["outcome"] == "failed"


def test_fake_ble_commands_in_textual_use_same_runtime(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from autospan.frontends.tui.app import ConsoleApp, PromptInput

    def run(app):
        async def scenario():
            async with app.run_test() as pilot:
                for line in ("ble status", "ble scan scanner 1", "ble reset scanner"):
                    app.query_one(PromptInput).value = line
                    await pilot.press("enter")
                    async with asyncio.timeout(5):
                        while app.operation_state != "idle":
                            await pilot.pause()
                            await asyncio.sleep(0.005)
                assert "scanner:" in app.retained_log and "rssi=" in app.retained_log
                await pilot.press("ctrl+q")
            return app.return_code

        return asyncio.run(scenario())

    monkeypatch.setattr("autospan.frontends.console._tui_available", lambda: True)
    monkeypatch.setattr(ConsoleApp, "run", run)
    assert run_console(host=fake_host(), evidence_root=tmp_path) == 0
    (manifest,) = tmp_path.glob("*/manifest.json")
    assert json.loads(manifest.read_text())["outcome"] == "passed"
