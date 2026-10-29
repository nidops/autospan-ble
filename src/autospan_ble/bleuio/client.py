"""Serialized BleuIO AT command broker with unsolicited-event routing."""

from __future__ import annotations

from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import BoundedSemaphore, Event, Lock, Thread
from time import monotonic, monotonic_ns
from math import isfinite
from typing import Any, Callable, Mapping

from autospan_ble.bleuio.errors import (
    BleuIOError,
    CommandRejected,
    CommandTimeout,
    DeviceDisconnected,
)
from autospan_ble.bleuio.parser import parse_line
from autospan_ble.bleuio.transport import LineTransport
from autospan_ble.bleuio.types import BleEvent, CommandResult, ParsedRecord, _next_event_order

EvidenceCallback = Callable[[str, Mapping[str, Any]], None]

# Shared by the queue and records temporarily retained by event consumers.
_MAX_EVENTS = 4096


@dataclass
class _Pending:
    command: str
    done: Event = field(default_factory=Event)
    index: int | None = None
    acknowledgement: Mapping[str, Any] | None = None
    replies: list[Mapping[str, Any]] = field(default_factory=list)
    error: BaseException | None = None


class BleuIOClient:
    """Own a transport and RX worker, serializing correlated AT command frames.

    Command replies match the vendor index; other records enter a separate event
    queue. Evidence callbacks run in caller/reader contexts and may fail operations.
    """

    def __init__(
        self, transport: LineTransport, *, evidence: EvidenceCallback | None = None
    ) -> None:
        self._transport = transport
        self._evidence = evidence
        self._command_lock = Lock()
        self._state_lock = Lock()
        self._pending: _Pending | None = None
        self._events: Queue[BleEvent] = Queue(maxsize=_MAX_EVENTS)
        self._event_slots = BoundedSemaphore(_MAX_EVENTS)
        self._stop = Event()
        self._reader: Thread | None = None
        self._reader_error: BaseException | None = None

    @property
    def endpoint(self) -> str:
        return self._transport.endpoint

    @property
    def failure(self) -> BaseException | None:
        return self._reader_error

    def initialize(self, *, timeout_s: float = 2.0) -> CommandResult:
        """Converge to echo-off + verbose mode, then verify identity with ATI."""
        self._check_available()
        self._bootstrap("AT", timeout_s=timeout_s)
        self._bootstrap("ATE0", timeout_s=timeout_s)
        self._bootstrap("ATV1", timeout_s=timeout_s)
        self.start_reader()
        return self.command("ATI", timeout_s=timeout_s)

    def _bootstrap(self, command: str, *, timeout_s: float) -> None:
        self._emit("tx", command=command, bootstrap=True)
        self._transport.write((command + "\r\n").encode("ascii"))
        deadline = monotonic() + timeout_s
        command_index: int | None = None
        acknowledged = False
        legacy_success = {
            "AT": {"OK"},
            "ATE0": {"OK", "ECHO OFF"},
            "ATV1": {"VERBOSE ON"},
        }
        while monotonic() < deadline:
            line = self._transport.readline(min(0.1, max(0.0, deadline - monotonic())))
            if line is None:
                continue
            self._emit("rx", line=line, bootstrap=True)
            record = parse_line(line)
            if (
                record.kind == "C"
                and record.payload is not None
                and record.payload.get("cmd") == command
                and command_index is None
                and record.index is not None
            ):
                command_index = record.index
            elif (
                record.kind == "A"
                and record.payload is not None
                and command_index is not None
                and record.index == command_index
            ):
                if int(record.payload.get("err", 0)) != 0:
                    raise CommandRejected(
                        str(record.payload.get("errMsg", "bootstrap command rejected"))
                    )
                acknowledged = True
            elif record.kind == "E" and acknowledged and record.index == command_index:
                return
            elif command_index is None and line.strip().upper() in legacy_success.get(
                command, set()
            ):
                return
        raise CommandTimeout(f"bootstrap command {command!r} did not complete")

    def start_reader(self) -> None:
        """Start the RX worker; failed/closed sessions require a fresh client."""
        self._check_available()
        if self._reader is not None and self._reader.is_alive():
            return
        self._reader = Thread(
            target=self._reader_main, name=f"bleuio-rx:{self.endpoint}", daemon=True
        )
        self._reader.start()

    def close(self) -> None:
        """Fail pending commands, stop/join the reader and close the owned transport.

        Raise ``BleuIOError`` if the worker cannot stop within the bounded joins.
        """
        self._stop.set()
        with self._state_lock:
            pending = self._pending
            if pending is not None and not pending.done.is_set():
                pending.error = DeviceDisconnected("BleuIO session closed")
                pending.done.set()
            self._pending = None
        reader = self._reader
        errors: list[BaseException] = []
        try:
            if reader is not None:
                reader.join(timeout=0.5)
        except BaseException as exc:
            errors.append(exc)
        try:
            self._transport.close()
        except BaseException as exc:
            errors.append(exc)
        if reader is not None and reader.is_alive():
            try:
                reader.join(timeout=0.5)
            except BaseException as exc:
                errors.append(exc)
            if reader.is_alive():
                errors.append(BleuIOError(f"RX worker did not stop for {self.endpoint}"))
        if reader is None or not reader.is_alive():
            self._reader = None
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup(f"{self.endpoint}: client cleanup failed", errors)

    def command(self, command: str, *, timeout_s: float = 3.0) -> CommandResult:
        """Wait for the command lane and a complete indexed command frame.

        The finite positive timeout includes lane acquisition. Invalid input raises
        ``ValueError``; vendor rejection raises ``CommandRejected``. A timeout or
        interruption during transmission/waiting invalidates the session so late
        replies cannot match subsequent commands. Lane contention alone leaves it usable.
        Reader/transport failure raises ``DeviceDisconnected``.
        """
        if "\r" in command or "\n" in command:
            raise ValueError("command must be a single AT line")
        if not isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("command timeout must be finite and > 0")
        deadline = monotonic() + timeout_s
        if not self._command_lock.acquire(timeout=timeout_s):
            raise CommandTimeout(f"command lane busy for {self.endpoint}: {command}")
        try:
            if self._stop.is_set():
                raise DeviceDisconnected(f"BleuIO session closed: {self.endpoint}")
            if self._reader_error is not None:
                raise DeviceDisconnected(
                    f"BleuIO session failed for {self.endpoint}"
                ) from self._reader_error
            pending = _Pending(command=command)
            with self._state_lock:
                if self._stop.is_set() or self._reader_error is not None:
                    raise DeviceDisconnected(
                        f"BleuIO session unavailable: {self.endpoint}"
                    ) from self._reader_error
                if self._pending is not None:
                    raise RuntimeError("internal error: overlapping BleuIO command")
                self._pending = pending
            try:
                self._emit("tx", command=command)
                data = (command + "\r\n").encode("ascii")
                try:
                    self._transport.write(data)
                    completed = pending.done.wait(max(0.0, deadline - monotonic()))
                except OSError as exc:
                    self._reader_error = exc
                    raise DeviceDisconnected(
                        f"write failed for {self.endpoint}: {command}"
                    ) from exc
                except BaseException as exc:
                    self._reader_error = exc
                    raise
                if not completed:
                    error = CommandTimeout(
                        f"BleuIO command timed out after {timeout_s:.3f}s: {command}"
                    )
                    self._reader_error = error
                    raise error
                if pending.error is not None:
                    raise pending.error
                ack = pending.acknowledgement
                if ack is None or pending.index is None:
                    raise BleuIOError(f"incomplete command frame for {self.endpoint}: {command}")
                if ack is not None and int(ack.get("err", 0)) != 0:
                    raise CommandRejected(
                        f"{command}: {ack.get('errMsg', 'rejected')} (err={ack.get('err')})",
                        error_code=int(ack["err"]),
                    )
                self._emit(
                    "command_completed",
                    command=command,
                    command_index=pending.index,
                    replies=tuple(pending.replies),
                )
                return CommandResult(command, pending.index, ack, tuple(pending.replies))
            finally:
                with self._state_lock:
                    if self._pending is pending:
                        self._pending = None
        finally:
            self._command_lock.release()

    def next_event(self, *, timeout_s: float) -> BleEvent:
        """Consume the next queued record or raise ``CommandTimeout`` at the deadline.

        Closed/failed sessions raise ``DeviceDisconnected`` before queue draining.
        Event waits alone do not invalidate an otherwise healthy command session.
        """
        event = self._next_event(timeout_s=timeout_s)
        self._release_event()
        return event

    def _check_available(self) -> None:
        failure = self._reader_error
        if failure is not None:
            raise DeviceDisconnected(f"RX worker failed for {self.endpoint}") from failure
        if self._stop.is_set():
            raise DeviceDisconnected(f"BleuIO session closed: {self.endpoint}")

    def _next_event(self, *, timeout_s: float) -> BleEvent:
        # Internal consumers retain the slot until consumption or restoration.
        deadline = monotonic() + timeout_s
        while monotonic() < deadline:
            self._check_available()
            try:
                event = self._events.get(timeout=min(0.1, max(0.0, deadline - monotonic())))
            except Empty:
                continue
            try:
                self._check_available()
            except DeviceDisconnected:
                self._restore_events([event])
                raise
            return event
        self._check_available()
        raise CommandTimeout(f"no BleuIO event within {timeout_s:.3f}s")

    def stop_scan(self, command_index: int, *, timeout_s: float = 3.0) -> None:
        """Send the vendor scan interrupt and require the indexed completion event."""
        if not isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("scan stop timeout must be finite and > 0")
        deadline = monotonic() + timeout_s
        if not self._command_lock.acquire(timeout=timeout_s):
            raise CommandTimeout(f"command lane busy while stopping scan for {self.endpoint}")
        errors: list[BaseException] = []
        try:
            if self._reader_error is not None or self._stop.is_set():
                raise DeviceDisconnected(f"BleuIO session unavailable: {self.endpoint}")
            try:
                self._emit("tx", command="CTRL+C", scan_command_index=command_index)
            except BaseException as exc:
                # Recording failures must not prevent release of active radio work.
                errors.append(exc)
            try:
                self._transport.write(b"\x03")
                self.wait_event(
                    lambda event: event.kind == "SE" and event.command_index == command_index,
                    timeout_s=max(0.001, deadline - monotonic()),
                )
            except OSError as exc:
                self._reader_error = exc
                failure = DeviceDisconnected(f"scan interrupt failed for {self.endpoint}")
                failure.__cause__ = exc
                errors.append(failure)
            except BaseException as exc:
                # Unconfirmed radio work cannot safely share the next command lane.
                self._reader_error = exc
                errors.append(exc)
        finally:
            self._command_lock.release()
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("scan interrupt and evidence recording failed", errors)

    def requeue_events(self, events: list[BleEvent]) -> None:
        """Append deferred events to the queue in their supplied order."""
        try:
            for event in events:
                self._queue_event(event)
        except BleuIOError as exc:
            self._fail(exc)
            raise DeviceDisconnected(str(exc)) from self._reader_error

    def _queue_event(self, event: BleEvent) -> None:
        if not self._event_slots.acquire(blocking=False):
            raise BleuIOError(f"unsolicited event buffer overflow for {self.endpoint}")
        self._events.put_nowait(event)

    def _release_event(self) -> None:
        self._event_slots.release()

    def _restore_events(self, events: list[BleEvent]) -> None:
        # These records already own slots, including while held outside the queue.
        for event in events:
            self._events.put_nowait(event)

    def _fail(self, failure: BaseException) -> None:
        self._reader_error = failure
        try:
            self._emit("rx_failed", error=repr(failure))
        except BaseException as recording:
            failure = BaseExceptionGroup("RX and evidence recording failed", [failure, recording])
        with self._state_lock:
            self._reader_error = failure
            pending = self._pending
            if pending is not None:
                pending.error = DeviceDisconnected(f"BleuIO session failed for {self.endpoint}")
                pending.error.__cause__ = failure
                pending.done.set()

    def wait_event(self, predicate: Callable[[BleEvent], bool], *, timeout_s: float) -> BleEvent:
        """Consume the first matching event within the timeout, retaining other records.

        Unmatched events return to the queue's tail in encounter order, including
        on timeout or predicate failure. Raise ``CommandTimeout`` if no match
        arrives, or ``DeviceDisconnected`` if the session becomes unavailable.
        """
        deadline = monotonic() + timeout_s
        deferred: list[BleEvent] = []
        try:
            while monotonic() < deadline:
                event = self._next_event(timeout_s=max(0.001, deadline - monotonic()))
                deferred.append(event)
                if predicate(event):
                    deferred.pop()
                    self._release_event()
                    return event
        finally:
            self._restore_events(deferred)
        self._check_available()
        raise CommandTimeout(f"matching BleuIO event not received within {timeout_s:.3f}s")

    def _reader_main(self) -> None:
        try:
            while not self._stop.is_set() and self._reader_error is None:
                line = self._transport.readline(0.1)
                if line is None:
                    continue
                received_order = _next_event_order()
                received_ns = monotonic_ns()
                self._emit("rx", line=line)
                self._route(
                    parse_line(line),
                    received_monotonic_ns=received_ns,
                    received_order=received_order,
                )
        except BaseException as exc:
            if self._stop.is_set():
                return
            self._fail(exc)

    def _route(
        self,
        record: ParsedRecord,
        *,
        received_monotonic_ns: int | None = None,
        received_order: int | None = None,
    ) -> None:
        order = _next_event_order() if received_order is None else received_order
        received_ns = monotonic_ns() if received_monotonic_ns is None else received_monotonic_ns
        payload = record.payload
        if record.kind in {"C", "A", "R", "E"} and payload is not None:
            with self._state_lock:
                pending = self._pending
                if pending is not None:
                    if record.kind == "C":
                        if payload.get("cmd") == pending.command and pending.index is None:
                            pending.index = record.index
                            return
                    elif pending.index is not None and record.index == pending.index:
                        if record.kind == "A":
                            pending.acknowledgement = payload
                        elif record.kind == "R":
                            pending.replies.append(payload)
                        elif record.kind == "E":
                            pending.done.set()
                        return
            self._queue_event(
                BleEvent(
                    record.kind,
                    payload,
                    record.index,
                    received_monotonic_ns=received_ns,
                    _received_order=order,
                )
            )
            return
        if record.kind in {"S", "SF", "ST", "SE", "event", "json"} and payload is not None:
            self._queue_event(
                BleEvent(
                    record.kind,
                    payload,
                    record.index,
                    received_monotonic_ns=received_ns,
                    _received_order=order,
                )
            )
            return
        if record.kind == "text" and record.raw:
            self._queue_event(
                BleEvent(
                    "text",
                    record.raw,
                    None,
                    received_monotonic_ns=received_ns,
                    _received_order=order,
                )
            )

    def _emit(self, kind: str, **fields: Any) -> None:
        if self._evidence is not None:
            self._evidence(kind, fields)
