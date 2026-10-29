"""Application-only DFU orchestration with synthetic vendor processes, never RF."""

from dataclasses import replace
from hashlib import sha256
import json
import os
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from autospan.api import GateError
from autospan._core.evidence_store import LocalEvidenceStore
from autospan._core.host import build_host
from autospan._core.runtime import RuntimeSession
from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
from autospan_ble.bleuio.types import PortInfo
from autospan_ble.config import parse_sniffer_setup, SnifferSetupConfig
from autospan_ble.raytac.setup import SnifferSetup, _validate_firmware


@pytest.fixture
def setup_fixture(tmp_path):
    firmware = tmp_path / "application.zip"
    with ZipFile(firmware, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {"manifest": {"application": {"bin_file": "app.bin", "dat_file": "app.dat"}}}
            ),
        )
        archive.writestr("app.bin", b"synthetic-application")
        archive.writestr("app.dat", b"synthetic-signed-init")
    state = tmp_path / "state"
    state.write_text("boot")
    sdk = tmp_path / "sdk.py"
    sdk.write_text(
        """
import json, sys, time
from pathlib import Path
mode, state, marker = sys.argv[1:4]
args = sys.argv[4:]
Path(marker).write_text(json.dumps(args))
if mode == 'timeout':
    time.sleep(30)
result = 'failure' if mode == 'failure' else 'success'
print(json.dumps({'type': 'task_end', 'data': {'result': result}}))
if args[:2] == ['device', 'list']:
    device = {'serialNumber': 'SELECTED-TEST-DEVICE',
              'traits': {'nordicDfu': True},
              'devkit': {'boardVersion': 'WRONG' if mode == 'wrong-board' else 'PCA10059'},
              'serialPorts': [{'comName': '/dev/boot-test'}]}
    print(json.dumps({'type': 'info', 'data': {'devices': [device]}}))
elif result == 'success':
    assert args[args.index('--serial-number') + 1] == 'SELECTED-TEST-DEVICE'
    assert args[args.index('--traits') + 1] == 'nordicDfu'
    Path(state).write_text('app' if mode != 'no-app' else 'boot')
""".lstrip()
    )
    extcap = tmp_path / "extcap.py"
    extcap.write_text("import sys\nprint('interface {value=/dev/app-test-4.1.1}{display=TEST}')\n")
    marker = tmp_path / "sdk-argv.json"

    class Ports:
        def ports(self):
            boot = state.read_text() == "boot"
            return iter(
                [
                    PortInfo(
                        "/dev/boot-test" if boot else "/dev/app-test",
                        vid=0x1915,
                        pid=0x521F if boot else 0x522A,
                        serial_number="BOOT" if boot else "CHANGED-APP-SERIAL",
                        location="1-2:1.0" if boot else "1-2:1.2",
                    )
                ]
            )

    config = SnifferSetupConfig(
        "air",
        SerialDeviceSelector(vid=0x1915, product_ids=(0x521F, 0x522A), location="1-2"),
        firmware,
        sha256(firmware.read_bytes()).hexdigest(),
        nrfutil_command=(sys.executable, str(sdk), "normal", str(state), str(marker)),
        extcap_command=(sys.executable, str(extcap)),
        timeout_s=5,
    )
    return config, SerialResolver(Ports()), marker, state


@pytest.mark.skipif(os.name != "posix", reason="sniffer setup verifies the POSIX capture backend")
def test_programs_exact_device_follows_usb_port_and_verifies_response(setup_fixture, tmp_path):
    config, resolver, marker, state = setup_fixture
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    setup = SnifferSetup(config, resolver)
    try:
        with evidence.scope(test_id="firmware-test"):
            result = setup.prepare(evidence)
        assert result.programmed
        assert result.firmware_version == "4.1.1"
        assert result.endpoint == "/dev/app-test"
        argv = json.loads(marker.read_text())
        assert argv[:2] == ["device", "program"]
        assert argv[argv.index("--serial-number") + 1] == "SELECTED-TEST-DEVICE"
        assert not any(word in argv for word in ("--erase-all", "recover", "--force"))
        marker.unlink()
        assert not setup.prepare(evidence).programmed
        assert not marker.exists(), "ready firmware must not be reprogrammed"
    finally:
        setup.close()
        evidence.close(outcome="passed")
    assert all(artifact.complete for artifact in evidence.artifacts())
    assert evidence.artifacts()[0].context.test_id == "firmware-test"
    with pytest.raises(RuntimeError, match="closed"):
        setup.prepare(evidence)


@pytest.mark.skipif(os.name != "posix", reason="sniffer setup verifies the POSIX capture backend")
@pytest.mark.parametrize(
    "mode,error",
    [
        ("failure", RuntimeError),
        ("wrong-board", RuntimeError),
        ("timeout", TimeoutError),
        ("no-app", Exception),
    ],
)
def test_setup_failure_is_bounded_and_retains_incomplete_evidence(
    setup_fixture, tmp_path, mode, error
):
    config, resolver, _, _ = setup_fixture
    argv = list(config.nrfutil_command)
    argv[2] = mode
    timeout_s = 0.25 if mode == "timeout" else (1.0 if mode == "no-app" else 2.0)
    setup = SnifferSetup(
        replace(config, nrfutil_command=tuple(argv), timeout_s=timeout_s), resolver
    )
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    try:
        with pytest.raises(error):
            setup.prepare(evidence)
    finally:
        setup.close()
        evidence.close(outcome="failed")
    assert not evidence.artifacts()[0].complete


def test_hash_mismatch_precedes_vendor_process(setup_fixture, tmp_path):
    config, resolver, marker, _ = setup_fixture
    setup = SnifferSetup(replace(config, firmware_sha256="0" * 64), resolver)
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    try:
        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            setup.prepare(evidence)
        assert not marker.exists()
    finally:
        setup.close()
        evidence.close()


def test_unsupported_platform_is_rejected_before_device_access(setup_fixture, monkeypatch):
    import autospan_ble.raytac.setup as backend

    config, resolver, marker, state = setup_fixture

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported setup must not access hardware or start vendor processes")

    monkeypatch.setattr(backend, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(resolver, "resolve", forbidden)
    monkeypatch.setattr(backend, "_run", forbidden)
    with pytest.raises(NotImplementedError, match="POSIX"):
        SnifferSetup(config, resolver).prepare(SimpleNamespace(reserve_artifact=forbidden))
    assert not marker.exists()
    assert state.read_text() == "boot"


def test_bootloader_bundle_is_rejected_even_with_correct_hash(tmp_path):
    path = tmp_path / "mixed.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", '{"manifest":{"application":{},"bootloader":{}}}')
    with pytest.raises(ValueError, match="only an application"):
        _validate_firmware(path, sha256(path.read_bytes()).hexdigest())


def raw_setup(config):
    return {
        "sniffer_setup": {
            "selector": {"vid": 0x1915, "location": "1-2"},
            "firmware": str(config.firmware),
            "firmware_sha256": config.firmware_sha256,
        }
    }


@pytest.mark.parametrize("gates", [[], ["hardware"], ["destructive"]])
def test_setup_requires_both_gates_before_hardware_enumeration(
    setup_fixture, tmp_path, monkeypatch, gates
):
    from autospan_ble.bleuio.selector import PySerialPortEnumerator

    def forbidden(self):
        raise AssertionError("gating allowed hardware enumeration")

    monkeypatch.setattr(PySerialPortEnumerator, "ports", forbidden)
    config, _, _, _ = setup_fixture
    host = build_host(plugin_names=["ble"], config={"ble": raw_setup(config)}, config_dir=tmp_path)
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    try:
        with pytest.raises(GateError):
            with RuntimeSession(
                plan=host.plan, factories=host.factories, evidence=evidence, enabled_gates=gates
            ):
                pass
    finally:
        evidence.close()


def test_setup_cannot_share_a_bench_owner(setup_fixture):
    config, _, _, _ = setup_fixture
    with pytest.raises(ValueError, match="active BLE owners"):
        parse_sniffer_setup({**raw_setup(config), "observers": {"air": {}}})


@pytest.mark.parametrize(
    "change",
    [
        {"firmware_sha256": ""},
        {"timeout_s": float("nan")},
        {"selector": {"vid": 0x1915, "location": "1-2", "product_ids": [0x521F]}},
        {"selector": {"vid": 0x1915, "location": "1-2", "serial_number": "BOOT"}},
    ],
)
def test_invalid_maintenance_config(setup_fixture, change):
    config, _, _, _ = setup_fixture
    raw = raw_setup(config)
    raw["sniffer_setup"].update(change)
    with pytest.raises(ValueError):
        parse_sniffer_setup(raw)
