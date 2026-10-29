"""Bluetooth LE address parsing and classification for BleuIO records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
import re


_MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_PREFIXED_RE = re.compile(r"^\[(?P<type>[01])\](?P<mac>(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})$")


class BleAddressType(IntEnum):
    """Address type used by BleuIO scan/connect syntax."""

    PUBLIC = 0
    RANDOM = 1


class BleAddressKind(str, Enum):
    """Bluetooth LE address kind, including random-address subtypes."""

    PUBLIC = "public"
    RANDOM_STATIC = "random-static"
    RESOLVABLE_PRIVATE = "resolvable-private"
    NON_RESOLVABLE_PRIVATE = "non-resolvable-private"
    RESERVED_RANDOM = "reserved-random"


class BleuIOAddressMode(str, Enum):
    """Values returned by ``AT+GAPADDRTYPE``."""

    PUBLIC_STATIC = "PUBLIC_STATIC_ADDRESS"
    PRIVATE_STATIC = "PRIVATE_STATIC_ADDRESS"
    PRIVATE_RANDOM_RESOLVABLE = "PRIVATE_RANDOM_RESOLVABLE_ADDRESS"
    PRIVATE_RANDOM_NONRESOLVABLE = "PRIVATE_RANDOM_NONRESOLVABLE_ADDRESS"
    PRIVATE_CONTROLLER = "PRIVATE_CNTL"

    @property
    def code(self) -> int:
        return {
            BleuIOAddressMode.PUBLIC_STATIC: 1,
            BleuIOAddressMode.PRIVATE_STATIC: 2,
            BleuIOAddressMode.PRIVATE_RANDOM_RESOLVABLE: 3,
            BleuIOAddressMode.PRIVATE_RANDOM_NONRESOLVABLE: 4,
            BleuIOAddressMode.PRIVATE_CONTROLLER: 5,
        }[self]

    @property
    def address_type(self) -> BleAddressType:
        if self is BleuIOAddressMode.PUBLIC_STATIC:
            return BleAddressType.PUBLIC
        return BleAddressType.RANDOM

    @property
    def kind(self) -> BleAddressKind:
        return {
            BleuIOAddressMode.PUBLIC_STATIC: BleAddressKind.PUBLIC,
            BleuIOAddressMode.PRIVATE_STATIC: BleAddressKind.RANDOM_STATIC,
            BleuIOAddressMode.PRIVATE_RANDOM_RESOLVABLE: BleAddressKind.RESOLVABLE_PRIVATE,
            BleuIOAddressMode.PRIVATE_RANDOM_NONRESOLVABLE: BleAddressKind.NON_RESOLVABLE_PRIVATE,
            BleuIOAddressMode.PRIVATE_CONTROLLER: BleAddressKind.RESOLVABLE_PRIVATE,
        }[self]


@dataclass(frozen=True)
class BleAddress:
    """Normalized Bluetooth LE device address with its transport address type."""

    value: str
    address_type: BleAddressType
    kind: BleAddressKind

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not _MAC_RE.fullmatch(self.value):
            raise ValueError(f"invalid Bluetooth address: {self.value!r}")
        address_type = coerce_address_type(self.address_type)
        mac = self.value.upper()
        expected = _kind_for(mac, address_type)
        if self.kind != expected:
            raise ValueError("Bluetooth address kind conflicts with its type and address bits")
        object.__setattr__(self, "value", mac)
        object.__setattr__(self, "address_type", address_type)
        object.__setattr__(self, "kind", expected)

    @classmethod
    def parse(
        cls,
        value: str,
        *,
        fallback_type: BleAddressType | int | None = None,
    ) -> "BleAddress":
        if not isinstance(value, str):
            raise ValueError("Bluetooth address must be a string")
        explicit_type = None if fallback_type is None else coerce_address_type(fallback_type)
        raw = value.strip()
        prefixed = _PREFIXED_RE.fullmatch(raw)
        if prefixed is not None:
            address_type = BleAddressType(int(prefixed.group("type")))
            if explicit_type is not None and explicit_type is not address_type:
                raise ValueError("Bluetooth address type conflicts with address prefix")
            mac = prefixed.group("mac").upper()
        else:
            if not _MAC_RE.fullmatch(raw):
                raise ValueError(f"invalid Bluetooth address: {value!r}")
            if fallback_type is None:
                raise ValueError(
                    f"Bluetooth address {value!r} has no [0]/[1] type prefix and no fallback type"
                )
            assert explicit_type is not None
            address_type = explicit_type
            mac = raw.upper()
        return cls(mac, address_type, _kind_for(mac, address_type))

    @property
    def bleuio(self) -> str:
        """Address formatted for BleuIO commands such as ``AT+GAPCONNECT``."""
        return f"[{int(self.address_type)}]{self.value}"

    def __str__(self) -> str:
        return self.bleuio


def _kind_for(mac: str, address_type: BleAddressType) -> BleAddressKind:
    if address_type is BleAddressType.PUBLIC:
        return BleAddressKind.PUBLIC
    most_significant_octet = int(mac.split(":", 1)[0], 16)
    subtype = (most_significant_octet >> 6) & 0b11
    return {
        0b00: BleAddressKind.NON_RESOLVABLE_PRIVATE,
        0b01: BleAddressKind.RESOLVABLE_PRIVATE,
        0b10: BleAddressKind.RESERVED_RANDOM,
        0b11: BleAddressKind.RANDOM_STATIC,
    }[subtype]


def coerce_address_type(value: int | BleAddressType) -> BleAddressType:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Bluetooth address type must be 0/public or 1/random")
    try:
        return BleAddressType(value)
    except ValueError as exc:
        raise ValueError("Bluetooth address type must be 0/public or 1/random") from exc
