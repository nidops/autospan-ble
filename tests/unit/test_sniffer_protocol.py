"""Observed Nordic control records; no synthetic RF qualification."""

import pytest

from autospan_ble.nrf_sniffer import _SlipDecoder, _query_firmware_version


def test_fragmented_slip_escaping_and_resynchronization():
    decoder = _SlipDecoder()
    assert decoder.feed(b"noise\xabhello\xcd") == []
    assert decoder.feed(b"\xac\xcd\xbd\xcd\xce\xbc") == [b"hello\xab\xbc\xcd"]
    assert decoder.feed(b"\xababandoned\xabnew\xbc") == [b"new"]
    assert decoder.feed(b"\xabbad\xcd\x00ignored\xbc") == []
    assert decoder.feed(b"\xab" + b"x" * 65542 + b"\xbc") == []
    assert decoder.feed(b"\xabok\xbc") == [b"ok"]


@pytest.mark.parametrize(
    "frame",
    [
        # Protocol 3, observed on physical nRF52840-dongle firmware 4.1.1.
        bytes.fromhex("050003b1191c342e312e31"),
        # Older Nordic UART header: header length, payload length, version, counter, ID.
        bytes.fromhex("060501b1191c342e312e31"),
    ],
)
def test_version_query_ignores_radio_records_and_closes_serial(monkeypatch, frame):
    import serial

    class Link:
        closed = False

        def __init__(self, *args, **kwargs):
            assert args == ("/dev/selected", 1000000)
            assert kwargs["exclusive"]
            self.chunks = iter(
                [
                    b"noise\xab\x00\x00\x03\x00\x00\x02\xbc\xab" + frame[:2],
                    frame[2:4],
                    frame[4:] + b"\xbc",
                ]
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            type(self).closed = True

        def reset_input_buffer(self):
            pass

        def write(self, data):
            assert data == bytes.fromhex("ab06000100001bbc")

        def read(self, size):
            return next(self.chunks, b"")

    monkeypatch.setattr(serial, "Serial", Link)
    assert _query_firmware_version("/dev/selected", 0.1) == "4.1.1"
    assert Link.closed


def test_version_timeout_releases_serial(monkeypatch):
    import serial

    class Silent:
        closed = False

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            type(self).closed = True

        def reset_input_buffer(self):
            pass

        def write(self, data):
            pass

        def read(self, size):
            return b""

    monkeypatch.setattr(serial, "Serial", Silent)
    with pytest.raises(TimeoutError, match="version request"):
        _query_firmware_version("/dev/silent", 0.01)
    assert Silent.closed
