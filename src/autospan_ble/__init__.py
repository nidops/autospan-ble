"""AutoSpan BLE reference plugin."""

from importlib.metadata import PackageNotFoundError, version

from autospan_ble.bleuio import (
    AdStructure,
    AdvertisingData,
    AdvertisingDataError,
    AdvertisingReport,
    AdvertisingReports,
    BleAddress,
    BleAddressKind,
    BleAddressType,
    BleCodec,
    BleGatt,
    BleuIOAddressMode,
    GattValue,
    ManufacturerData,
    PayloadDecoder,
    ScanResult,
    ScanResults,
    codec,
)
from autospan_ble.facade import BleBench
from autospan_ble.nrf_sniffer import NrfSnifferObserver
from autospan_ble.observer import BleObserver, CaptureSession, CaptureResult, ObserverState

__all__ = [
    "AdStructure",
    "AdvertisingData",
    "AdvertisingDataError",
    "AdvertisingReport",
    "AdvertisingReports",
    "BleAddress",
    "BleAddressKind",
    "BleAddressType",
    "BleCodec",
    "BleGatt",
    "BleuIOAddressMode",
    "GattValue",
    "ManufacturerData",
    "PayloadDecoder",
    "ScanResult",
    "ScanResults",
    "codec",
    "NrfSnifferObserver",
    "BleBench",
    "BleObserver",
    "CaptureSession",
    "CaptureResult",
    "ObserverState",
]
try:
    __version__ = version("autospan-ble")
except PackageNotFoundError:  # source checkout before installation
    __version__ = "0+unknown"
