"""Configuration parsing for the BLE project plugin."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any, Mapping

from autospan_ble.bleuio.selector import SerialDeviceSelector
from autospan_ble.bleuio.transport import DEFAULT_MAX_RX_LINE_BYTES, MAX_RX_LINE_BYTES


def _integer(value: object | None) -> int | None:
    if value is None:
        return None
    if type(value) is int:
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ValueError("USB ID must be an integer or integer string")


def _product_ids(value: Any) -> tuple[int, ...]:
    if isinstance(value, (str, int)):
        value = (value,)
    if not isinstance(value, (list, tuple)) or any(item is None for item in value):
        raise ValueError("product_ids must contain integer USB IDs")
    return tuple(pid for item in value if (pid := _integer(item)) is not None)


def _baud(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("baud must be a positive integer")
    return int(value)


def _check_timeout(value: Any, name: str) -> None:
    message = f"{name} must be finite and > 0"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    try:
        valid = isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(message)


def _timeout(value: Any, name: str) -> float:
    message = f"{name} must be finite and > 0"
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(message)
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(message) from exc
    _check_timeout(result, name)
    return result


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    backend: str
    selector: SerialDeviceSelector
    baud: int = 57600
    reset_timeout_s: float = 20.0
    fake_scan_results: tuple[dict[str, Any], ...] = ()
    fake_address: str = "12:34:56:78:9A:BC"
    max_rx_line_bytes: int = DEFAULT_MAX_RX_LINE_BYTES

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("logical device name must not be empty")
        if type(self.baud) is not int or self.baud <= 0:
            raise ValueError("baud must be a positive integer")
        _check_timeout(self.reset_timeout_s, "reset timeout")
        if (
            type(self.max_rx_line_bytes) is not int
            or not 1 <= self.max_rx_line_bytes <= MAX_RX_LINE_BYTES
        ):
            raise ValueError(
                f"max_rx_line_bytes must be an integer between 1 and {MAX_RX_LINE_BYTES}"
            )


@dataclass(frozen=True)
class ObserverConfig:
    name: str
    backend: str
    selector: SerialDeviceSelector
    extcap_command: tuple[str, ...] = ("nrf_sniffer_ble.sh",)
    extcap_version: str | None = None
    startup_timeout_s: float = 5.0
    stop_timeout_s: float = 3.0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("logical observer name must not be empty")
        if self.backend not in {"nrf-sniffer", "raytac-nrf-extcap"}:
            raise ValueError(f"unsupported observer backend: {self.backend}")
        if not self.extcap_command or any(not item.strip() for item in self.extcap_command):
            raise ValueError("observer extcap_command must not be empty")
        if self.extcap_version is not None and not self.extcap_version.strip():
            raise ValueError("observer extcap_version must not be empty")
        _check_timeout(self.startup_timeout_s, "observer startup timeout")
        _check_timeout(self.stop_timeout_s, "observer stop timeout")


@dataclass(frozen=True)
class BleConfig:
    devices: tuple[DeviceConfig, ...]
    observers: tuple[ObserverConfig, ...] = ()
    capture_decoder_command: tuple[str, ...] = ("tshark",)

    @property
    def uses_hardware(self) -> bool:
        return any(device.backend != "fake" for device in self.devices) or bool(self.observers)


def parse_config(config: Mapping[str, Any]) -> BleConfig:
    if "sniffer_setup" in config:
        raise ValueError("sniffer_setup is a separate maintenance configuration")
    raw_devices = config.get("devices", {})
    raw_observers = config.get("observers", {})
    if not isinstance(raw_devices, Mapping):
        raise ValueError("ble.devices must be a mapping")
    if not isinstance(raw_observers, Mapping):
        raise ValueError("ble.observers must be a mapping")
    if not raw_devices and not raw_observers:
        raise ValueError("ble requires at least one device or observer")
    devices: list[DeviceConfig] = []
    claims: set[str] = set()
    for name, raw in raw_devices.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"ble.devices.{name} must be a mapping")
        backend = str(raw.get("backend", "pyserial")).strip().lower()
        if backend not in {"pyserial", "fake"}:
            raise ValueError(f"ble.devices.{name}.backend must be pyserial or fake")
        selector_raw = raw.get("selector", {})
        if not isinstance(selector_raw, Mapping):
            raise ValueError(f"ble.devices.{name}.selector must be a mapping")
        selector = SerialDeviceSelector(
            vid=_integer(selector_raw.get("vid")),
            product_ids=_product_ids(selector_raw.get("product_ids", ())),
            serial_number=(
                str(selector_raw["serial_number"]) if selector_raw.get("serial_number") else None
            ),
            location=(str(selector_raw["location"]) if selector_raw.get("location") else None),
            path=(str(selector_raw["path"]) if selector_raw.get("path") else None),
        )
        if selector.claim_key in claims:
            raise ValueError(f"physical device claimed more than once: {selector.claim_key}")
        claims.add(selector.claim_key)
        fake_rows_raw = raw.get("fake_scan_results", ())
        if not isinstance(fake_rows_raw, (list, tuple)):
            raise ValueError(f"ble.devices.{name}.fake_scan_results must be a list")
        if not all(isinstance(row, Mapping) for row in fake_rows_raw):
            raise ValueError(f"ble.devices.{name}.fake_scan_results entries must be mappings")
        devices.append(
            DeviceConfig(
                name=str(name),
                backend=backend,
                selector=selector,
                baud=_baud(raw.get("baud", 57600)),
                reset_timeout_s=_timeout(raw.get("reset_timeout_s", 20.0), "reset timeout"),
                fake_scan_results=tuple(dict(item) for item in fake_rows_raw),
                fake_address=str(raw.get("fake_address", "12:34:56:78:9A:BC")),
                max_rx_line_bytes=raw.get("max_rx_line_bytes", DEFAULT_MAX_RX_LINE_BYTES),
            )
        )
    observers: list[ObserverConfig] = []
    names = {device.name for device in devices}
    for name, raw in raw_observers.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"ble.observers.{name} must be a mapping")
        if str(name) in names:
            raise ValueError(f"BLE logical name used by both device and observer: {name}")
        backend = str(raw.get("backend", "nrf-sniffer")).strip().lower()
        selector_raw = raw.get("selector", {})
        if not isinstance(selector_raw, Mapping):
            raise ValueError(f"ble.observers.{name}.selector must be a mapping")
        selector = SerialDeviceSelector(
            vid=_integer(selector_raw.get("vid")),
            product_ids=_product_ids(selector_raw.get("product_ids", ())),
            serial_number=(
                str(selector_raw["serial_number"]) if selector_raw.get("serial_number") else None
            ),
            location=(str(selector_raw["location"]) if selector_raw.get("location") else None),
            path=(str(selector_raw["path"]) if selector_raw.get("path") else None),
        )
        if selector.claim_key in claims:
            raise ValueError(f"physical device claimed more than once: {selector.claim_key}")
        claims.add(selector.claim_key)
        command_raw = raw.get("extcap_command", ("nrf_sniffer_ble.sh",))
        command: tuple[str, ...]
        if isinstance(command_raw, str):
            command = (command_raw,)
        elif isinstance(command_raw, (list, tuple)):
            if not all(isinstance(item, str) for item in command_raw):
                raise ValueError(f"ble.observers.{name}.extcap_command entries must be strings")
            command = tuple(command_raw)
        else:
            raise ValueError(f"ble.observers.{name}.extcap_command must be a string or list")
        observers.append(
            ObserverConfig(
                name=str(name),
                backend=backend,
                selector=selector,
                extcap_command=command,
                extcap_version=(
                    str(raw["extcap_version"])
                    if raw.get("extcap_version") is not None and raw.get("extcap_version") != "None"
                    else None
                ),
                startup_timeout_s=_timeout(
                    raw.get("startup_timeout_s", 5.0), "observer startup timeout"
                ),
                stop_timeout_s=_timeout(raw.get("stop_timeout_s", 3.0), "observer stop timeout"),
            )
        )
    return BleConfig(
        tuple(devices),
        tuple(observers),
        _command(config.get("capture_decoder_command", ("tshark",)), "capture_decoder_command"),
    )


def _command(raw: object, name: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError(f"{name} must be a nonempty string or list")
    if any(not isinstance(item, str) or not item.strip() for item in raw):
        raise ValueError(f"{name} entries must be nonempty strings")
    return tuple(raw)


@dataclass(frozen=True)
class SnifferSetupConfig:
    name: str
    selector: SerialDeviceSelector
    firmware: Path
    firmware_sha256: str
    nrfutil_command: tuple[str, ...] = ("nrfutil",)
    extcap_command: tuple[str, ...] = ("nrf_sniffer_ble.sh",)
    expected_version: str = "4.1.1"
    timeout_s: float = 120.0
    startup_timeout_s: float = 8.0

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.expected_version.strip():
            raise ValueError("setup name and expected_version must not be empty")
        if self.selector.vid != 0x1915 or not self.selector.location:
            raise ValueError("sniffer setup requires Nordic VID 0x1915 and a stable USB location")
        if self.selector.path or self.selector.serial_number:
            raise ValueError("setup must follow USB location across firmware identity changes")
        if not self.selector.product_ids or not set(self.selector.product_ids) <= {
            0x521A,
            0x521F,
            0x522A,
        }:
            raise ValueError("unsupported sniffer setup product IDs")
        if not {0x521F, 0x522A} <= set(self.selector.product_ids):
            raise ValueError("setup selector must include bootloader 0x521F and sniffer 0x522A")
        if len(self.firmware_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.firmware_sha256
        ):
            raise ValueError("firmware_sha256 must be a lowercase SHA-256 digest")
        for value in (self.timeout_s, self.startup_timeout_s):
            _check_timeout(value, "sniffer setup timeouts")
        _command(self.nrfutil_command, "nrfutil_command")
        _command(self.extcap_command, "extcap_command")


def parse_sniffer_setup(config: Mapping[str, Any]) -> SnifferSetupConfig:
    if config.get("devices") or config.get("observers"):
        raise ValueError("sniffer_setup cannot share a configuration with active BLE owners")
    raw = config.get("sniffer_setup")
    if not isinstance(raw, Mapping):
        raise ValueError("ble.sniffer_setup must be a mapping")
    selector = raw.get("selector", {})
    if not isinstance(selector, Mapping):
        raise ValueError("sniffer_setup.selector must be a mapping")
    pids = selector.get("product_ids", (0x521F, 0x522A))
    if not isinstance(pids, (list, tuple)):
        raise ValueError("sniffer_setup product_ids must be a list")
    firmware = raw.get("firmware")
    if not isinstance(firmware, str) or not firmware.strip():
        raise ValueError("sniffer_setup requires an application-only DFU ZIP")
    return SnifferSetupConfig(
        name=str(raw.get("name", "air")),
        selector=SerialDeviceSelector(
            vid=_integer(selector.get("vid")),
            product_ids=_product_ids(pids),
            location=str(selector["location"]) if selector.get("location") else None,
            serial_number=str(selector["serial_number"]) if selector.get("serial_number") else None,
            path=str(selector["path"]) if selector.get("path") else None,
        ),
        firmware=Path(firmware),
        firmware_sha256=str(raw.get("firmware_sha256", "")),
        nrfutil_command=_command(raw.get("nrfutil_command", ("nrfutil",)), "nrfutil_command"),
        extcap_command=_command(
            raw.get("extcap_command", ("nrf_sniffer_ble.sh",)), "extcap_command"
        ),
        expected_version=str(raw.get("expected_version", "4.1.1")),
        timeout_s=_timeout(raw.get("timeout_s", 120), "sniffer setup timeouts"),
        startup_timeout_s=_timeout(raw.get("startup_timeout_s", 8), "sniffer setup timeouts"),
    )
