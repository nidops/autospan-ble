"""Shared scenario/console check of a known advertiser in a passive capture."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from subprocess import DEVNULL, Popen, TimeoutExpired
from tempfile import TemporaryFile

from autospan.api import Evidence
from autospan_ble.facade import BleBench
from autospan_ble.nrf_sniffer import _stop_process


@dataclass(frozen=True)
class SnifferCheckResult:
    address: str
    packet_count: int
    advertiser_packets: int


def _advertiser_packets(path: Path, address: str, command: tuple[str, ...]) -> int:
    if re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", address) is None:
        raise ValueError(f"invalid advertiser MAC address: {address!r}")
    with TemporaryFile("w+b") as stdout, TemporaryFile("w+b") as stderr:
        process = Popen(
            [
                *command,
                "-n",
                "-r",
                str(path),
                "-Y",
                f"btle.advertising_address == {address} && nordic_ble.crcok == 1",
                "-T",
                "fields",
                "-e",
                "frame.number",
            ],
            stdin=DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            process.wait(timeout=15)
        except TimeoutExpired as exc:
            raise TimeoutError("capture decoder timed out") from exc
        finally:
            _stop_process(process, 3)
        if process.returncode:
            stderr.seek(0)
            raise RuntimeError(
                f"capture decoder failed: {stderr.read(8192).decode(errors='replace')}"
            )
        stdout.seek(0)
        rows = stdout.read().decode().splitlines()
        if any(not row.isdigit() for row in rows):
            raise RuntimeError("capture decoder returned invalid frame numbers")
        return len(rows)


def check_sniffer(
    bench: BleBench,
    evidence: Evidence,
    observer_name: str,
    advertiser_name: str,
    scanner_name: str,
    *,
    seconds: float = 3.0,
) -> SnifferCheckResult:
    """Assert typed discovery and CRC-valid captured packets from the same peer.

    TShark is an external verification tool, only required for this RF check.
    Ordinary BLE operations and opaque capture do not depend on it.
    """
    if advertiser_name == scanner_name:
        raise ValueError("advertiser and scanner must be distinct endpoints")
    advertiser = bench.device(advertiser_name)
    scanner = bench.device(scanner_name)
    with evidence.step(
        f"{observer_name} captures {advertiser_name}; {scanner_name} verifies discovery"
    ):
        with evidence.step(f"{advertiser_name}: query known advertiser MAC"):
            address = advertiser.mac_address()
        with bench.capture(observer_name, evidence) as session:
            with evidence.step(f"{advertiser_name}: RF-check advertising lifetime and cleanup"):
                with advertiser.advertising():
                    with evidence.step(f"{observer_name}: wait for RF packets"):
                        session.wait_for_packets()
                    with evidence.step(
                        f"{scanner_name}: discover known advertiser {advertiser_name}"
                    ):
                        rows = scanner.scan(seconds=seconds)
                        if not any(row.address.value == address.upper() for row in rows):
                            raise AssertionError(
                                f"{scanner_name} did not observe {advertiser_name} ({address})"
                            )
            # TShark needs a finalized PCAP while the evidence reservation still
            # owns its temporary path. Session stop is idempotent and updates the
            # observer owner, so the surrounding capture context remains safe.
            with evidence.step(f"{observer_name}: stop capture and finalize PCAP"):
                capture = session.stop()
            with evidence.step(f"{observer_name}: decode PCAP and verify known-peer CRC"):
                count = _advertiser_packets(capture.path, address, bench.capture_decoder_command)
                if count <= 0:
                    raise AssertionError(
                        f"{observer_name} captured no CRC-valid packets from {address}"
                    )
            result = SnifferCheckResult(address, capture.packet_count, count)
            evidence.event(
                "ble_sniffer_check_passed",
                source=f"ble.observer.{observer_name}",
                advertiser=advertiser_name,
                scanner=scanner_name,
                **result.__dict__,
            )
            return result
