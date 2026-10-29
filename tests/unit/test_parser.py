from autospan_ble.bleuio.parser import parse_line


def test_verbose_command_records_are_classified():
    assert parse_line('{"C":7,"cmd":"ATI"}').kind == "C"
    assert parse_line('{"A":7,"err":0,"errMsg":"ok"}').kind == "A"
    assert parse_line('{"R":7,"fw":"1"}').kind == "R"
    assert parse_line('{"E":7,"nol":4}').kind == "E"


def test_scan_and_unsolicited_events_are_not_command_replies():
    scan = parse_line('{"S":9,"indx":1,"rssi":-58,"addr":"[0]12:34"}')
    target = parse_line('{"ST":9,"addr":"12:34:56:78:9A:BC","type":0,"data":"020106"}')
    found = parse_line('{"SF":9,"addr":"12:34:56:78:9A:BC","type":0,"data":"020106"}')
    done = parse_line('{"SE":9,"evt":{"action":"scan completed"}}')
    event = parse_line('{"260":"FFFF","action":"advertising stopped"}')
    assert (scan.kind, scan.index) == ("S", 9)
    assert (target.kind, target.index) == ("ST", 9)
    assert (found.kind, found.index) == ("SF", 9)
    assert (done.kind, done.index) == ("SE", 9)
    assert event.kind == "event"


def test_observed_firmware_gatt_event_uses_evt_object():
    record = parse_line(
        '{"775":"0000","evt":{"handle":"0003","readStatus":0,"hex":"0x426C6575494F","len":6}}'
    )
    assert record.kind == "event"
    assert record.index is None
    assert record.payload["evt"]["hex"] == "0x426C6575494F"
