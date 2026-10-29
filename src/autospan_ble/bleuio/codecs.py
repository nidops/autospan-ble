"""Built-in BLE byte layouts compiled once by StructWire."""

from enum import StrEnum
from importlib.resources import as_file, files

from structwire import Catalog, Codec, catalog_from_yaml


class BleCodec(StrEnum):
    """Stable identifiers that match ``common-codecs.yaml`` exactly."""

    AD_HEADER = "ad_header"
    COMPANY_ID = "company_id"
    ADVERTISING_FLAGS = "advertising_flags"
    TX_POWER = "tx_power"
    UUID16 = "uuid16"
    UUID32 = "uuid32"
    UUID128 = "uuid128"
    DEVICE_NAME = "bleuio_device_name"


with as_file(files(__package__).joinpath("common-codecs.yaml")) as _schema_path:
    _CATALOG: Catalog = catalog_from_yaml(_schema_path)

_expected = {item.value for item in BleCodec}
_actual = set(_CATALOG)
if _actual != _expected:
    missing = sorted(_expected - _actual)
    extra = sorted(_actual - _expected)
    raise RuntimeError(
        "built-in BLE codec identifiers do not match common-codecs.yaml "
        f"(missing={missing}, extra={extra})"
    )


def codec(codec_id: BleCodec | str) -> Codec:
    """Return one built-in StructWire codec by its stable identifier."""
    try:
        key = BleCodec(str(codec_id))
    except ValueError as exc:
        raise KeyError(f"unknown BLE codec identifier: {codec_id!r}") from exc
    return _CATALOG[key.value]


__all__ = ["BleCodec", "codec"]
