import pytest

from autospan_ble.bleuio.address import (
    BleAddress,
    BleAddressKind,
    BleAddressType,
    BleuIOAddressMode,
)


def test_public_bleuio_address_is_typed_and_normalized():
    address = BleAddress.parse("[0]12:34:56:78:9a:bc")
    assert address.value == "12:34:56:78:9A:BC"
    assert address.address_type is BleAddressType.PUBLIC
    assert address.kind is BleAddressKind.PUBLIC
    assert address.bleuio == "[0]12:34:56:78:9A:BC"


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("[1]CC:C1:EE:7B:71:96", BleAddressKind.RANDOM_STATIC),
        ("[1]4C:C1:EE:7B:71:96", BleAddressKind.RESOLVABLE_PRIVATE),
        ("[1]2C:C1:EE:7B:71:96", BleAddressKind.NON_RESOLVABLE_PRIVATE),
        ("[1]8C:C1:EE:7B:71:96", BleAddressKind.RESERVED_RANDOM),
    ],
)
def test_random_address_subtype_is_derived_from_most_significant_bits(raw, kind):
    address = BleAddress.parse(raw)
    assert address.address_type is BleAddressType.RANDOM
    assert address.kind is kind


def test_unprefixed_address_requires_explicit_type():
    with pytest.raises(ValueError, match=r"no \[0\]/\[1\] type prefix"):
        BleAddress.parse("12:34:56:78:9A:BC")
    address = BleAddress.parse("12:34:56:78:9A:BC", fallback_type=BleAddressType.PUBLIC)
    assert address.kind is BleAddressKind.PUBLIC


def test_bleuio_address_modes_map_to_transport_type_and_kind():
    assert BleuIOAddressMode.PUBLIC_STATIC.code == 1
    assert BleuIOAddressMode.PUBLIC_STATIC.address_type is BleAddressType.PUBLIC
    assert BleuIOAddressMode.PRIVATE_STATIC.kind is BleAddressKind.RANDOM_STATIC
    assert BleuIOAddressMode.PRIVATE_RANDOM_RESOLVABLE.kind is BleAddressKind.RESOLVABLE_PRIVATE
    assert (
        BleuIOAddressMode.PRIVATE_RANDOM_NONRESOLVABLE.kind is BleAddressKind.NON_RESOLVABLE_PRIVATE
    )
    assert BleuIOAddressMode.PRIVATE_CONTROLLER.kind is BleAddressKind.RESOLVABLE_PRIVATE


def test_address_type_rejects_bool_and_unknown_codes():
    with pytest.raises(ValueError, match="0/public or 1/random"):
        BleAddress.parse("12:34:56:78:9A:BC", fallback_type=True)
    with pytest.raises(ValueError, match="0/public or 1/random"):
        BleAddress.parse("12:34:56:78:9A:BC", fallback_type=2)
