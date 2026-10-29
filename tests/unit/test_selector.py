import pytest

from autospan_ble.bleuio.errors import AmbiguousDevice, DeviceNotFound
from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
from autospan_ble.bleuio.types import PortInfo


class Ports:
    def __init__(self, *ports):
        self._ports = ports

    def ports(self):
        return iter(self._ports)


def test_selector_requires_stable_identity():
    with pytest.raises(ValueError, match="stable identity"):
        SerialDeviceSelector(vid=0x2DCF, product_ids=(0x6002,))


def test_serial_selector_survives_endpoint_change():
    selector = SerialDeviceSelector(vid=0x2DCF, serial_number="ABC")
    resolver = SerialResolver(
        Ports(PortInfo("/dev/ttyACM4", vid=0x2DCF, pid=0x6002, serial_number="ABC"))
    )
    assert resolver.resolve(selector).device == "/dev/ttyACM4"
    assert selector.claim_key == "serial:vid=2dcf:sn=ABC"


def test_ambiguous_selector_is_rejected():
    selector = SerialDeviceSelector(vid=0x2DCF, location="1-2")
    resolver = SerialResolver(
        Ports(
            PortInfo("a", vid=0x2DCF, location="1-2"),
            PortInfo("b", vid=0x2DCF, location="1-2"),
        )
    )
    with pytest.raises(AmbiguousDevice):
        resolver.resolve(selector)


def test_missing_selector_is_clear():
    selector = SerialDeviceSelector(path="fake://missing")
    with pytest.raises(DeviceNotFound):
        SerialResolver(Ports()).resolve(selector)


def test_pyserial_linux_metadata_disappearance_is_retryable(monkeypatch):
    from serial.tools import list_ports
    from autospan_ble.bleuio.errors import PortInventoryChanged
    from autospan_ble.bleuio.selector import PySerialPortEnumerator

    namespace = {"__name__": "serial.tools.list_ports_linux"}
    exec("def vanished():\n    return int(None, 16)", namespace)
    monkeypatch.setattr(list_ports, "comports", namespace["vanished"])
    with pytest.raises(PortInventoryChanged):
        tuple(PySerialPortEnumerator().ports())


def test_arbitrary_enumeration_type_error_is_not_hidden(monkeypatch):
    from serial.tools import list_ports
    from autospan_ble.bleuio.selector import PySerialPortEnumerator

    def broken():
        raise TypeError("programming error")

    monkeypatch.setattr(list_ports, "comports", broken)
    with pytest.raises(TypeError, match="programming error"):
        tuple(PySerialPortEnumerator().ports())


def test_path_selector_follows_existing_by_id_symlink(tmp_path):
    endpoint = tmp_path / "tty-new"
    endpoint.touch()
    link = tmp_path / "by-id"
    link.symlink_to(endpoint)
    selector = SerialDeviceSelector(path=str(link))
    assert selector.matches(PortInfo(str(endpoint)))


def test_usb_port_location_survives_firmware_interface_changes():
    selector = SerialDeviceSelector(vid=0x1915, location="1-2.3")
    assert selector.matches(PortInfo("boot", vid=0x1915, location="1-2.3:1.0"))
    assert selector.matches(PortInfo("app", vid=0x1915, location="1-2.3:1.2"))
    assert not selector.matches(PortInfo("other", vid=0x1915, location="1-2.30:1.0"))
    interface = SerialDeviceSelector(location="1-2.3:1.0")
    assert not interface.matches(PortInfo("app", location="1-2.3:1.2"))
