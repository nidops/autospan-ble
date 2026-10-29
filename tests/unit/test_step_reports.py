"""Step/report acceptance uses the real core evidence store and fake endpoint I/O."""

import importlib.util
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import quote, unquote

import pytest
import yaml

from autospan._core.evidence_store import LocalEvidenceStore
from autospan._core.host import build_host
from autospan._core.runtime import RuntimeSession
from autospan_ble import BleBench


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reporter = load("ble_step_report_validation", ROOT / "scripts/report_steps.py")
endpoints = load("ble_hardware_steps_validation", ROOT / "tests/hardware/test_endpoints.py")
observers = load("ble_hardware_observer_steps_validation", ROOT / "tests/hardware/test_observer.py")
synthetic_observer_factory = load(
    "ble_step_report_sniffer_fixture", ROOT / "tests/unit/test_raytac_observer.py"
).sniffer


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    ("operation", "step_count"),
    [
        ("test_discovery_connection_and_project_gatt_codec", 7),
        ("test_target_scan_retains_on_air_payload_and_typed_address", 4),
        ("test_assertion_failure_releases_connection_and_advertising", 2),
        ("test_twenty_resets_preserve_identity_and_release_old_reader", 41),
        ("test_bench_close_and_reopen_release_all_endpoint_workers", 3),
    ],
)
def test_hardware_endpoint_phases_produce_passing_correlated_step_reports(
    tmp_path, operation, step_count, reverse
):
    config = yaml.safe_load((ROOT / "autospan.fake.yaml").read_text())
    for device in config["ble"]["devices"].values():
        for row in device["fake_scan_results"]:
            row.update(data="020106", type=0)
    host = build_host(plugin_names=["ble"], config=config, config_dir=ROOT)
    evidence = LocalEvidenceStore(tmp_path)
    test_id = f"{operation}[{'reverse' if reverse else 'forward'}]"
    with evidence.scope(test_id=test_id):
        with RuntimeSession(plan=host.plan, factories=host.factories, evidence=evidence) as runtime:
            bench = BleBench.from_runtime(runtime)
            if operation == "test_bench_close_and_reopen_release_all_endpoint_workers":
                getattr(endpoints, operation)(bench, runtime)
            else:
                names = bench.names()[:2]
                pair = tuple(bench.device(name) for name in (names[::-1] if reverse else names))
                getattr(endpoints, operation)(pair, runtime)
        evidence.event("pytest_phase_finished", phase="call", outcome="passed")
    evidence.close(outcome="passed")
    events = [
        json.loads(line) for line in (evidence.root / "events.jsonl").read_text().splitlines()
    ]
    started = [row for row in events if row["name"] == "step_started"]
    finished = [row for row in events if row["name"] == "step_finished"]
    assert len(started) == len(finished) == step_count
    assert all(row["test_id"] == test_id for row in started + finished)
    assert all(row["step_id"] == row["attributes"]["step"] for row in started + finished)
    assert all(row["attributes"]["outcome"] == "passed" for row in finished)
    commands = [
        row for row in events if row["attributes"].get("event") == "serial_command_completed"
    ]
    assert any(row["step_id"] for row in commands)
    markdown = reporter.render_report(evidence.root)
    assert test_id in markdown and "1 passed." in markdown
    assert "FAILED" not in markdown and "INCOMPLETE" not in markdown


@pytest.mark.skipif(os.name != "posix", reason="capture backend requires POSIX")
@pytest.mark.parametrize(
    "operation", ["known-peer-forward", "known-peer-reverse", "capture-endurance"]
)
def test_hardware_observer_phases_correlate_capture_links_and_every_cycle(
    synthetic_observer_factory, tmp_path, monkeypatch, operation
):
    import autospan_ble.checks as checks
    import autospan_ble.plugin as plugin

    observer, _ = synthetic_observer_factory()
    monkeypatch.setattr(plugin, "_build_observer", lambda config: observer)
    monkeypatch.setattr(checks, "_advertiser_packets", lambda path, address, command: 1)
    config = yaml.safe_load((ROOT / "autospan.fake.yaml").read_text())
    config["ble"]["observers"] = {"air": {"selector": {"serial_number": "OBSERVER-TEST"}}}
    host = build_host(plugin_names=["ble"], config=config, config_dir=ROOT)
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    with evidence.scope(test_id=operation):
        with RuntimeSession(
            plan=host.plan, factories=host.factories, evidence=evidence, enabled_gates=["hardware"]
        ) as runtime:
            bench = BleBench.from_runtime(runtime)
            if operation == "capture-endurance":
                observers.test_twenty_capture_windows_release_workers_and_allow_endpoint_commands(
                    bench, runtime
                )
            else:
                observers.test_capture_contains_crc_valid_packets_from_known_advertiser(
                    bench, runtime, operation.endswith("reverse")
                )
        evidence.event("pytest_phase_finished", phase="call", outcome="passed")
    evidence.close(outcome="passed")
    events = [
        json.loads(line) for line in (evidence.root / "events.jsonl").read_text().splitlines()
    ]
    finished = [row for row in events if row["name"] == "step_finished"]
    assert len(finished) == (80 if operation == "capture-endurance" else 8)
    assert all(row["test_id"] == operation and row["step_id"] for row in finished)
    assert all(row["attributes"]["outcome"] == "passed" for row in finished)
    markdown = reporter.render_report(evidence.root)
    assert "1 passed." in markdown and "FAILED" not in markdown
    captures = [artifact for artifact in evidence.artifacts() if artifact.kind == "capture"]
    assert len(captures) == (20 if operation == "capture-endurance" else 1)
    assert all(artifact.path.name in markdown for artifact in captures)


def test_report_retains_nested_failure_duration_artifact_links_and_session_logs(tmp_path):
    evidence = LocalEvidenceStore(tmp_path)
    with evidence.scope(test_id="test_capture[forward]"):
        with pytest.raises(AssertionError):
            with evidence.step("Capture known peer"):
                with evidence.reserve_artifact(
                    kind="capture", source="ble.air", suffix=".pcap"
                ) as capture:
                    capture.path.write_bytes(b"pcap-bytes")
                    with evidence.step("Check CRC | known peer"):
                        raise AssertionError("CRC <invalid>\nno known peer")
        evidence.event("pytest_phase_finished", phase="call", outcome="failed")
    with evidence.reserve_artifact(
        kind="protocol", source="ble.scanner", suffix=".jsonl"
    ) as serial:
        serial.path.write_text("raw RX\n")
    evidence.close(outcome="failed")
    output = tmp_path / "report.md"
    markdown = reporter.render_report(evidence.root, output=output)
    assert "Final verdict: **FAILED**" in markdown and "1 failed." in markdown
    assert "| Capture known peer | FAILED |" in markdown
    assert "↳ Check CRC \\| known peer" in markdown
    assert "CRC &lt;invalid&gt;" in markdown
    assert "(incomplete, 10 B)" in markdown
    assert "## Session artifacts" in markdown
    assert "no causal test/step attribution" in markdown
    events = [
        json.loads(line) for line in (evidence.root / "events.jsonl").read_text().splitlines()
    ]
    for label in ("Capture known peer", "Check CRC | known peer"):
        started = next(
            row
            for row in events
            if row["name"] == "step_started" and row["attributes"]["step"] == label
        )
        finished = next(
            row
            for row in events
            if row["name"] == "step_finished" and row["attributes"]["step"] == label
        )
        assert f"{(finished['monotonic_ns'] - started['monotonic_ns']) / 1e9:.3f}" in markdown
    for artifact in evidence.artifacts():
        assert artifact.path.name in markdown


@pytest.mark.parametrize("exported", [False, True])
@pytest.mark.parametrize("windows_paths", [False, True])
def test_repeated_step_labels_link_each_capture_only_to_its_own_occurrence(
    tmp_path, exported, windows_paths, monkeypatch
):
    if windows_paths:
        # Exercise Windows relative paths on every CI host, including Linux.
        monkeypatch.setattr(reporter, "os", SimpleNamespace(path=ntpath))
        monkeypatch.setattr(reporter, "Path", PureWindowsPath)
    evidence = LocalEvidenceStore(tmp_path)
    with evidence.scope(test_id="test_repeated"):
        for _ in range(2):
            with evidence.step("Capture"):
                with evidence.reserve_artifact(
                    kind="capture", source="ble.air", suffix=".pcap"
                ) as artifact:
                    artifact.path.write_bytes(b"pcap")
        evidence.event("pytest_phase_finished", phase="call", outcome="passed")
    evidence.close(outcome="passed")
    output = tmp_path / "reports with spaces" / "report.md" if exported else None
    rows = [
        line
        for line in reporter.render_report(evidence.root, output=output).splitlines()
        if line.startswith("| Capture |")
    ]
    assert len(rows) == 2
    for row, artifact in zip(rows, evidence.artifacts(), strict=True):
        assert artifact.path.name in row
        relative = Path(os.path.relpath(artifact.path, output.parent if output else evidence.root))
        assert row.count(f"]({quote(relative.as_posix(), safe='/')})") == 1
        target = row.split("](", 1)[1].split(")", 1)[0]
        assert "\\" not in unquote(target)
        assert (output.parent if output else evidence.root).joinpath(unquote(target)).resolve() == (
            artifact.path.resolve()
        )


def test_report_marks_unfinished_steps_incomplete_and_keeps_teardown_failures(tmp_path):
    evidence = LocalEvidenceStore(tmp_path)
    with evidence.scope(test_id="test_unfinished"):
        evidence.event("step_started", step="Wait for packet")
        evidence.event("pytest_phase_finished", phase="call", outcome="passed")
    with evidence.scope(test_id="test_bad_teardown"):
        with evidence.step("Read GATT"):
            pass
        evidence.event("pytest_phase_finished", phase="call", outcome="passed")
        evidence.event("pytest_phase_finished", phase="teardown", outcome="failed")
    with evidence.scope(test_id="test_gated"):
        evidence.event("pytest_phase_finished", phase="setup", outcome="skipped")
    evidence.close(outcome="failed")
    markdown = reporter.render_report(evidence.root)
    assert "1 failed, 1 incomplete, 1 skipped." in markdown
    assert "| Wait for packet | INCOMPLETE | — |" in markdown
    assert "test_bad_teardown\n\nResult: **FAILED**" in markdown
    assert "test_gated\n\nResult: **SKIPPED**.\n\nNo steps recorded." in markdown


def test_hosted_failed_discovery_renders_actual_pytest_verdict_and_named_error(
    tmp_path, installed_plugin
):
    package, command = installed_plugin("ble-regression")
    scenario = package / "test_report_failure.py"
    scenario.write_text(
        "from autospan_ble import BleBench\n"
        "def test_missing_peer(runtime):\n"
        "    with runtime.evidence.step('Discover required peer'):\n"
        "        bench = BleBench.from_runtime(runtime)\n"
        "        bench.device('scanner').scan(1).expect(address='00:11:22:33:44:55')\n"
    )
    output = tmp_path / "evidence"
    html_report = tmp_path / "failure.html"
    run = subprocess.run(
        [
            *command,
            "test",
            "--config",
            str(ROOT / "autospan.fake.yaml"),
            "--set",
            "autospan.plugins=[ble, ble-regression]",
            "--evidence-dir",
            str(output),
            "--",
            str(scenario),
            "-k",
            "test_missing_peer",
            "-q",
            "-p",
            "metadata",
            "-p",
            "html",
            f"--html={html_report}",
            "--self-contained-html",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    html = html_report.read_text()
    assert "test_missing_peer" in html and "00:11:22:33:44:55" in html
    assert 'href="assets/style.css"' not in html
    (manifest,) = output.glob("*/manifest.json")
    report = tmp_path / "failure.md"
    cli = subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / "scripts/report_steps.py"),
            str(manifest.parent),
            "--output",
            str(report),
        ],
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert cli.returncode == 0, cli.stdout + cli.stderr
    markdown = report.read_text()
    assert "Final verdict: **FAILED**" in markdown
    assert "| Discover required peer | FAILED |" in markdown
    assert "00:11:22:33:44:55" in markdown and "test_missing_peer" in markdown
