"""Built-in GATT identifiers used by the reference BleuIO scenarios."""

from enum import StrEnum

from structwire import Codec

from autospan_ble.bleuio.codecs import BleCodec, codec


class BleGatt(StrEnum):
    """Logical built-in attributes whose values match their YAML codec identifier."""

    DEVICE_NAME = BleCodec.DEVICE_NAME.value

    @property
    def handle(self) -> int:
        return {BleGatt.DEVICE_NAME: 0x0003}[self]

    @property
    def codec_id(self) -> BleCodec:
        return BleCodec(self.value)

    @property
    def codec(self) -> Codec:
        return codec(self.codec_id)


__all__ = ["BleGatt"]
