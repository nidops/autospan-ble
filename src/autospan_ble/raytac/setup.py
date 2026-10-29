"""Explicit, application-only USB DFU maintenance for the nRF52840 dongle.

This operation is separate from passive observation. The AutoSpan component
requires both hardware and destructive gates; opening it never programs a device.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import os
from os.path import realpath
from pathlib import Path
from subprocess import DEVNULL, Popen, TimeoutExpired
from tempfile import TemporaryFile
from threading import RLock
from time import monotonic
from typing import Any, TextIO
from zipfile import ZipFile

from autospan.api import Evidence
from autospan_ble.bleuio.selector import SerialResolver
from autospan_ble.config import SnifferSetupConfig
from autospan_ble.nrf_sniffer import NrfSnifferObserver, _stop_process


@dataclass(frozen=True)
class SnifferSetupResult:
    programmed: bool
    firmware_version: str
    endpoint: str
    usb_location: str
    firmware_sha256: str


def _validate_firmware(path: Path, expected_digest: str) -> None:
    digest = sha256(path.read_bytes()).hexdigest()
    if digest != expected_digest:
        raise ValueError(f"firmware SHA-256 mismatch: expected {expected_digest}, got {digest}")
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))["manifest"]
        if not isinstance(manifest, dict) or set(manifest) != {"application"}:
            raise ValueError("firmware must contain only an application; preserve the bootloader")
        application = manifest["application"]
        for key in ("bin_file", "dat_file"):
            if not archive.read(application[key]):
                raise ValueError(f"empty DFU {key}")


def _run(command: tuple[str, ...], timeout_s: float, log: TextIO) -> list[dict[str, Any]]:
    """Own and reap one vendor process, including wrapper descendants."""
    with TemporaryFile("w+b") as stdout, TemporaryFile("w+b") as stderr:
        process = Popen(
            command, stdin=DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True
        )
        try:
            try:
                process.wait(timeout=timeout_s)
            except TimeoutExpired as exc:
                raise TimeoutError(f"Nordic command timed out after {timeout_s:g}s") from exc
        finally:
            _stop_process(process, 3.0)
            stdout.seek(0)
            stderr.seek(0)
            output = stdout.read().decode(errors="replace")
            diagnostic = stderr.read().decode(errors="replace")
            log.write(json.dumps({"argv": command, "stdout": output, "stderr": diagnostic}) + "\n")
            log.flush()
        if process.returncode:
            raise RuntimeError(
                f"Nordic command failed (exit={process.returncode}): {diagnostic[-8192:]}"
            )
        records = [json.loads(line) for line in output.splitlines() if line.strip()]
        ends = [row["data"] for row in records if row.get("type") == "task_end"]
        if not ends or any(row.get("result") != "success" for row in ends):
            raise RuntimeError(f"Nordic command did not report success: {output[-8192:]}")
        return records


class SnifferSetup:
    """One explicitly claimed dongle; setup is idempotent when firmware is ready."""

    def __init__(self, config: SnifferSetupConfig, resolver: SerialResolver) -> None:
        self._config = config
        self._resolver = resolver
        self._lock = RLock()
        self._closed = False

    def prepare(self, evidence: Evidence) -> SnifferSetupResult:
        with self._lock:
            if self._closed:
                raise RuntimeError("sniffer setup component is closed")
            config = self._config
            _validate_firmware(config.firmware, config.firmware_sha256)
            if os.name != "posix":
                raise NotImplementedError("sniffer setup requires the POSIX capture backend")
            deadline = monotonic() + config.timeout_s

            def remaining() -> float:
                seconds = deadline - monotonic()
                if seconds <= 0:
                    raise TimeoutError("sniffer setup deadline expired")
                return seconds

            source = f"ble.setup.{config.name}"
            with (
                evidence.reserve_artifact(
                    kind="firmware-setup",
                    source=source,
                    suffix=".jsonl",
                    media_type="application/x-ndjson",
                    format="nrfutil-json",
                    name=f"{config.name}-setup",
                    attributes={
                        "logical_observer": config.name,
                        "firmware_sha256": config.firmware_sha256,
                    },
                ) as artifact,
                artifact.path.open("w", encoding="utf-8") as log,
            ):
                port = self._resolver.resolve(config.selector)
                programmed = port.pid != 0x522A
                if programmed:
                    if port.pid != 0x521F:
                        raise RuntimeError(
                            "Enter the Raytac USB bootloader: hold its button while reconnecting "
                            "it to the same USB port until the LED lights, then run setup again"
                        )
                    records = _run(
                        (*config.nrfutil_command, "device", "list", "--json"), remaining(), log
                    )
                    candidates = [
                        device
                        for row in records
                        if row.get("type") == "info"
                        for device in row.get("data", {}).get("devices", [])
                        if any(
                            realpath(p["comName"]) == realpath(port.device)
                            for p in device.get("serialPorts", [])
                        )
                    ]
                    if len(candidates) != 1:
                        raise RuntimeError(
                            "Nordic inventory did not uniquely identify the selected bootloader"
                        )
                    device = candidates[0]
                    serial_number = device.get("serialNumber")
                    if (
                        not isinstance(serial_number, str)
                        or not serial_number.strip()
                        or "," in serial_number
                        or not device.get("traits", {}).get("nordicDfu")
                        or device.get("devkit", {}).get("boardVersion") != "PCA10059"
                    ):
                        raise RuntimeError(
                            "selected device is not a supported nRF52840-dongle DFU bootloader"
                        )
                    evidence.event(
                        "ble_sniffer_programming",
                        source=source,
                        logical_observer=config.name,
                        usb_location=config.selector.location,
                        firmware_sha256=config.firmware_sha256,
                    )
                    _run(
                        (
                            *config.nrfutil_command,
                            "device",
                            "program",
                            "--firmware",
                            str(config.firmware.resolve()),
                            "--serial-number",
                            serial_number,
                            "--traits",
                            "nordicDfu",
                            "--json",
                        ),
                        remaining(),
                        log,
                    )
                application = replace(config.selector, product_ids=(0x522A,))
                port = self._resolver.wait(application, timeout_s=remaining())
                observer = NrfSnifferObserver(
                    config.name,
                    application,
                    self._resolver,
                    extcap_command=config.extcap_command,
                    extcap_version=config.expected_version,
                    startup_timeout_s=min(config.startup_timeout_s, remaining()),
                )
                try:
                    observer.open()
                    if observer.firmware_version != config.expected_version:
                        raise RuntimeError("sniffer did not report the expected firmware version")
                    result = SnifferSetupResult(
                        programmed,
                        observer.firmware_version,
                        port.device,
                        config.selector.location or "",
                        config.firmware_sha256,
                    )
                finally:
                    observer.close()
                log.write(json.dumps({"verified": result.__dict__}) + "\n")
                evidence.event("ble_sniffer_ready", source=source, **result.__dict__)
                return result

    def close(self) -> None:
        with self._lock:
            self._closed = True
