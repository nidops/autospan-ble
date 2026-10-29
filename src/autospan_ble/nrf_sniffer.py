"""Nordic extcap translation and owned POSIX capture sessions.

The USB selector is shared with the endpoint backend; BLE actions are not. Nordic
owns the sniffer protocol. A session owns its FIFO, process group, copy worker and
output. AutoSpan evidence is supplied by the bench facade, not this adapter.
"""

from __future__ import annotations

import os
import re
import signal
import struct
from math import isfinite
from pathlib import Path
from shutil import which
from subprocess import DEVNULL, Popen, TimeoutExpired
from tempfile import TemporaryDirectory, TemporaryFile
from threading import Event, RLock, Thread
from time import monotonic, monotonic_ns
from types import TracebackType
from typing import BinaryIO

from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
from autospan_ble.bleuio.types import PortInfo
from autospan_ble.observer.api import CaptureSession
from autospan_ble.observer.types import CaptureResult, ObserverState


class _SlipDecoder:
    """Nordic UART framing; bounded even if a device never terminates a frame."""

    def __init__(self) -> None:
        self._frame: bytearray | None = None
        self._escaped = False

    def feed(self, data: bytes) -> list[bytes]:
        frames = []
        for value in data:
            if self._escaped:
                self._escaped = False
                if self._frame is not None:
                    decoded = {0xAC: 0xAB, 0xBD: 0xBC, 0xCE: 0xCD}.get(value)
                    if decoded is None:
                        self._frame = None
                    else:
                        self._frame.append(decoded)
            elif value == 0xAB:
                self._frame = bytearray()
            elif value == 0xBC:
                if self._frame is not None:
                    frames.append(bytes(self._frame))
                self._frame = None
            elif self._frame is not None:
                if value == 0xCD:
                    self._escaped = True
                else:
                    self._frame.append(value)
            if self._frame is not None and len(self._frame) > 65541:
                self._frame = None
        return frames


def _query_firmware_version(endpoint: str, timeout_s: float) -> str:
    """Request Nordic's UART version reply when extcap only lists USB identity.

    Request 0x1B and response 0x1C are also used by nrfutil ble-sniffer.
    The observed 4.1.1 device replies using protocol 3 with an ASCII version.
    """
    from serial import Serial

    deadline = monotonic() + timeout_s
    decoder = _SlipDecoder()
    with Serial(
        endpoint, 1000000, timeout=min(0.05, timeout_s), write_timeout=timeout_s, exclusive=True
    ) as link:
        link.reset_input_buffer()
        link.write(bytes.fromhex("ab06000100001bbc"))
        while monotonic() < deadline:
            for frame in decoder.feed(link.read(4096)):
                if len(frame) < 6 or frame[5] != 0x1C:
                    continue
                if frame[2] == 3:
                    size = int.from_bytes(frame[:2], "little")
                elif frame[2] in {1, 2} and frame[0] == 6:
                    size = frame[1]
                else:
                    continue
                if len(frame) != 6 + size:
                    raise RuntimeError("invalid Nordic sniffer version reply length")
                version = frame[6:].decode("ascii").rstrip("\x00")
                if not version.strip():
                    raise RuntimeError("empty Nordic sniffer firmware version")
                return version
    raise TimeoutError(
        f"sniffer on {endpoint} did not answer a version request within {timeout_s:g}s"
    )


def _positive_timeout(value: float, name: str) -> None:
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and > 0")


def _signal_group(process: Popen[bytes], number: int) -> None:
    try:
        os.killpg(process.pid, number)
    except ProcessLookupError:
        pass  # The owned group has already exited.


def _stop_process(process: Popen[bytes], timeout_s: float) -> bool:
    """Stop the whole owned group and reap its leader; report forced leader kill."""
    deadline = monotonic() + timeout_s
    forced = False
    if process.poll() is None:
        _signal_group(process, signal.SIGINT)
        try:
            process.wait(timeout=timeout_s / 2)
        except TimeoutExpired:
            _signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=max(0.001, deadline - monotonic()))
            except TimeoutExpired:
                forced = True
                _signal_group(process, signal.SIGKILL)
                process.wait(timeout=1.0)
    # A wrapper can exit before its child. Never leave the capture child running.
    _signal_group(process, signal.SIGKILL)
    return forced


def _diagnostic(stream: BinaryIO) -> str:
    stream.seek(0, os.SEEK_END)
    end = stream.tell()
    stream.seek(max(0, end - 8192))
    return stream.read().decode(errors="replace").strip()


def _header(path: Path) -> tuple[str, int] | None:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) < 24:
        return None
    endian = {
        b"\xd4\xc3\xb2\xa1": "<",
        b"\x4d\x3c\xb2\xa1": "<",
        b"\xa1\xb2\xc3\xd4": ">",
        b"\xa1\xb2\x3c\x4d": ">",
    }.get(header[:4])
    if endian is None:
        raise RuntimeError("extcap output is not a PCAP stream")
    major, minor, _, _, snaplen, _ = struct.unpack(endian + "HHIIII", header[4:])
    if (major, minor) != (2, 4) or snaplen == 0:
        raise RuntimeError("extcap output has an invalid PCAP header")
    return endian, snaplen


def _packet_count(path: Path) -> int:
    header = _header(path)
    if header is None:
        raise RuntimeError("extcap output has an incomplete PCAP header")
    endian, snaplen = header
    count = 0
    size = path.stat().st_size
    with path.open("rb") as stream:
        stream.seek(24)
        while data := stream.read(16):
            if len(data) != 16:
                raise RuntimeError("extcap output ends in a partial PCAP record")
            _, _, included, original = struct.unpack(endian + "IIII", data)
            if included > snaplen or included > original or stream.tell() + included > size:
                raise RuntimeError("extcap output has a truncated or invalid PCAP packet")
            stream.seek(included, os.SEEK_CUR)
            count += 1
    return count


def _has_packet(path: Path) -> bool:
    header = _header(path)
    if header is None:
        return False
    endian, snaplen = header
    with path.open("rb") as stream:
        stream.seek(24)
        data = stream.read(16)
        if len(data) < 16:
            return False
        _, _, included, original = struct.unpack(endian + "IIII", data)
        if included > snaplen or included > original:
            raise RuntimeError("extcap output has an invalid PCAP packet")
        return path.stat().st_size >= 40 + included


class _CaptureSession:
    def __init__(
        self, name: str, path: Path, command: tuple[str, ...], startup_s: float, stop_s: float
    ) -> None:
        self._name = name
        self._path = path
        self._command = command
        self._startup_s = startup_s
        self._stop_s = stop_s
        self._process: Popen[bytes] | None = None
        self._thread: Thread | None = None
        self._directory: TemporaryDirectory[str] | None = None
        self._stderr: BinaryIO | None = None
        self._control_in: int | None = None
        self._control_buffer = bytearray()
        self._stop = Event()
        self._packets_ready = Event()
        self._lock = RLock()
        self._error: BaseException | None = None
        self._result: CaptureResult | None = None
        self._finished = False
        self._started_ns = monotonic_ns()

    @property
    def result(self) -> CaptureResult | None:
        return self._result

    @property
    def failed(self) -> bool:
        return self._error is not None or (
            not self._finished and self._process is not None and self._process.poll() is not None
        )

    def start(self) -> None:
        output: BinaryIO | None = None
        descriptor: int | None = None
        control_out_descriptor: int | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            output = self._path.open("xb", buffering=0)
            self._directory = TemporaryDirectory(prefix="autospan-nrf-")
            fifo = Path(self._directory.name) / "capture.fifo"
            control_in = Path(self._directory.name) / "control-in.fifo"
            control_out = Path(self._directory.name) / "control-out.fifo"
            os.mkfifo(fifo, 0o600)
            os.mkfifo(control_in, 0o600)
            os.mkfifo(control_out, 0o600)
            descriptor = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            # Keep the writer open for the whole capture, even with no GUI commands.
            self._control_in = os.open(control_in, os.O_RDWR | os.O_NONBLOCK)
            control_out_descriptor = os.open(control_out, os.O_RDONLY | os.O_NONBLOCK)
            self._thread = Thread(
                target=self._copy,
                args=(descriptor, control_out_descriptor, output),
                name=f"nrf-capture-{self._name}",
            )
            self._thread.start()
            descriptor = None  # The copy worker now owns this descriptor.
            control_out_descriptor = None
            self._stderr = TemporaryFile("w+b")
            self._process = Popen(
                [
                    *self._command,
                    "--capture",
                    "--fifo",
                    str(fifo),
                    "--extcap-control-in",
                    str(control_in),
                    "--extcap-control-out",
                    str(control_out),
                ],
                stdin=DEVNULL,
                stdout=DEVNULL,
                stderr=self._stderr,
                start_new_session=True,
            )
            # Wireshark's Initialized toolbar message (T, length=2, command=0).
            os.write(self._control_in, b"T\x00\x00\x02\x00\x00")
            deadline = monotonic() + self._startup_s
            while monotonic() < deadline:
                if self.failed:
                    raise RuntimeError("extcap failed before capture readiness") from self._error
                if _header(self._path) is not None:
                    return
                self._stop.wait(min(0.01, max(0, deadline - monotonic())))
            raise TimeoutError(f"extcap produced no valid PCAP header within {self._startup_s:g}s")
        except BaseException as exc:
            self._error = exc
            detail = ""
            try:
                self._cleanup()
            except BaseException as cleanup:
                raise BaseExceptionGroup(
                    "capture startup and cleanup failed", [exc, cleanup]
                ) from None
            finally:
                if output is not None and (self._thread is None or not self._thread.is_alive()):
                    output.close()
                if descriptor is not None:
                    os.close(descriptor)
                if control_out_descriptor is not None:
                    os.close(control_out_descriptor)
                if self._stderr is not None:
                    detail = _diagnostic(self._stderr)
                self._close_stderr()
            if detail and isinstance(exc, Exception):
                raise RuntimeError(f"observer {self._name!r}: {exc}; {detail}") from exc
            raise

    def _copy(self, descriptor: int, control_out: int, output: BinaryIO) -> None:
        try:
            while True:
                self._read_control(control_out)
                try:
                    data = os.read(descriptor, 65536)
                except BlockingIOError:
                    data = b""
                if data:
                    output.write(data)
                    if not self._packets_ready.is_set() and _has_packet(self._path):
                        self._packets_ready.set()
                elif self._stop.is_set():
                    break
                else:
                    self._stop.wait(0.01)
        except BaseException as exc:
            self._error = exc
        finally:
            os.close(descriptor)
            os.close(control_out)
            output.close()

    def _read_control(self, descriptor: int) -> None:
        """Drain toolbar updates so they cannot block RF; surface vendor errors."""
        try:
            data = os.read(descriptor, 65536)
        except BlockingIOError:
            return
        self._control_buffer.extend(data)
        while len(self._control_buffer) >= 4:
            length = int.from_bytes(self._control_buffer[1:4], "big")
            if length > 65537:
                raise RuntimeError("invalid extcap control message length")
            if len(self._control_buffer) < 4 + length:
                return
            message = bytes(self._control_buffer[: 4 + length])
            del self._control_buffer[: 4 + length]
            if message[:1] == b"T" and length >= 2 and message[5] == 9:
                raise RuntimeError(f"extcap control error: {message[6:].decode(errors='replace')}")

    def _cleanup(self) -> bool:
        forced = False
        errors: list[BaseException] = []
        try:
            if self._process is not None:
                forced = _stop_process(self._process, self._stop_s)
        except BaseException as exc:
            errors.append(exc)
        self._stop.set()
        try:
            if self._thread is not None and self._thread.ident is not None:
                self._thread.join(timeout=self._stop_s)
                if self._thread.is_alive():
                    raise RuntimeError(f"capture worker for {self._name!r} did not stop")
        except BaseException as exc:
            errors.append(exc)
        try:
            if self._control_in is not None:
                os.close(self._control_in)
                self._control_in = None
        except BaseException as exc:
            errors.append(exc)
        # Retain the directory and owner while a process/worker may still use it.
        if not errors:
            try:
                if self._directory is not None:
                    self._directory.cleanup()
            except BaseException as exc:
                errors.append(exc)
        self._finished = not errors
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("capture cleanup failed", errors)
        return forced

    def _close_stderr(self) -> None:
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None

    def wait_for_packets(self, timeout_s: float | None = None) -> None:
        timeout_s = self._startup_s if timeout_s is None else timeout_s
        _positive_timeout(timeout_s, "packet readiness timeout")
        deadline = monotonic() + timeout_s
        while True:
            if self.failed:
                raise RuntimeError(f"capture for {self._name!r} failed") from self._error
            if self._packets_ready.is_set():
                return
            if self._finished:
                raise RuntimeError(f"capture for {self._name!r} stopped without packets")
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError(f"extcap produced no complete packet within {timeout_s:g}s")
            self._packets_ready.wait(min(0.01, remaining))

    def stop(self) -> CaptureResult:
        with self._lock:
            if self._result is not None:
                return self._result
            if self._finished:
                if self._error is not None:
                    raise self._error
                raise RuntimeError(f"capture for {self._name!r} failed") from self._error
            unexpected_exit = self._process is not None and self._process.poll() is not None
            try:
                forced = self._cleanup()
                assert self._process is not None
                code = self._process.returncode
                detail = _diagnostic(self._stderr) if self._stderr is not None else ""
                if self._error is not None:
                    raise RuntimeError(f"capture writer for {self._name!r} failed") from self._error
                if unexpected_exit or forced or code not in {0, -2, -15, 130, 143}:
                    raise RuntimeError(
                        f"extcap stopped unexpectedly (exit={code}, forced_kill={forced})"
                        + (f": {detail}" if detail else "")
                    )
                count = _packet_count(self._path)
                self._result = CaptureResult(
                    self._path, self._path.stat().st_size, self._started_ns, monotonic_ns(), count
                )
                return self._result
            except BaseException as exc:
                self._error = exc
                raise
            finally:
                self._close_stderr()


class _ObserverCaptureSession:
    """Public capture handle whose stop path always updates its owning observer."""

    def __init__(self, owner: "NrfSnifferObserver", session: _CaptureSession) -> None:
        self._owner = owner
        self._session = session

    @property
    def result(self) -> CaptureResult | None:
        return self._session.result

    def wait_for_packets(self, timeout_s: float | None = None) -> None:
        self._session.wait_for_packets(timeout_s)

    def stop(self) -> CaptureResult:
        if self._session.result is not None:
            return self._session.result
        return self._owner._stop_session(self._session)

    def __enter__(self) -> "_ObserverCaptureSession":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.stop()
        except BaseException as cleanup:
            if exc is not None and cleanup is not exc:
                raise BaseExceptionGroup(
                    "capture body and cleanup failed", [exc, cleanup]
                ) from None
            raise


class NrfSnifferObserver:
    """Passive BLE observer using a configured Nordic extcap executable on POSIX."""

    def __init__(
        self,
        name: str,
        selector: SerialDeviceSelector,
        resolver: SerialResolver,
        *,
        extcap_command: tuple[str, ...] = ("nrf_sniffer_ble.sh",),
        extcap_version: str | None = None,
        startup_timeout_s: float = 5.0,
        stop_timeout_s: float = 3.0,
    ) -> None:
        if (
            not name.strip()
            or not extcap_command
            or any(not word.strip() for word in extcap_command)
        ):
            raise ValueError("observer name and extcap command must not be empty")
        _positive_timeout(startup_timeout_s, "startup timeout")
        _positive_timeout(stop_timeout_s, "stop timeout")
        self._name = name
        self._selector = selector
        self._resolver = resolver
        self._command = extcap_command
        self._version = None if extcap_version == "None" else extcap_version
        self._startup_s = startup_timeout_s
        self._stop_s = stop_timeout_s
        self._state = ObserverState.CLOSED
        self._endpoint: str | None = None
        self._interface: str | None = None
        self._firmware_version: str | None = None
        self._session: _CaptureSession | None = None
        self._lock = RLock()

    @property
    def state(self) -> ObserverState:
        with self._lock:
            if self._session is not None:
                if self._session.failed:
                    return ObserverState.FAILED
                if self._session.result is not None:
                    return ObserverState.READY
            return self._state

    @property
    def endpoint(self) -> str | None:
        return self._endpoint

    @property
    def firmware_version(self) -> str | None:
        """Version returned by a sniffer response, never inferred from USB metadata."""
        return self._firmware_version

    def resolve(self) -> PortInfo:
        return self._resolver.resolve(self._selector)

    def open(self) -> None:
        with self._lock:
            if self.state in {ObserverState.READY, ObserverState.CAPTURING}:
                return
            if self.state is ObserverState.FAILED:
                raise RuntimeError(f"observer {self._name!r} failed; close it before reopening")
            if os.name != "posix":
                raise NotImplementedError("the FIFO capture backend currently requires POSIX")
            executable = self._command[0]
            if which(executable) is None:
                raise FileNotFoundError(f"nRF Sniffer extcap executable not found: {executable}")
            port = self.resolve()
            deadline = monotonic() + self._startup_s
            with TemporaryFile("w+b") as stdout, TemporaryFile("w+b") as stderr:
                process = Popen(
                    [*self._command, "--extcap-interfaces"],
                    stdin=DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
                try:
                    process.wait(timeout=self._startup_s)
                except TimeoutExpired as exc:
                    _stop_process(process, self._stop_s)
                    raise TimeoutError("Nordic extcap discovery timed out") from exc
                except BaseException:
                    _stop_process(process, self._stop_s)
                    raise
                _stop_process(process, self._stop_s)
                if process.returncode:
                    raise RuntimeError(f"Nordic extcap discovery failed: {_diagnostic(stderr)}")
                stdout.seek(0)
                interfaces = []
                for line in stdout.read(1024 * 1024).decode(errors="replace").splitlines():
                    if not line.startswith("interface "):
                        continue
                    fields = dict(re.findall(r"\{([\w-]+)=([^{}]*)\}", line))
                    value = fields.get("value", "")
                    if value == port.device or value.startswith(port.device + "-"):
                        if value.endswith("-None"):
                            continue
                        if (
                            self._version is None
                            or value == port.device
                            or value == f"{port.device}-{self._version}"
                        ):
                            interfaces.append(value)
                if len(interfaces) != 1:
                    raise RuntimeError(
                        f"observer {self._name!r}: resolved {port.device}, but Nordic extcap "
                        "did not report one responding sniffer; verify sniffer firmware and tooling"
                        + (f"; {_diagnostic(stderr)}" if _diagnostic(stderr) else "")
                    )
            interface = interfaces[0]
            if interface == port.device:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("Nordic extcap discovery left no time for the version probe")
                version = _query_firmware_version(port.device, remaining)
            else:
                version = interface[len(port.device) + 1 :]
            if self._version is not None and version != self._version:
                raise RuntimeError(
                    f"sniffer firmware version {version!r} != expected {self._version!r}"
                )
            self._endpoint = port.device
            self._interface = interface
            self._firmware_version = version
            self._state = ObserverState.READY

    def start_capture(self, path: Path) -> CaptureSession:
        with self._lock:
            if self.state is not ObserverState.READY:
                raise RuntimeError(f"observer {self._name!r} is not ready: {self.state.value}")
            assert self._interface is not None
            session = _CaptureSession(
                self._name,
                Path(path),
                (*self._command, "--extcap-interface", self._interface),
                self._startup_s,
                self._stop_s,
            )
            self._session = session
            try:
                session.start()
            except BaseException:
                if session._finished:
                    self._session = None
                self._state = ObserverState.FAILED
                raise
            self._state = ObserverState.CAPTURING
            return _ObserverCaptureSession(self, session)

    def _stop_session(self, session: _CaptureSession) -> CaptureResult:
        with self._lock:
            if self._session is not session:
                if session.result is not None or session.failed:
                    # A retired handle can report its terminal outcome again,
                    # without changing a reopened observer or a later capture.
                    return session.stop()
                raise RuntimeError(f"observer {self._name!r} does not own this capture session")
            try:
                return session.stop()
            finally:
                self._state = ObserverState.FAILED if session.failed else ObserverState.READY
                if session._finished:
                    self._session = None

    def stop_capture(self) -> CaptureResult:
        with self._lock:
            if self._session is None:
                raise RuntimeError(f"observer {self._name!r} has no capture session")
            session = self._session
        return self._stop_session(session)

    def close(self) -> None:
        with self._lock:
            try:
                if self._session is not None:
                    self.stop_capture()
            finally:
                self._endpoint = None
                self._interface = None
                self._firmware_version = None
                self._state = (
                    ObserverState.FAILED if self._session is not None else ObserverState.CLOSED
                )
