"""Bluetooth advertising payload parsing and project-codec integration."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from uuid import UUID

from structwire import DecodeError

from autospan_ble.bleuio.address import BleAddress
from autospan_ble.bleuio.codecs import BleCodec, codec
from autospan_ble.bleuio.errors import BleuIOError


class PayloadDecoder(Protocol):
    """Structural decoder implemented by StructWire ``Codec`` and similar codecs."""

    def decode(self, payload: bytes) -> dict[str, Any]: ...


class AdvertisingDataError(BleuIOError):
    """Malformed Bluetooth advertising data."""


@dataclass(frozen=True)
class ManufacturerData:
    """Manufacturer-specific AD data with the company identifier removed from payload."""

    company_id: int
    payload: bytes

    def __post_init__(self) -> None:
        _company_id(self.company_id)
        object.__setattr__(self, "payload", _bytes(self.payload))

    def decode(self, codec: PayloadDecoder) -> dict[str, Any]:
        """Decode the vendor payload with a project-owned codec such as StructWire."""
        return codec.decode(self.payload)

    def to_ad_structure(self) -> AdStructure:
        """Encode the common company-ID prefix and wrap the application bytes."""
        prefix = codec(BleCodec.COMPANY_ID).encode({"company_id": self.company_id})
        return AdStructure(0xFF, prefix + self.payload)


@dataclass(frozen=True)
class AdStructure:
    """One Bluetooth advertising-data structure: type plus value bytes."""

    ad_type: int
    data: bytes

    def __post_init__(self) -> None:
        if (
            not isinstance(self.ad_type, int)
            or isinstance(self.ad_type, bool)
            or not 0 <= self.ad_type <= 0xFF
        ):
            raise ValueError("AD type must be between 0x00 and 0xFF")
        object.__setattr__(self, "data", _bytes(self.data))
        if len(self.data) > 0xFE:
            raise ValueError("AD data is too long for a one-octet AD length")

    def to_bytes(self) -> bytes:
        header = codec(BleCodec.AD_HEADER).encode(
            {"length": len(self.data) + 1, "ad_type": self.ad_type}
        )
        return header + self.data

    @property
    def local_name(self) -> str | None:
        if self.ad_type not in {0x08, 0x09}:
            return None
        try:
            return self.data.rstrip(b"\x00").decode("utf-8")
        except UnicodeDecodeError:
            return None

    @property
    def manufacturer(self) -> ManufacturerData | None:
        if self.ad_type != 0xFF:
            return None
        if len(self.data) < 2:
            raise AdvertisingDataError("manufacturer-specific AD data is missing company ID")
        # Bluetooth AD multi-octet integer fields are transmitted least-significant octet first.
        values = _decode_common(BleCodec.COMPANY_ID, self.data[:2])
        return ManufacturerData(values["company_id"], self.data[2:])

    @property
    def flags(self) -> dict[str, Any] | None:
        """Named first-octet Flags; absent AD type is unknown, an empty value is zero."""
        if self.ad_type != 0x01:
            return None
        return _decode_common(BleCodec.ADVERTISING_FLAGS, self.data[:1] or b"\x00")

    @property
    def tx_power_dbm(self) -> int | None:
        if self.ad_type != 0x0A:
            return None
        value: int = _decode_common(BleCodec.TX_POWER, self.data)["tx_power_dbm"]
        if value == -128:
            raise AdvertisingDataError("TX Power Level must be between -127 and 127 dBm")
        return value

    @property
    def service_uuids(self) -> tuple[str, ...]:
        """Service UUID lists in canonical UUID notation, retaining list order."""
        codec_id = {
            2: BleCodec.UUID16,
            3: BleCodec.UUID16,
            4: BleCodec.UUID32,
            5: BleCodec.UUID32,
            6: BleCodec.UUID128,
            7: BleCodec.UUID128,
        }.get(self.ad_type)
        if codec_id is None:
            return ()
        value_codec = codec(codec_id)
        if len(self.data) % value_codec.size:
            raise AdvertisingDataError(
                f"service UUID list must contain whole {value_codec.size}-byte UUIDs"
            )
        result: list[str] = []
        for offset in range(0, len(self.data), value_codec.size):
            value = value_codec.decode(self.data[offset : offset + value_codec.size])["uuid"]
            if value_codec.size == 16:
                # BLE serializes the entire UUID least-significant octet first.
                # uuid.UUID(bytes_le=...) instead uses Microsoft's mixed-endian GUID convention.
                result.append(str(UUID(bytes=value[::-1])))
            else:
                result.append(f"{value:08x}-0000-1000-8000-00805f9b34fb")
        return tuple(result)


@dataclass(frozen=True)
class AdvertisingData:
    """Parsed Bluetooth AD structures plus the original byte sequence."""

    raw: bytes
    structures: tuple[AdStructure, ...]

    @classmethod
    def parse(cls, payload: bytes | bytearray | memoryview) -> "AdvertisingData":
        """Copy a bytes-like AD payload into immutable structures, retaining raw bytes.

        Zero length terminates padding. Truncated structures or nonzero bytes
        after the terminator raise ``AdvertisingDataError``; other input types
        raise ``ValueError``. Field-specific decoding occurs when helpers are read.
        """
        raw = _bytes(payload)
        structures: list[AdStructure] = []
        offset = 0
        while offset < len(raw):
            length = raw[offset]
            if length == 0:
                if any(raw[offset + 1 :]):
                    raise AdvertisingDataError("non-zero data follows AD terminator")
                break
            end = offset + 1 + length
            if end > len(raw):
                raise AdvertisingDataError(
                    f"AD structure at byte {offset} declares {length} bytes beyond payload"
                )
            header = _decode_common(BleCodec.AD_HEADER, raw[offset : offset + 2])
            structures.append(AdStructure(header["ad_type"], raw[offset + 2 : end]))
            offset = end
        return cls(raw, tuple(structures))

    @classmethod
    def build(cls, *structures: AdStructure) -> "AdvertisingData":
        """Serialize structures in order without padding or a radio-size limit.

        Each ``AdStructure`` validates its length on construction. The endpoint
        validates the firmware's payload limit before starting advertising.
        """
        raw = b"".join(item.to_bytes() for item in structures)
        return cls(raw, tuple(structures))

    @property
    def local_name(self) -> str | None:
        complete: str | None = None
        shortened: str | None = None
        for item in self.structures:
            name = item.local_name
            if name is None:
                continue
            if item.ad_type == 0x09:
                complete = name
            elif shortened is None:
                shortened = name
        return complete if complete is not None else shortened

    def by_type(self, ad_type: int) -> tuple[AdStructure, ...]:
        return tuple(item for item in self.structures if item.ad_type == ad_type)

    @property
    def flags(self) -> dict[str, Any] | None:
        """Interpret the first Flags AD structure, or return None if absent."""
        return next((item.flags for item in self.structures if item.ad_type == 0x01), None)

    @property
    def tx_power_dbm(self) -> int | None:
        """Interpret the first TX Power Level AD structure, or return None if absent."""
        return next((item.tx_power_dbm for item in self.structures if item.ad_type == 0x0A), None)

    @property
    def service_uuids(self) -> tuple[str, ...]:
        """Aggregate service UUID lists without duplicates, preserving first-seen order."""
        return tuple(dict.fromkeys(uuid for item in self.structures for uuid in item.service_uuids))

    def manufacturer_data(self, company_id: int | None = None) -> tuple[ManufacturerData, ...]:
        if company_id is not None:
            _company_id(company_id)
        result: list[ManufacturerData] = []
        for item in self.structures:
            manufacturer = item.manufacturer
            if manufacturer is None:
                continue
            if company_id is None or manufacturer.company_id == company_id:
                result.append(manufacturer)
        return tuple(result)


@dataclass(frozen=True)
class AdvertisingReport:
    """One ``AT+SCANTARGET`` advertising or scan-response record."""

    address: BleAddress
    data_type: int
    payload: bytes
    data: AdvertisingData
    rssi: int | None = None
    manufacturer_id: int | None = None
    fields: Mapping[str, Any] | None = None

    @property
    def is_advertisement(self) -> bool:
        # Legacy HCI report types: ADV_IND, ADV_DIRECT_IND, ADV_SCAN_IND, ADV_NONCONN_IND.
        return self.data_type in {0, 1, 2, 3}

    @property
    def is_scan_response(self) -> bool:
        # BleuIO's official AT+SCANTARGET examples use 4 for RESP records.
        return self.data_type == 4


class AdvertisingReports(tuple[AdvertisingReport, ...]):
    """Immutable target-scan reports with a small expectation helper."""

    __slots__ = ()

    def __new__(cls, reports: Iterable[AdvertisingReport] = ()) -> AdvertisingReports:
        return super().__new__(cls, reports)

    def expect(
        self,
        *,
        manufacturer_id: int | None = None,
        data_type: int | None = None,
        local_name: str | None = None,
    ) -> AdvertisingReport:
        """Return the first report satisfying every supplied criterion.

        Invalid criteria raise ``ValueError``; no match raises ``AssertionError``
        with observed records. Manufacturer matching checks metadata and AD bytes.
        The original immutable reports remain available without filtering.
        """
        candidates = list(self)
        if manufacturer_id is not None:
            _company_id(manufacturer_id)
            candidates = [
                row
                for row in candidates
                if row.manufacturer_id == manufacturer_id
                or any(item.company_id == manufacturer_id for item in row.data.manufacturer_data())
            ]
        if data_type is not None:
            if (
                not isinstance(data_type, int)
                or isinstance(data_type, bool)
                or not 0 <= data_type <= 0xFF
            ):
                raise ValueError("advertising data type must be between 0x00 and 0xFF")
            candidates = [row for row in candidates if row.data_type == data_type]
        if local_name is not None:
            if not isinstance(local_name, str):
                raise ValueError("expected local name must be a string")
            candidates = [row for row in candidates if row.data.local_name == local_name]
        if candidates:
            return candidates[0]
        expected_bits = []
        if manufacturer_id is not None:
            expected_bits.append(f"manufacturer_id={_format_company(manufacturer_id)}")
        if data_type is not None:
            expected_bits.append(f"data_type={data_type}")
        if local_name is not None:
            expected_bits.append(f"local_name={local_name!r}")
        expectation = ", ".join(expected_bits) if expected_bits else "at least one report"
        observed = (
            "\n".join(
                f"  {row.address.bleuio} type={row.data_type} "
                f"manufacturer_id={_format_company(row.manufacturer_id)} name={row.data.local_name!r} "
                f"payload={row.payload.hex()}"
                for row in self
            )
            or "  <none>"
        )
        raise AssertionError(
            f"expected advertising report ({expectation}) was not observed:\n{observed}"
        )


def _bytes(value: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("payload must be bytes, bytearray or memoryview")
    return bytes(value)


def _decode_common(name: BleCodec, payload: bytes) -> dict[str, Any]:
    try:
        return codec(name).decode(payload)
    except DecodeError as exc:
        raise AdvertisingDataError(f"invalid {name.value} AD data: {exc}") from exc


def _format_company(value: int | None) -> str:
    return "None" if value is None else f"0x{value:04X}"


def _company_id(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFF:
        raise ValueError("company ID must be between 0x0000 and 0xFFFF")
