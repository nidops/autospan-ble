"""Real subprocess/FIFO regression tests; synthetic packets do not simulate RF."""

import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

import pytest

from autospan.api import GateError
from autospan._core.evidence_store import LocalEvidenceStore
from autospan._core.host import build_host
from autospan._core.runtime import RuntimeSession
from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
from autospan_ble.bleuio.types import PortInfo
from autospan_ble.config import parse_config
from autospan_ble.facade import BleBench
from autospan_ble.nrf_sniffer import NrfSnifferObserver
from autospan_ble.observer.types import ObserverState
from autospan_ble.raytac.extcap import RaytacObserver
from autospan_ble.raytac.profile import RAYTAC_NRF_SNIFFER_PID, RAYTAC_NRF_SNIFFER_VID

pytestmark = pytest.mark.skipif(os.name != "posix", reason="capture backend requires POSIX")


class Ports:
    def __init__(self, *ports):
        self._ports = ports

    def ports(self):
        return iter(self._ports)


@pytest.fixture
def sniffer(tmp_path):
    script = tmp_path / "extcap.py"
    script.write_text(
        r"""
import os, signal, stat, struct, subprocess, sys, time
from pathlib import Path
mode, port, marker = sys.argv[1:4]
if mode == 'crash' and sys.platform == 'linux':
    # Host crash reporters can delay SIGSEGV exit beyond the observation window.
    # This synthetic child needs the exit status, not a core dump.
    import ctypes
    assert ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE
if '--extcap-interfaces' in sys.argv:
    if mode == 'discovery-timeout':
        time.sleep(30)
    if mode != 'missing':
        version = 'None' if mode == 'no-version' else '4.1.1'
        interface = port if mode == 'bare' else port + '-' + version
        print('interface {value=' + interface + '}{display=Synthetic sniffer}')
    sys.exit(0)
Path(marker).write_text(str(os.getpid()))
assert sys.argv[sys.argv.index('--extcap-interface') + 1] == port + '-4.1.1'
fifo = Path(sys.argv[sys.argv.index('--fifo') + 1])
assert stat.S_ISFIFO(fifo.stat().st_mode)
running = True
def stop(*args):
    global running
    running = False
signal.signal(signal.SIGINT, signal.SIG_IGN if mode == 'stubborn' else stop)
signal.signal(signal.SIGTERM, signal.SIG_IGN if mode == 'stubborn' else stop)
if mode == 'wrapper':
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    Path(marker + '.child').write_text(str(child.pid))
with fifo.open('wb', buffering=0) as stream:
    if mode in {'controls', 'control-error'}:
        control_in = Path(sys.argv[sys.argv.index('--extcap-control-in') + 1])
        control_out = Path(sys.argv[sys.argv.index('--extcap-control-out') + 1])
        assert stat.S_ISFIFO(control_in.stat().st_mode)
        assert stat.S_ISFIFO(control_out.stat().st_mode)
        with control_in.open('rb', buffering=0) as incoming, control_out.open('wb', buffering=0) as outgoing:
            assert incoming.read(6) == b'T\x00\x00\x02\x00\x00'
            payload = b'device list update' if mode == 'controls' else b'RF backend failed'
            message = b'T' + (len(payload) + 2).to_bytes(3, 'big') + bytes([0, 2 if mode == 'controls' else 9]) + payload
            for i in range(1000):
                outgoing.write(message)
    if mode == 'invalid':
        stream.write(b'not a pcap stream'.ljust(24, b'!'))
    elif mode != 'silent':
        stream.write(struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 272))
        if mode == 'partial':
            stream.write(b'bad record')
        elif mode != 'empty':
            if mode == 'delayed':
                while running and not Path(marker + '.packets').exists():
                    time.sleep(0.01)
            stream.write(struct.pack('<IIII', 1, 0, 4, 4) + b'test')
    if mode == 'stderr':
        os.write(2, b'log' * 100000)
    while running:
        if mode == 'crash' and Path(marker + '.crash').exists():
            os.kill(os.getpid(), signal.SIGSEGV)
        time.sleep(0.01)
""".lstrip()
    )

    def make(mode="normal", port="/dev/observer", stop=2.0, startup=2.0):
        marker = tmp_path / f"{mode}.pid"
        observer = NrfSnifferObserver(
            "air",
            SerialDeviceSelector(serial_number="OBSERVER-TEST"),
            SerialResolver(Ports(PortInfo(port, serial_number="OBSERVER-TEST"))),
            extcap_command=(sys.executable, str(script), mode, port, str(marker)),
            startup_timeout_s=startup,
            stop_timeout_s=stop,
        )
        return observer, marker

    yield make
    assert not any(t.name.startswith("nrf-capture-") for t in threading.enumerate())


def test_observer_config_is_a_hardware_claim():
    config = parse_config(
        {
            "observers": {
                "air": {
                    "selector": {
                        "vid": hex(RAYTAC_NRF_SNIFFER_VID),
                        "product_ids": [hex(RAYTAC_NRF_SNIFFER_PID)],
                        "serial_number": "OBSERVER-TEST",
                    },
                    "extcap_command": [sys.executable, "sniffer.py"],
                }
            }
        }
    )
    assert config.devices == ()
    assert config.uses_hardware
    assert config.observers[0].backend == "nrf-sniffer"
    assert config.observers[0].selector.claim_key == "serial:vid=1915:sn=OBSERVER-TEST"
    assert RaytacObserver is NrfSnifferObserver


@pytest.mark.parametrize(
    "change",
    [
        {"extcap_command": []},
        {"extcap_command": [42]},
        {"startup_timeout_s": 0},
        {"startup_timeout_s": float("nan")},
        {"stop_timeout_s": -1},
        {"stop_timeout_s": float("inf")},
    ],
)
def test_invalid_observer_config(change):
    with pytest.raises(ValueError):
        parse_config({"observers": {"air": {"selector": {"serial_number": "AIR"}, **change}}})


def test_device_and_observer_cannot_claim_same_physical_identity():
    with pytest.raises(ValueError, match="claimed more than once"):
        parse_config(
            {
                "devices": {"dut": {"backend": "fake", "selector": {"serial_number": "SAME"}}},
                "observers": {"air": {"selector": {"serial_number": "SAME"}}},
            }
        )


@pytest.mark.parametrize("mode", ["normal", "stderr", "empty", "controls"])
def test_fifo_capture_and_reopen(sniffer, tmp_path, mode):
    observer, marker = sniffer(mode)
    for index in range(2):
        observer.open()
        assert observer.state is ObserverState.READY
        path = tmp_path / f"{mode}-{index}.pcap"
        with observer.start_capture(path) as session:
            assert observer.state is ObserverState.CAPTURING
            assert path.stat().st_size >= 24
        assert session.result is not None
        assert session.stop() is session.result
        assert session.result.packet_count == (0 if mode == "empty" else 1)
        assert session.result.byte_count == path.stat().st_size
        assert observer.state is ObserverState.READY
        observer.close()
        assert observer.state is ObserverState.CLOSED
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)


def test_direct_capture_context_releases_observer_ownership(sniffer, tmp_path):
    observer, _ = sniffer()
    observer.open()
    first = tmp_path / "first.pcap"
    with observer.start_capture(first) as session:
        assert observer.state is ObserverState.CAPTURING
    assert session.result is not None
    assert observer.state is ObserverState.READY

    second = tmp_path / "second.pcap"
    with observer.start_capture(second) as second_session:
        assert observer.state is ObserverState.CAPTURING
    assert second_session.result is not None
    assert observer.state is ObserverState.READY
    observer.close()


def test_capture_waits_for_a_complete_packet_after_header(sniffer, tmp_path):
    observer, marker = sniffer("delayed")
    observer.open()
    try:
        with observer.start_capture(tmp_path / "delayed.pcap") as session:
            with pytest.raises(TimeoutError, match="no complete packet"):
                session.wait_for_packets(0.03)
            Path(str(marker) + ".packets").touch()
            session.wait_for_packets(2)
        assert session.result is not None
        assert session.result.packet_count == 1
    finally:
        observer.close()


def test_empty_capture_remains_valid_but_cannot_claim_rf_readiness(sniffer, tmp_path):
    observer, _ = sniffer("empty")
    observer.open()
    try:
        with observer.start_capture(tmp_path / "empty.pcap") as session:
            with pytest.raises(TimeoutError, match="no complete packet"):
                session.wait_for_packets(0.02)
        assert session.result is not None
        assert session.result.packet_count == 0
        with pytest.raises(RuntimeError, match="stopped without packets"):
            session.wait_for_packets(0.02)
    finally:
        observer.close()


def test_completed_capture_scope_does_not_stop_a_new_session(sniffer, tmp_path):
    observer, _ = sniffer()
    bench = BleBench({}, {"air": observer})
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    bench.open()
    try:
        with bench.capture("air", evidence) as first:
            result = first.stop()
            second = observer.start_capture(tmp_path / "second.pcap")
            assert first.stop() is result
        assert observer.state is ObserverState.CAPTURING
        assert second.stop().packet_count == 1
    finally:
        bench.close()
        evidence.close()
    (artifact,) = evidence.artifacts()
    assert artifact.complete


def test_failed_capture_stop_preserves_error_after_reopen(sniffer, tmp_path):
    observer, _ = sniffer("partial")
    observer.open()
    session = observer.start_capture(tmp_path / "partial.pcap")
    try:
        with pytest.raises(RuntimeError, match="partial PCAP record") as failure:
            session.stop()
        assert observer.state is ObserverState.FAILED
        observer.close()
        observer.open()
        with pytest.raises(RuntimeError, match="partial PCAP record") as repeated:
            session.stop()
        assert repeated.value is failure.value
        assert observer.state is ObserverState.READY
    finally:
        observer.close()


@pytest.mark.parametrize("use_facade", [False, True])
def test_early_capture_failure_does_not_add_an_ownership_error(sniffer, tmp_path, use_facade):
    observer, _ = sniffer("partial")
    bench = BleBench({}, {"air": observer})
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    bench.open()
    try:
        scope = (
            bench.capture("air", evidence)
            if use_facade
            else observer.start_capture(tmp_path / "direct.pcap")
        )
        with pytest.raises(RuntimeError, match="partial PCAP record"):
            with scope as session:
                session.stop()
        assert observer.state is ObserverState.FAILED
    finally:
        bench.close()
        evidence.close(outcome="failed")
    if use_facade:
        (artifact,) = evidence.artifacts()
        assert not artifact.complete


@pytest.mark.parametrize("mode", ["missing", "no-version"])
def test_ready_requires_responding_interface(sniffer, mode):
    observer, _ = sniffer(mode)
    with pytest.raises(RuntimeError, match="responding sniffer"):
        observer.open()
    assert observer.state is ObserverState.CLOSED


def test_bare_extcap_interface_requires_application_version_reply(sniffer, monkeypatch):
    import autospan_ble.nrf_sniffer as backend

    observer, _ = sniffer("bare")
    calls = []

    def version(endpoint, timeout_s):
        calls.append(endpoint)
        assert timeout_s > 0
        return "4.1.1"

    monkeypatch.setattr(backend, "_query_firmware_version", version)
    observer.open()
    assert observer.firmware_version == "4.1.1"
    assert calls == ["/dev/observer"]
    observer.close()
    assert observer.firmware_version is None

    def silent(*args):
        raise TimeoutError("no application reply")

    monkeypatch.setattr(backend, "_query_firmware_version", silent)
    with pytest.raises(TimeoutError, match="application reply"):
        observer.open()
    assert observer.state is ObserverState.CLOSED


def test_discovery_is_bounded(sniffer):
    observer, _ = sniffer("discovery-timeout", startup=0.1)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="discovery timed out"):
        observer.open()
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("mode,error", [("silent", TimeoutError), ("invalid", RuntimeError)])
def test_failed_start_releases_worker_and_process(sniffer, tmp_path, mode, error):
    observer, marker = sniffer(mode, startup=0.75)
    observer.open()
    with pytest.raises(error):
        observer.start_capture(tmp_path / "failed.pcap")
    assert observer.state is ObserverState.FAILED
    observer.close()
    assert observer.state is ObserverState.CLOSED
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_partial_capture_is_reported(sniffer, tmp_path):
    observer, _ = sniffer("partial")
    observer.open()
    observer.start_capture(tmp_path / "partial.pcap")
    with pytest.raises(RuntimeError, match="partial PCAP record"):
        observer.stop_capture()
    assert observer.state is ObserverState.FAILED
    observer.close()


def test_vendor_control_error_is_a_capture_failure(sniffer, tmp_path):
    observer, _ = sniffer("control-error")
    observer.open()
    with pytest.raises(RuntimeError):
        observer.start_capture(tmp_path / "control-error.pcap")
    assert observer.state is ObserverState.FAILED
    observer.close()


def test_force_kill_is_a_failure(sniffer, tmp_path):
    observer, marker = sniffer("stubborn", stop=0.1)
    observer.open()
    observer.start_capture(tmp_path / "stubborn.pcap")
    with pytest.raises(RuntimeError, match="forced_kill=True"):
        observer.stop_capture()
    observer.close()
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_crash_is_not_a_normal_signal_exit(sniffer, tmp_path):
    observer, marker = sniffer("crash")
    observer.open()
    try:
        observer.start_capture(tmp_path / "crashed.pcap")
        Path(str(marker) + ".crash").touch()
        deadline = time.monotonic() + 1
        while observer.state is not ObserverState.FAILED and time.monotonic() < deadline:
            time.sleep(0.01)
        assert observer.state is ObserverState.FAILED
        with pytest.raises(RuntimeError, match=f"exit=-{signal.SIGSEGV}"):
            observer.stop_capture()
    finally:
        observer.close()


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux process inventory required")
def test_wrapper_children_do_not_keep_running(sniffer, tmp_path):
    observer, marker = sniffer("wrapper")
    observer.open()
    observer.start_capture(tmp_path / "wrapper.pcap")
    observer.close()
    child = int(Path(str(marker) + ".child").read_text())
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        state_path = Path(f"/proc/{child}/stat")
        if not state_path.exists() or state_path.read_text().split()[2] == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("extcap wrapper left a live child")


def test_capture_body_failure_retains_incomplete_correlated_artifact(sniffer, tmp_path):
    observer, _ = sniffer()
    bench = BleBench({}, {"air": observer})
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    bench.open()
    try:
        with evidence.scope(test_id="capture-test"):
            with evidence.step("RF window"):
                with pytest.raises(ValueError, match="scenario failed"):
                    with bench.capture("air", evidence):
                        raise ValueError("scenario failed")
        assert observer.state is ObserverState.READY
    finally:
        bench.close()
        evidence.close(outcome="failed")
    (artifact,) = evidence.artifacts()
    assert not artifact.complete
    assert artifact.context.test_id == "capture-test"
    assert artifact.context.step_id == "RF window"
    assert artifact.source == "ble.observer.air"
    assert artifact.path.read_bytes()[:4] == b"\xd4\xc3\xb2\xa1"
    assert artifact.size == 44
    records = [
        json.loads(line) for line in (evidence.root / "events.jsonl").read_text().splitlines()
    ]
    started = [r for r in records if r["name"] == "ble_capture_started"]
    assert len(started) == 1 and started[0]["test_id"] == "capture-test"


def test_duplicate_resolved_device_is_rejected_before_open(sniffer):
    observer, marker = sniffer()
    other, _ = sniffer("stderr")
    with pytest.raises(ValueError, match="physical device claimed"):
        BleBench({}, {"air": observer, "other": other}).open()
    assert not marker.exists()
    assert observer.state is ObserverState.CLOSED
    assert other.state is ObserverState.CLOSED


def test_partial_bench_start_is_rolled_back(sniffer):
    observer, _ = sniffer()
    failed, _ = sniffer("missing", port="/dev/other-observer")
    with pytest.raises(RuntimeError, match="responding sniffer"):
        BleBench({}, {"air": observer, "failed": failed}).open()
    assert observer.state is ObserverState.CLOSED
    assert failed.state is ObserverState.CLOSED


def test_observer_hardware_gate_precedes_discovery(sniffer, tmp_path, monkeypatch):
    import autospan_ble.plugin as plugin

    observer, _ = sniffer()
    monkeypatch.setattr(plugin, "_build_observer", lambda config: observer)
    host = build_host(
        plugin_names=["ble"],
        config={"ble": {"observers": {"air": {"selector": {"serial_number": "OBSERVER-TEST"}}}}},
        config_dir=tmp_path,
    )
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    try:
        with pytest.raises(GateError):
            with RuntimeSession(
                plan=host.plan, factories=host.factories, evidence=evidence, enabled_gates=[]
            ):
                pass
        assert observer.state is ObserverState.CLOSED
    finally:
        evidence.close()


def test_autospan_capture_uses_installed_plugin_lifecycle(sniffer, tmp_path, monkeypatch):
    import autospan_ble.plugin as plugin

    observer, _ = sniffer()
    monkeypatch.setattr(plugin, "_build_observer", lambda config: observer)
    host = build_host(
        plugin_names=["ble"],
        config={"ble": {"observers": {"air": {"selector": {"serial_number": "OBSERVER-TEST"}}}}},
        config_dir=tmp_path,
    )
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    try:
        with RuntimeSession(
            plan=host.plan, factories=host.factories, evidence=evidence, enabled_gates=["hardware"]
        ) as runtime:
            with evidence.scope(test_id="hosted-capture"):
                with runtime.require("ble.bench", BleBench).capture("air", evidence) as session:
                    assert observer.state is ObserverState.CAPTURING
                assert session.result.packet_count == 1
        assert observer.state is ObserverState.CLOSED
    finally:
        evidence.close(outcome="passed")
    (artifact,) = evidence.artifacts()
    assert artifact.complete and artifact.context.test_id == "hosted-capture"


def test_raytac_open_fails_early_when_extcap_is_missing():
    observer = RaytacObserver(
        "air",
        SerialDeviceSelector(serial_number="OBSERVER-TEST"),
        SerialResolver(Ports(PortInfo("/dev/observer", serial_number="OBSERVER-TEST"))),
        extcap_command=("definitely-not-an-autospan-sniffer-command",),
    )
    with pytest.raises(FileNotFoundError, match="extcap executable"):
        observer.open()
    assert observer.state is ObserverState.CLOSED


def test_only_sniffer_scenario_captures_both_role_directions(sniffer, tmp_path, monkeypatch):
    import yaml
    import autospan_ble.checks as checks
    import autospan_ble.plugin as plugin
    from autospan_ble.scenarios.test_discovery import test_advertiser_is_observed
    from autospan_ble.scenarios.test_sniffer import test_sniffer_captures_known_advertiser

    observer, _ = sniffer()
    monkeypatch.setattr(plugin, "_build_observer", lambda config: observer)
    decoded_addresses = []

    def decode(path, address, command):
        assert path.exists()
        decoded_addresses.append(address)
        return 1

    monkeypatch.setattr(checks, "_advertiser_packets", decode)
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "autospan.fake.yaml").read_text()
    )
    config["ble"]["observers"] = {"air": {"selector": {"serial_number": "OBSERVER-TEST"}}}
    host = build_host(plugin_names=["ble"], config=config, config_dir=tmp_path)
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    try:
        with RuntimeSession(
            plan=host.plan, factories=host.factories, evidence=evidence, enabled_gates=["hardware"]
        ) as runtime:
            for reverse in (False, True):
                with evidence.scope(test_id=f"active-{reverse}"):
                    test_advertiser_is_observed(runtime, reverse)
                assert not [
                    a
                    for a in evidence.artifacts()
                    if a.kind == "capture" and a.context.test_id == f"active-{reverse}"
                ]
                with evidence.scope(test_id=f"sniffer-{reverse}"):
                    test_sniffer_captures_known_advertiser(runtime, reverse)
        assert observer.state is ObserverState.CLOSED
    finally:
        evidence.close(outcome="passed")
    captures = [artifact for artifact in evidence.artifacts() if artifact.kind == "capture"]
    assert len(captures) == 2
    assert all(artifact.complete and artifact.size == 44 for artifact in captures)
    assert {artifact.context.test_id for artifact in captures} == {
        "sniffer-False",
        "sniffer-True",
    }
    assert len(decoded_addresses) == 2 and len(set(decoded_addresses)) == 2


@pytest.mark.skipif(
    not Path("/proc/self/fd").exists(), reason="Linux descriptor inventory required"
)
def test_repeated_capture_does_not_leak_descriptors(sniffer, tmp_path):
    observer, _ = sniffer()
    observer.open()
    before = len(list(Path("/proc/self/fd").iterdir()))
    try:
        for index in range(20):
            observer.start_capture(tmp_path / f"cycle-{index}.pcap")
            result = observer.stop_capture()
            assert result.packet_count == 1
            assert observer.state is ObserverState.READY
        assert len(list(Path("/proc/self/fd").iterdir())) == before
    finally:
        observer.close()


def test_scenario_and_capture_failure_both_propagate(sniffer, tmp_path):
    observer, _ = sniffer("partial")
    bench = BleBench({}, {"air": observer})
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    bench.open()
    try:
        with pytest.raises(ExceptionGroup) as failure:
            with bench.capture("air", evidence):
                raise ValueError("scenario failed")
        assert isinstance(failure.value.exceptions[0], ValueError)
        assert "partial PCAP record" in str(failure.value.exceptions[1])
        assert observer.state is ObserverState.FAILED
    finally:
        bench.close()
        evidence.close(outcome="failed")
    (artifact,) = evidence.artifacts()
    assert not artifact.complete


def test_endpoint_and_observer_alias_collision_is_rejected_before_open(sniffer):
    from autospan_ble.bleuio.device import BleuIODevice
    from autospan_ble.simulation.fake import FakeBleuIOEnvironment, FakePortEnumerator

    observer, marker = sniffer()
    env = FakeBleuIOEnvironment(endpoint="/dev/observer", serial_number="ENDPOINT-TEST")

    def forbidden(endpoint):
        raise AssertionError("duplicate physical owner opened a transport")

    device = BleuIODevice(
        "dut",
        SerialDeviceSelector(serial_number="ENDPOINT-TEST"),
        SerialResolver(FakePortEnumerator(env)),
        forbidden,
    )
    with pytest.raises(ValueError, match="physical device claimed"):
        BleBench({"dut": device}, {"air": observer}).open()
    assert not marker.exists()


@pytest.mark.parametrize("peer_count", [0, 2])
def test_sniffer_check_requires_known_peer_and_retains_capture(
    sniffer, tmp_path, monkeypatch, peer_count
):
    import yaml
    import autospan_ble.checks as checks
    import autospan_ble.plugin as plugin

    observer, _ = sniffer()
    monkeypatch.setattr(plugin, "_build_observer", lambda config: observer)
    calls = []

    def decode(path, address, command):
        assert path.exists()
        calls.append(address)
        return peer_count

    monkeypatch.setattr(checks, "_advertiser_packets", decode)
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "autospan.fake.yaml").read_text()
    )
    config["ble"]["observers"] = {"air": {"selector": {"serial_number": "OBSERVER-TEST"}}}
    host = build_host(plugin_names=["ble"], config=config, config_dir=tmp_path)
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    try:
        with RuntimeSession(
            plan=host.plan, factories=host.factories, evidence=evidence, enabled_gates=["hardware"]
        ) as runtime:
            bench = runtime.require("ble.bench", BleBench)
            args = (bench, evidence, "air", *bench.names()[:2])
            if peer_count:
                assert checks.check_sniffer(*args).advertiser_packets == peer_count
            else:
                with pytest.raises(AssertionError, match="no CRC-valid packets"):
                    checks.check_sniffer(*args)
            assert observer.state is ObserverState.READY
    finally:
        evidence.close()
    assert len(calls) == 1
    (capture,) = [a for a in evidence.artifacts() if a.kind == "capture"]
    assert capture.complete == bool(peer_count)
    assert observer.state is ObserverState.CLOSED


def test_failed_process_cleanup_retains_owner_until_retry(sniffer, tmp_path, monkeypatch):
    import autospan_ble.nrf_sniffer as backend

    observer, _ = sniffer()
    observer.open()
    handle = observer.start_capture(tmp_path / "retry.pcap")
    session = observer._session
    stop = backend._stop_process

    def fail(process, timeout):
        raise OSError("cannot signal owned process")

    monkeypatch.setattr(backend, "_stop_process", fail)
    try:
        with pytest.raises(OSError, match="cannot signal"):
            handle.stop()
        assert not session._finished
        assert observer._session is session
        with pytest.raises(RuntimeError, match="failed"):
            observer.open()
    finally:
        monkeypatch.setattr(backend, "_stop_process", stop)
        try:
            observer.close()
        except (OSError, RuntimeError):
            pass  # The failed capture remains a failure even after successful cleanup.
        observer.close()
        # Also reap the fixture on the old broken implementation.
        if session._process.poll() is None:
            stop(session._process, 1)
    assert session._process.poll() is not None
    assert not session._thread.is_alive()
    assert observer.state is ObserverState.CLOSED
