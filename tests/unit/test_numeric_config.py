"""Configuration validation only: no device, capture, or firmware access."""

from dataclasses import replace

import pytest

from autospan_ble.bleuio.selector import SerialDeviceSelector
from autospan_ble.bleuio.transport import PySerialLineTransport
from autospan_ble.config import DeviceConfig, ObserverConfig, parse_config, parse_sniffer_setup


@pytest.mark.parametrize("scope", ["devices", "observers"])
@pytest.mark.parametrize("field", ["vid", "product_ids"])
@pytest.mark.parametrize("value", [True, False, 1.5, -1, 65536, "0x10000", "-1"])
def test_invalid_usb_ids_are_rejected(scope, field, value):
    selector = {"path": "unused", field: [value] if field == "product_ids" else value}
    with pytest.raises(ValueError):
        parse_config({scope: {"dut": {"selector": selector}}})


@pytest.mark.parametrize("scope", ["devices", "observers"])
@pytest.mark.parametrize("value", [None, [None], 1.5, {"pid": 1}])
def test_invalid_product_id_containers_do_not_remove_constraints(scope, value):
    with pytest.raises(ValueError):
        parse_config({scope: {"dut": {"selector": {"path": "unused", "product_ids": value}}}})


@pytest.mark.parametrize("field", ["vid", "product_ids"])
@pytest.mark.parametrize("value", [True, False, 1.5, -1, 65536])
def test_direct_selector_rejects_invalid_usb_ids(field, value):
    kwargs = {field: (value,) if field == "product_ids" else value}
    with pytest.raises(ValueError, match="VID/PID"):
        SerialDeviceSelector(path="unused", **kwargs)


@pytest.mark.parametrize("scope", ["devices", "observers"])
@pytest.mark.parametrize("value", [0, 65535, "0x0000", "0xFFFF"])
def test_usb_boundaries_and_hex_strings_remain_valid(scope, value):
    result = parse_config(
        {scope: {"dut": {"selector": {"path": "unused", "vid": value, "product_ids": value}}}}
    )
    config = result.devices[0] if scope == "devices" else result.observers[0]
    expected = int(value, 0) if isinstance(value, str) else value
    assert config.selector.vid == expected
    assert config.selector.product_ids == (expected,)


@pytest.mark.parametrize(
    "value", [True, False, 1.5, 0, -1, float("nan"), float("inf"), None, "57600.5"]
)
def test_invalid_baud_rejected_by_parser_and_constructors(value):
    selector = SerialDeviceSelector(path="unused")
    with pytest.raises(ValueError):
        parse_config({"devices": {"dut": {"selector": {"path": "unused"}, "baud": value}}})
    with pytest.raises(ValueError, match="baud"):
        DeviceConfig("dut", "pyserial", selector, baud=value)
    with pytest.raises(ValueError, match="baud"):
        PySerialLineTransport("unused", baud=value)


@pytest.mark.parametrize("value", [57600, "57600", "00057600", 500001])
def test_positive_baud_is_not_restricted_to_a_whitelist(value):
    result = parse_config({"devices": {"dut": {"selector": {"path": "unused"}, "baud": value}}})
    assert result.devices[0].baud == int(value)


def timeout_config(scope, field, value):
    if scope == "setup":
        return parse_sniffer_setup(
            {
                "sniffer_setup": {
                    "selector": {
                        "vid": "0x1915",
                        "product_ids": ["0x521f", "0x522a"],
                        "location": "1-2",
                    },
                    "firmware": "application.zip",
                    "firmware_sha256": "0" * 64,
                    field: value,
                }
            }
        )
    result = parse_config({scope: {"dut": {"selector": {"path": "unused"}, field: value}}})
    return result.devices[0] if scope == "devices" else result.observers[0]


TIMEOUT_FIELDS = [
    ("devices", "reset_timeout_s"),
    ("observers", "startup_timeout_s"),
    ("observers", "stop_timeout_s"),
    ("setup", "timeout_s"),
    ("setup", "startup_timeout_s"),
]


@pytest.mark.parametrize("scope,field", TIMEOUT_FIELDS)
@pytest.mark.parametrize(
    "value",
    [True, False, 0, -1, float("nan"), float("inf"), -float("inf"), "nan", "bad", None, 10**400],
)
def test_invalid_timeouts_rejected_before_and_after_conversion(scope, field, value):
    with pytest.raises(ValueError, match="finite"):
        timeout_config(scope, field, value)
    baseline = timeout_config(scope, field, 1)
    with pytest.raises(ValueError, match="finite"):
        replace(baseline, **{field: value})


@pytest.mark.parametrize("scope,field", TIMEOUT_FIELDS)
@pytest.mark.parametrize("value", [1, 0.5, "0.5"])
def test_valid_timeout_formats_remain_supported(scope, field, value):
    assert getattr(timeout_config(scope, field, value), field) == float(value)


def test_maintenance_product_ids_do_not_silently_drop_null():
    with pytest.raises(ValueError):
        parse_sniffer_setup(
            {
                "sniffer_setup": {
                    "selector": {
                        "vid": "0x1915",
                        "product_ids": ["0x521f", "0x522a", None],
                        "location": "1-2",
                    },
                    "firmware": "application.zip",
                    "firmware_sha256": "0" * 64,
                }
            }
        )


def test_defaults_remain_unchanged():
    selector = SerialDeviceSelector(path="unused")
    device = DeviceConfig("dut", "pyserial", selector)
    observer = ObserverConfig("air", "nrf-sniffer", selector)
    assert (device.baud, device.reset_timeout_s, device.max_rx_line_bytes) == (57600, 20.0, 65536)
    assert (observer.startup_timeout_s, observer.stop_timeout_s) == (5.0, 3.0)
