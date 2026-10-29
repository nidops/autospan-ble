import pytest

from autospan_ble.bleuio.transport import PySerialLineTransport


def test_serial_timeout_preserves_fragmented_json_record():
    class Serial:
        def __init__(self):
            self.fragments = iter([b'{"R":7,', b'"fwVer":"2.7.9.78"}', b"\r\n"])

        def read_until(self, *, size=None):
            return next(self.fragments)

    transport = PySerialLineTransport("unused")
    transport._serial = Serial()
    assert transport.readline(0.01) is None
    assert transport.readline(0.01) is None
    assert transport.readline(0.01) == '{"R":7,"fwVer":"2.7.9.78"}'


def test_posix_serial_open_is_exclusive():
    import os
    import pytest

    if os.name != "posix":
        pytest.skip("POSIX exclusive serial ownership")
    import pty

    master, slave = pty.openpty()
    endpoint = os.ttyname(slave)
    first = PySerialLineTransport(endpoint)
    second = PySerialLineTransport(endpoint)
    try:
        first.open()
        with pytest.raises(OSError, match="exclusive"):
            second.open()
        first.close()
        second.open()
    finally:
        first.close()
        second.close()
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize(
    "fragments, expected_sizes, succeeds",
    [
        ([b"abc\n"], [5], True),
        ([b"ab", b"c\n"], [5, 3], True),
        ([b"abcd", b"\n"], [5, 1], False),
        ([b"abcde"], [5], False),
    ],
)
def test_receive_limit_bounds_each_read_and_latches_overflow(
    monkeypatch, fragments, expected_sizes, succeeds
):
    import serial
    from autospan_ble.bleuio.errors import BleuIOError

    class Serial:
        def __init__(self, *args, **kwargs):
            self.fragments = iter(fragments)
            self.sizes = []

        def read_until(self, *, size):
            self.sizes.append(size)
            data = next(self.fragments)
            assert len(data) <= size
            return data

        def close(self):
            pass

    monkeypatch.setattr(serial, "Serial", Serial)
    transport = PySerialLineTransport("unused", max_rx_line_bytes=4)
    transport.open()
    owner = transport._serial
    try:
        if succeeds:
            for _ in fragments:
                result = transport.readline(0.1)
            assert result == "abc"
        else:
            with pytest.raises(BleuIOError, match="exceeds"):
                for _ in fragments:
                    transport.readline(0.1)
            assert not transport._rx_buffer
            transport.open()  # Opening an already open port must not clear failure.
            with pytest.raises(BleuIOError, match="framing failed"):
                transport.readline(0.1)
            transport.close()
            transport.open()
            transport._serial.fragments = iter([b"ok\n"])
            assert transport.readline(0.1) == "ok"
        assert owner.sizes == expected_sizes
    finally:
        transport.close()


@pytest.mark.parametrize("value", [True, False, 0, -1, 1048577, 1.5, "100", None])
def test_receive_limit_rejects_invalid_config_and_constructor_values(value):
    from autospan_ble.config import parse_config

    with pytest.raises(ValueError, match="max_rx_line_bytes"):
        PySerialLineTransport("unused", max_rx_line_bytes=value)
    with pytest.raises(ValueError, match="max_rx_line_bytes"):
        parse_config(
            {"devices": {"dut": {"selector": {"path": "unused"}, "max_rx_line_bytes": value}}}
        )


@pytest.mark.parametrize(
    "options, expected",
    [({}, 65536), ({"max_rx_line_bytes": 1}, 1), ({"max_rx_line_bytes": 1048576}, 1048576)],
)
def test_receive_limit_reaches_physical_transport_without_acquiring_hardware(options, expected):
    from autospan_ble.config import parse_config
    from autospan_ble.plugin import _build_device

    config = parse_config(
        {"devices": {"dut": {"selector": {"path": "unused"}, **options}}}
    ).devices[0]
    device = _build_device(None, config)
    transport = device._transport_builder("unused")
    assert transport._max_rx_line_bytes == expected
    assert transport._serial is None
