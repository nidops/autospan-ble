"""Deterministic BleuIO fake used by unit tests and public simulation scenarios."""

from __future__ import annotations

import json
from queue import Empty, Queue
from threading import Lock
from typing import Iterable

from autospan_ble.bleuio.types import PortInfo


class FakeBleuIOEnvironment:
    def __init__(
        self,
        *,
        endpoint: str,
        vid: int = 0x2DCF,
        pid: int = 0x6002,
        serial_number: str | None = None,
        location: str | None = None,
        scan_results: Iterable[dict] = (),
        address: str = "12:34:56:78:9A:BC",
    ) -> None:
        self.endpoint = endpoint
        self.vid = vid
        self.pid = pid
        self.serial_number = serial_number
        self.location = location
        self.scan_results = tuple(dict(item) for item in scan_results)
        self.address = address
        self.verbose = False
        self.role = "dual"
        self.advertising = False
        self.connected = False
        self.address_mode = "PUBLIC_STATIC_ADDRESS"
        self.advertising_data = ""
        self.reset_count = 0
        self._index = 0
        self._scan_index: int | None = None
        self._lock = Lock()

    def port(self) -> PortInfo:
        return PortInfo(
            device=self.endpoint,
            vid=self.vid,
            pid=self.pid,
            serial_number=self.serial_number,
            location=self.location,
            manufacturer="AutoSpan Fake",
            product="Fake BleuIO",
        )

    def handle(self, command: str) -> list[str]:
        if command == "\x03":
            if self._scan_index is None:
                return []
            return [json.dumps({"SE": self._scan_index, "action": "scan completed"})]
        with self._lock:
            if not self.verbose:
                if command == "ATV1":
                    self.verbose = True
                    return ["VERBOSE ON"]
                if command == "ATE0":
                    return ["ECHO OFF"]
                if command == "AT":
                    return ["OK"]
            self._index += 1
            idx = self._index
            lines: list[dict] = [{"C": idx, "cmd": command}, {"A": idx, "err": 0, "errMsg": "ok"}]
            if command == "ATI":
                lines.append({"R": idx, "fw": "fake-1.0", "hw": "fake", "role": self.role})
            elif command == "AT+GETMAC":
                lines.append({"R": idx, "own_mac_addr": self.address})
            elif command == "AT+GAPSTATUS":
                lines.extend(
                    [
                        {"R": idx, "gap_role": self.role},
                        {"R": idx, "connected": self.connected, "advertising": self.advertising},
                    ]
                )
            elif command == "AT+GAPADDRTYPE":
                lines.append({"R": idx, "addrType": self.address_mode})
            elif command.startswith("AT+GAPADDRTYPE="):
                modes = {
                    "1": "PUBLIC_STATIC_ADDRESS",
                    "2": "PRIVATE_STATIC_ADDRESS",
                    "3": "PRIVATE_RANDOM_RESOLVABLE_ADDRESS",
                    "4": "PRIVATE_RANDOM_NONRESOLVABLE_ADDRESS",
                    "5": "PRIVATE_CNTL",
                }
                code = command.split("=", 1)[1]
                if self.advertising or self.connected:
                    lines[1].update(err=5, errMsg="not allowed")
                elif code not in modes:
                    lines[1].update(err=4, errMsg="invalid parameter")
                else:
                    self.address_mode = modes[code]
            elif command == "AT+CENTRAL":
                if self.role == "central":
                    lines[1].update(err=2, errMsg="already done")
                self.role = "central"
                lines.append({"R": idx, "role": "central"})
            elif command == "AT+PERIPHERAL":
                if self.role == "peripheral":
                    lines[1].update(err=2, errMsg="already done")
                self.role = "peripheral"
                lines.append({"R": idx, "role": "peripheral"})
            elif command.startswith("AT+GAPSCAN"):
                self._scan_index = idx
                lines.append({"R": idx, "action": "scanning"})
            elif command.startswith("AT+SCANTARGET="):
                self._scan_index = idx
                lines.append({"R": idx, "action": "scanning"})
            elif command.startswith("AT+ADVSTART"):
                self.advertising = True
                lines.append({"R": idx, "action": "advertising"})
            elif command == "AT+ADVSTOP":
                self.advertising = False
                lines.append({"R": idx, "info": "stopping advertising"})
            elif command.startswith("AT+GAPCONNECT="):
                self.connected = True
                lines.append({"R": idx, "action": "connecting"})
            elif command == "AT+GAPDISCONNECT":
                self.connected = False
                lines.append({"R": idx, "action": "disconnecting"})
            elif command == "AT+CANCELCONNECT":
                self.connected = False
            elif command == "AT+TARGETCONN":
                lines.append({"R": idx, "target": "0000" if self.connected else "FFFF"})
            elif command == "AT+GATTCREAD=0003":
                if not self.connected:
                    lines[1].update(err=6, errMsg="not connected")
            elif command == "ATR":
                pass
            elif command.startswith("AT+ADVDATA="):
                self.advertising_data = command.split("=", 1)[1].replace(":", "")
                lines.append({"R": idx, "adv": self.advertising_data})
            elif command not in {"AT", "ATE0", "ATV1"}:
                lines[1].update(err=1, errMsg="unknown command")
            lines.append({"E": idx, "nol": len(lines) + 1})
            encoded = [json.dumps(item, separators=(",", ":")) for item in lines]
            if command.startswith("AT+GAPCONNECT="):
                target = command.split("=", 1)[1]
                encoded.append(
                    json.dumps(
                        {"256": "0000", "evt": {"action": "connected", "addr": target[3:].lower()}}
                    )
                )
            elif command == "AT+GAPDISCONNECT":
                encoded.append(json.dumps({"258": "0000", "evt": {"action": "disconnected"}}))
            elif command == "AT+GATTCREAD=0003" and self.connected:
                encoded.extend(
                    [
                        json.dumps(
                            {
                                "775": "0000",
                                "evt": {
                                    "handle": "0003",
                                    "readStatus": 0,
                                    "ascii": "BleuIO",
                                    "len": 6,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "775": "0000",
                                "evt": {
                                    "handle": "0003",
                                    "readStatus": 0,
                                    "hex": "0x426C6575494F",
                                    "len": 6,
                                },
                            }
                        ),
                    ]
                )
            if command.startswith("AT+GAPSCAN"):
                for number, result in enumerate(self.scan_results, start=1):
                    row = {"S": idx, "indx": result.get("indx", number), **result}
                    encoded.append(json.dumps(row, separators=(",", ":")))
                encoded.append(
                    json.dumps(
                        {"SE": idx, "evt": {"action": "scan completed"}}, separators=(",", ":")
                    )
                )
            elif command.startswith("AT+SCANTARGET="):
                target = command.split("=", 1)[1].rsplit("=", 1)[0]
                target_mac = target[3:] if target.startswith("[") else target
                for result in self.scan_results:
                    raw_addr = str(result.get("addr", ""))
                    result_mac = raw_addr[3:] if raw_addr.startswith("[") else raw_addr
                    if result_mac and result_mac.upper() != target_mac.upper():
                        continue
                    if raw_addr.startswith("[") and raw_addr[:3] != target[:3]:
                        continue
                    if "data" not in result:
                        continue
                    row = {"ST": idx, **result}
                    row.pop("indx", None)
                    encoded.append(json.dumps(row, separators=(",", ":")))
                encoded.append(
                    json.dumps(
                        {"SE": idx, "evt": {"action": "scan completed"}}, separators=(",", ":")
                    )
                )
            if command == "AT+ADVSTOP":
                encoded.append(
                    json.dumps(
                        {"260": "FFFF", "evt": {"action": "advertising stopped"}},
                        separators=(",", ":"),
                    )
                )
            if command == "ATR":
                self.reset_count += 1
                base = self.endpoint.split("#", 1)[0]
                self.endpoint = f"{base}#{self.reset_count}"
                self.verbose = False
                self.role = "dual"
                self.advertising = False
                self.connected = False
                self._index = 0
            return encoded


class FakePortEnumerator:
    def __init__(self, environment: FakeBleuIOEnvironment) -> None:
        self._environment = environment

    def ports(self):
        yield self._environment.port()


class FakeLineTransport:
    def __init__(self, environment: FakeBleuIOEnvironment, endpoint: str) -> None:
        self._environment = environment
        self._endpoint = endpoint
        self._rx: Queue[str] = Queue()
        self._open = False

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def open(self) -> None:
        if self._endpoint != self._environment.endpoint:
            raise OSError(f"endpoint {self._endpoint} no longer exists")
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, data: bytes) -> None:
        if not self._open:
            raise OSError("fake transport is closed")
        if self._endpoint != self._environment.endpoint:
            raise OSError(f"endpoint {self._endpoint} disappeared")
        command = data.decode("ascii").strip()
        for line in self._environment.handle(command):
            self._rx.put(line)

    def readline(self, timeout_s: float) -> str | None:
        if not self._open:
            raise OSError("fake transport is closed")
        try:
            return self._rx.get(timeout=max(0.0, timeout_s))
        except Empty:
            return None
