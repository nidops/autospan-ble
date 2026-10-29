"""Supervised logical BleuIO device."""

from __future__ import annotations

from contextlib import contextmanager
from time import monotonic, sleep
from math import isfinite
from threading import RLock
from typing import Any, Callable, Iterator, Mapping, TypeVar

from autospan_ble.bleuio.address import (
    BleAddress,
    BleAddressType,
    BleuIOAddressMode,
    coerce_address_type,
)
from autospan_ble.bleuio.client import BleuIOClient, EvidenceCallback
from autospan_ble.bleuio.codecs import BleCodec, codec as builtin_codec
from autospan_ble.bleuio.gatt import BleGatt
from autospan_ble.bleuio.errors import (
    BleuIOError,
    CommandTimeout,
    CommandRejected,
    DeviceDisconnected,
    DeviceNotFound,
    PortInventoryChanged,
)
from autospan_ble.bleuio.selector import SerialDeviceSelector, SerialResolver
from autospan_ble.bleuio.transport import LineTransport
from autospan_ble.bleuio.payloads import (
    AdvertisingData,
    AdvertisingReport,
    AdvertisingReports,
    PayloadDecoder,
)
from autospan_ble.bleuio.types import (
    _next_event_order,
    BleEvent,
    CommandResult,
    DeviceState,
    GattValue,
    PortInfo,
    ScanResult,
    ScanResults,
)

TransportBuilder = Callable[[str], LineTransport]
_ScanRow = TypeVar("_ScanRow")


class BleuIODevice:
    """A logical BLE endpoint whose identity survives serial re-enumeration."""

    def __init__(
        self,
        name: str,
        selector: SerialDeviceSelector,
        resolver: SerialResolver,
        transport_builder: TransportBuilder,
        *,
        evidence: EvidenceCallback | None = None,
        reset_timeout_s: float = 20.0,
    ) -> None:
        self.name = name
        self.selector = selector
        self._resolver = resolver
        self._transport_builder = transport_builder
        self._evidence = evidence
        if not isfinite(reset_timeout_s) or reset_timeout_s <= 0:
            raise ValueError("reset timeout must be finite and > 0")
        self._reset_timeout_s = reset_timeout_s
        self._operation_lock = RLock()
        self._transport: LineTransport | None = None
        self._client: BleuIOClient | None = None
        self._state = DeviceState.CLOSED
        self._identity: CommandResult | None = None
        self._owns_advertising = False
        self._owns_connection = False
        self._gatt_read_uncertain = False

    @property
    def state(self) -> DeviceState:
        if (
            self._state == DeviceState.READY
            and self._client is not None
            and self._client.failure is not None
        ):
            return DeviceState.FAILED
        return self._state

    @property
    def endpoint(self) -> str | None:
        return self._transport.endpoint if self._transport is not None else None

    @property
    def identity(self) -> CommandResult | None:
        return self._identity

    def resolve(self) -> PortInfo:
        """Resolve current physical identity without opening hardware."""
        return self._resolver.resolve(self.selector)

    def open(self) -> None:
        """Resolve USB identity and initialize the transport, RX worker and ATI reply.

        A READY endpoint is reused. Resolution, transport or initialization errors
        propagate after closing the failed session; state remains FAILED.
        """
        with self._operation_lock:
            if self.state == DeviceState.READY:
                return
            self.close(failed=True)
            try:
                self._transition(DeviceState.OPENING)
                port = self._resolver.resolve(self.selector)
                self._open_endpoint(port.device)
            except BaseException as primary:
                self._state = DeviceState.FAILED
                errors = [primary]
                try:
                    self.close(failed=True)
                except BaseException as cleanup:
                    errors.append(cleanup)
                try:
                    self._transition(DeviceState.FAILED)
                except BaseException as recording:
                    errors.append(recording)
                if len(errors) > 1:
                    raise BaseExceptionGroup(
                        f"{self.name}: opening and cleanup failed", errors
                    ) from None
                raise

    def _open_endpoint(self, endpoint: str, *, timeout_s: float = 2.0) -> None:
        transport = self._transport_builder(endpoint)
        client = BleuIOClient(transport, evidence=self._client_evidence)
        self._transport = transport
        self._client = client
        self._transition(DeviceState.INITIALIZING, endpoint=endpoint)
        # The caller owns rollback, including failures in lifecycle evidence.
        transport.open()
        self._identity = client.initialize(timeout_s=timeout_s)
        if not self._identity.replies:
            raise BleuIOError(f"{self.name}: ATI returned no identity")
        self._transition(DeviceState.READY, endpoint=endpoint)

    def close(self, *, failed: bool = False) -> None:
        """Release owned connection/advertising work and close the serial session.

        ``failed=True`` skips radio commands and preserves failure state; closing
        a transport alone does not guarantee that firmware radio work stopped.
        All cleanup is attempted and failures form a ``BaseExceptionGroup``.
        """
        with self._operation_lock:
            errors: list[BaseException] = []
            if not failed and self.state == DeviceState.READY and self._owns_connection:
                try:
                    self.disconnect()
                except BaseException as exc:
                    errors.append(exc)
            if not failed and self.state == DeviceState.READY and self._owns_advertising:
                try:
                    self.stop_advertising()
                except BaseException as exc:
                    errors.append(exc)
            client = self._client
            self._identity = None
            self._owns_advertising = False
            self._owns_connection = False
            self._gatt_read_uncertain = False
            if client is not None:
                try:
                    client.close()
                except BaseException as exc:
                    errors.append(exc)
                else:
                    self._client = None
                    self._transport = None
            if errors:
                self._state = DeviceState.FAILED
            elif failed and self._state != DeviceState.CLOSED:
                self._state = DeviceState.FAILED
            elif not failed:
                self._state = DeviceState.CLOSED
            if not failed:
                try:
                    self._transition(self._state)
                except BaseException as recording:
                    errors.append(recording)
            if errors:
                raise BaseExceptionGroup(f"{self.name}: cleanup failed", errors)

    def raw(self, command: str, *, timeout_s: float = 3.0) -> CommandResult:
        """Send one vendor AT line and return its correlated acknowledgement/replies.

        Requires an open session. Invalid lines/timeouts raise ``ValueError``;
        rejection, timeout and session loss raise BleuIO errors. A sent command
        timeout invalidates the session. Raw operations do not track radio ownership
        or wait for asynchronous completion; prefer scoped helpers for radio work.
        """
        with self._operation_lock:
            return self._require_client().command(command, timeout_s=timeout_s)

    def mac_address(self) -> str:
        """Query the identity MAC; USB identity remains the selector's responsibility."""
        result = self.raw("AT+GETMAC")
        for row in result.replies:
            if "own_mac_addr" in row:
                return str(row["own_mac_addr"]).upper()
        raise BleuIOError(f"{self.name}: GETMAC returned no address")

    def address_mode(self) -> BleuIOAddressMode:
        """Return the current local address mode configured by ``AT+GAPADDRTYPE``."""
        result = self.raw("AT+GAPADDRTYPE")
        for row in result.replies:
            raw = row.get("addrType")
            if raw is None:
                continue
            try:
                return BleuIOAddressMode(str(raw))
            except ValueError as exc:
                raise BleuIOError(f"{self.name}: unknown GAP address type {raw!r}") from exc
        raise BleuIOError(f"{self.name}: GAPADDRTYPE returned no address mode")

    def set_address_mode(self, mode: BleuIOAddressMode | int | str) -> BleuIOAddressMode:
        """Set one BleuIO local-address mode and return the converged mode."""
        if isinstance(mode, BleuIOAddressMode):
            selected = mode
        elif isinstance(mode, int) and not isinstance(mode, bool):
            try:
                selected = next(item for item in BleuIOAddressMode if item.code == mode)
            except StopIteration as exc:
                raise ValueError("address mode code must be between 1 and 5") from exc
        else:
            try:
                selected = BleuIOAddressMode(str(mode))
            except ValueError as exc:
                raise ValueError(f"unsupported BleuIO address mode: {mode!r}") from exc
        with self._operation_lock:
            if self.address_mode() is not selected:
                self.raw(f"AT+GAPADDRTYPE={selected.code}")
            converged = self.address_mode()
            if converged is not selected:
                raise BleuIOError(
                    f"{self.name}: address mode did not converge to {selected.value}; got {converged.value}"
                )
            self._emit("address_mode", mode=converged.value, code=converged.code)
            return converged

    def wait_event(
        self, predicate: Callable[[BleEvent], bool], *, timeout_s: float = 5.0
    ) -> BleEvent:
        """Wait for a vendor event without discarding unrelated asynchronous records."""
        if not isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("event timeout must be finite and > 0")
        with self._operation_lock:
            return self._require_client().wait_event(predicate, timeout_s=timeout_s)

    def set_role(self, role: str) -> None:
        with self._operation_lock:
            normalized = role.strip().lower()
            commands = {"central": "AT+CENTRAL", "peripheral": "AT+PERIPHERAL"}
            try:
                command = commands[normalized]
            except KeyError as exc:
                raise ValueError(
                    f"unsupported initial role {role!r}; use central or peripheral"
                ) from exc
            status = self.raw("AT+GAPSTATUS")
            if not any(row.get("gap_role") == normalized for row in status.replies):
                self.raw(command)
            self._emit("role", role=normalized)

    def scan(self, seconds: float = 2.0, *, timeout_s: float | None = None) -> ScanResults:
        """Discover peers, retaining each address's public/random type."""
        with self._operation_lock:
            duration = _scan_duration(seconds, timeout_s)
            rows, index = self._scan_records(
                f"AT+GAPSCAN={duration}",
                "S",
                self._scan_result,
                seconds=duration,
                timeout_s=timeout_s,
            )
            self._emit("scan_complete", count=len(rows), command_index=index)
            return ScanResults(rows)

    def _scan_result(self, payload: Mapping[str, Any]) -> ScanResult:
        raw = payload.get("addr")
        if not isinstance(raw, str):
            raise BleuIOError(f"{self.name}: invalid scan address {raw!r}")
        try:
            address = BleAddress.parse(raw)
        except ValueError as exc:
            raise BleuIOError(f"{self.name}: invalid scan address {raw!r}") from exc
        name = payload.get("name")
        if name is not None and not isinstance(name, str):
            raise BleuIOError(f"{self.name}: invalid scan name {name!r}")
        return ScanResult(
            address=address,
            rssi=_record_int(payload, "rssi"),
            index=_record_int(payload, "indx"),
            name=name,
            manufacturer_id=_manufacturer_id(payload.get("mfsid")),
            fields=dict(payload),
        )

    def scan_target(
        self,
        address: BleAddress | ScanResult | str,
        *,
        address_type: int | BleAddressType | None = None,
        seconds: float = 2.0,
        timeout_s: float | None = None,
    ) -> AdvertisingReports:
        """Capture advertising and scan-response data from one typed peer."""
        with self._operation_lock:
            duration = _scan_duration(seconds, timeout_s)
            target = _target_address(address, address_type=address_type)

            def decode(payload: Mapping[str, Any]) -> AdvertisingReport:
                raw = payload.get("addr")
                if not isinstance(raw, str):
                    raise BleuIOError(f"{self.name}: invalid target-scan address {raw!r}")
                try:
                    address = BleAddress.parse(raw, fallback_type=target.address_type)
                except ValueError as exc:
                    raise BleuIOError(f"{self.name}: invalid target-scan address {raw!r}") from exc
                if address != target:
                    raise BleuIOError(
                        f"{self.name}: target-scan record belongs to a different peer"
                    )
                data_raw = payload.get("data")
                if not isinstance(data_raw, str):
                    raise BleuIOError(f"{self.name}: target-scan record has no hex data")
                try:
                    value = bytes.fromhex(data_raw.removeprefix("0x").removeprefix("0X"))
                except ValueError as exc:
                    raise BleuIOError(f"{self.name}: invalid target-scan hex payload") from exc
                data_type = _record_int(payload, "type")
                if data_type is None or not 0 <= data_type <= 0xFF:
                    raise BleuIOError(f"{self.name}: invalid target-scan data type {data_type!r}")
                return AdvertisingReport(
                    address=address,
                    data_type=data_type,
                    payload=value,
                    data=AdvertisingData.parse(value),
                    rssi=_record_int(payload, "rssi"),
                    manufacturer_id=_manufacturer_id(payload.get("mfsid")),
                    fields=dict(payload),
                )

            reports, index = self._scan_records(
                f"AT+SCANTARGET={target.bleuio}={duration}",
                "ST",
                decode,
                seconds=duration,
                timeout_s=timeout_s,
            )
            self._emit(
                "target_scan_complete",
                address=target.bleuio,
                count=len(reports),
                command_index=index,
            )
            return AdvertisingReports(reports)

    def _scan_records(
        self,
        command: str,
        kind: str,
        decode: Callable[[Mapping[str, Any]], _ScanRow],
        *,
        seconds: int,
        timeout_s: float | None,
    ) -> tuple[tuple[_ScanRow, ...], int]:
        self.set_role("central")
        client = self._require_client()
        requested_order = _next_event_order()
        result = self.raw(command, timeout_s=3.0)
        assert result.index is not None
        deadline = monotonic() + (timeout_s if timeout_s is not None else seconds + 3.0)
        rows: list[_ScanRow] = []
        deferred: list[BleEvent] = []
        try:
            while monotonic() < deadline:
                event = client._next_event(timeout_s=max(0.001, deadline - monotonic()))
                if (
                    event.command_index != result.index
                    or event._received_order <= requested_order
                    or event.kind not in {kind, "SE"}
                ):
                    deferred.append(event)
                    continue
                client._release_event()
                if event.kind == "SE":
                    return tuple(rows), result.index
                if not isinstance(event.payload, Mapping):
                    raise BleuIOError(f"{self.name}: invalid {kind} scan record")
                rows.append(decode(event.payload))
            raise CommandTimeout(f"scan completion event not received for {self.name}")
        except BaseException as primary:
            try:
                client.stop_scan(result.index)
            except BaseException as cleanup:
                if cleanup is not primary:
                    raise BaseExceptionGroup(
                        f"{self.name}: scan and radio cleanup failed", [primary, cleanup]
                    ) from None
            raise
        finally:
            client._restore_events(deferred)

    def start_advertising(self, *, adv_data: str | bytes | None = None) -> None:
        """Set peripheral role and start advertising, retaining ownership until stopped.

        Optional bytes are limited to 28 bytes for the Standard firmware's Flags
        prefix; vendor hex strings pass through without binary length validation.
        Invalid data raises ``ValueError``; command errors propagate. Prefer
        ``advertising()`` to guarantee cleanup when the test body raises.
        """
        with self._operation_lock:
            encoded = None
            if adv_data is not None:
                if isinstance(adv_data, bytes):
                    # Qualified Standard firmware prepends a three-byte Flags AD
                    # structure and silently truncates custom data above 28 bytes.
                    if len(adv_data) > 28:
                        raise ValueError(
                            "AT+ADVDATA binary payload must be at most 28 bytes "
                            "(3 bytes reserved for firmware Flags)"
                        )
                    encoded = ":".join(f"{value:02X}" for value in adv_data)
                elif isinstance(adv_data, str):
                    encoded = adv_data
                else:
                    raise ValueError("advertising data must be bytes or a vendor hex string")
                if not encoded.strip() or "\r" in encoded or "\n" in encoded:
                    raise ValueError("advertising data must be non-empty and occupy one AT line")
            self.set_role("peripheral")
            if encoded is not None:
                self.raw(f"AT+ADVDATA={encoded}")
            status = self.raw("AT+GAPSTATUS")
            # The command can succeed before completion evidence raises. Own the
            # attempted lifetime before sending so scope exit still stops it.
            self._owns_advertising = True
            if not any(row.get("advertising") is True for row in status.replies):
                self.raw("AT+ADVSTART")
            self._emit("advertising_started")

    def stop_advertising(self) -> None:
        """Stop owned/active advertising and confirm its asynchronous stop event.

        Ownership clears only on success. Rejection, timeout or session loss
        propagates, so an unsuccessful call does not prove that radio work stopped.
        """
        with self._operation_lock:
            status = self.raw("AT+GAPSTATUS")
            if self._owns_advertising or any(
                row.get("advertising") is True for row in status.replies
            ):
                requested_order = _next_event_order()
                try:
                    # A peripheral can resume advertising just after peer
                    # disconnect. An earlier idle status must not skip our stop.
                    self.raw("AT+ADVSTOP")
                except CommandRejected as exc:
                    if exc.error_code != 5:
                        raise
                    current = self.raw("AT+GAPSTATUS")
                    if not any(row.get("advertising") is False for row in current.replies) or any(
                        row.get("connected") is True for row in current.replies
                    ):
                        raise
                else:
                    self.wait_event(
                        lambda event: (
                            event._received_order > requested_order
                            and isinstance(event.payload, Mapping)
                            and "260" in event.payload
                            and event.payload.get("evt", event.payload).get("action")
                            == "advertising stopped"
                        ),
                        timeout_s=3.0,
                    )
            self._owns_advertising = False
            self._emit("advertising_stopped")

    @contextmanager
    def advertising(self, *, adv_data: str | bytes | None = None) -> Iterator["BleuIODevice"]:
        """Own one advertising window and always stop it on scope exit."""
        body_error: BaseException | None = None
        owns_lifetime = False
        try:
            with self._operation_lock:
                if self._owns_advertising:
                    raise BleuIOError(f"{self.name}: already owns an advertising lifetime")
                owns_lifetime = True
                self.start_advertising(adv_data=adv_data)
            yield self
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            with self._operation_lock:
                if owns_lifetime and self._owns_advertising:
                    try:
                        self.stop_advertising()
                    except BaseException as cleanup:
                        if body_error is not None:
                            raise BaseExceptionGroup(
                                f"{self.name}: advertising scope and cleanup failed",
                                [body_error, cleanup],
                            ) from None
                        raise

    def connect(
        self,
        address: BleAddress | ScanResult | str,
        *,
        address_type: int | BleAddressType | None = None,
    ) -> CommandResult:
        """Connect to a typed peer and wait for the link event and GATT target.

        Strings default to public addresses; conflicting address types raise
        ``ValueError``. The command and completion waits are each bounded by
        eight seconds. A completion timeout attempts cancellation and propagates
        failure. Ownership remains until ``disconnect()``; prefer ``connected()``
        for automatic cleanup, including partially completed connections.
        """
        with self._operation_lock:
            target = _target_address(address, address_type=address_type)
            self.set_role("central")
            requested_order = _next_event_order()
            self._owns_connection = True
            result = self.raw(f"AT+GAPCONNECT={target.bleuio}", timeout_s=8.0)
            self._emit(
                "connect_requested",
                address=target.value,
                address_type=target.address_type.name.lower(),
                address_kind=target.kind.value,
            )
            completion_deadline = monotonic() + 8.0
            try:
                event = self.wait_event(
                    lambda event: (
                        event._received_order > requested_order
                        and isinstance(event.payload, Mapping)
                        and "256" in event.payload
                        and event.payload.get("evt", {}).get("action") == "connected"
                        and str(event.payload.get("evt", {}).get("addr", "")).upper()
                        == target.value
                    ),
                    timeout_s=8.0,
                )
                assert isinstance(event.payload, Mapping)
                connection_index = str(event.payload["256"]).upper()
                if len(connection_index) != 4 or any(
                    char not in "0123456789ABCDEF" for char in connection_index
                ):
                    raise BleuIOError(f"{self.name}: invalid connection index {connection_index!r}")
                # Standard firmware can publish "connected" before assigning its
                # default GATT target. Query the actual target until it matches this
                # link; this firmware can reject an explicit target setter with 4.
                while True:
                    remaining = completion_deadline - monotonic()
                    if remaining <= 0:
                        raise CommandTimeout(f"GATT target not ready for {self.name}")
                    target_status = self.raw("AT+TARGETCONN", timeout_s=min(3.0, remaining))
                    if any(
                        str(row.get("target", "")).upper() == connection_index
                        for row in target_status.replies
                    ):
                        break
                    sleep(min(0.01, max(0, completion_deadline - monotonic())))
            except CommandTimeout:
                self._cancel_connect()
                raise
            self._emit(
                "connected",
                address=target.value,
                address_type=target.address_type.name.lower(),
                address_kind=target.kind.value,
            )
            return result

    def _cancel_connect(self) -> None:
        try:
            self.raw("AT+CANCELCONNECT")
        except CommandRejected as exc:
            # Observed Standard firmware returns 5 (not allowed) when no attempt
            # remains; 2 is already done. Other rejections remain failures.
            if exc.error_code not in {2, 5}:
                raise

    def disconnect(self) -> CommandResult:
        """Cancel pending connection work or disconnect, confirming idle target release.

        Return the command result or already-idle status. Ownership clears only
        after confirmation; rejection, timeout and session errors propagate.
        """
        with self._operation_lock:
            status = self.raw("AT+GAPSTATUS")
            if self._owns_connection and not any(
                row.get("connected") is True for row in status.replies
            ):
                # Serial close does not cancel pending radio work. Recheck after
                # cancellation in case the link established during the status query.
                self._cancel_connect()
                status = self.raw("AT+GAPSTATUS")
            if not any(row.get("connected") is True for row in status.replies):
                if self._owns_connection:
                    status = self._wait_connection_released()
                self._owns_connection = False
                return status
            requested_order = _next_event_order()
            result = self.raw("AT+GAPDISCONNECT", timeout_s=5.0)
            self._emit("disconnect_requested")
            self.wait_event(
                lambda event: (
                    event._received_order > requested_order
                    and isinstance(event.payload, Mapping)
                    and "258" in event.payload
                    and event.payload.get("evt", {}).get("action") == "disconnected"
                ),
                timeout_s=5.0,
            )
            self._wait_connection_released()
            self._owns_connection = False
            self._emit("disconnected")
            return result

    def _wait_connection_released(self, *, timeout_s: float = 5.0) -> CommandResult:
        """Wait for both link state and target release before allowing another link."""
        deadline = monotonic() + timeout_s
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CommandTimeout(f"connection release not ready for {self.name}")
            status = self.raw("AT+GAPSTATUS", timeout_s=min(3.0, remaining))
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CommandTimeout(f"connection release not ready for {self.name}")
            target = self.raw("AT+TARGETCONN", timeout_s=min(3.0, remaining))
            if (
                any(row.get("connected") is False for row in status.replies)
                and not any(row.get("connected") is True for row in status.replies)
                and any(
                    str(row.get("target", "")).upper() in {"NONE", "FFFF"} for row in target.replies
                )
            ):
                return status
            sleep(min(0.01, max(0, deadline - monotonic())))

    @contextmanager
    def connected(
        self,
        address: BleAddress | ScanResult | str,
        *,
        address_type: int | BleAddressType | None = None,
    ) -> Iterator["BleuIODevice"]:
        """Own one connection and always disconnect it on scope exit."""
        body_error: BaseException | None = None
        owns_lifetime = False
        try:
            with self._operation_lock:
                if self._owns_connection:
                    raise BleuIOError(f"{self.name}: already owns a connection lifetime")
                owns_lifetime = True
                self.connect(address, address_type=address_type)
            yield self
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            with self._operation_lock:
                if owns_lifetime and self._owns_connection:
                    try:
                        self.disconnect()
                    except BaseException as cleanup:
                        if body_error is not None:
                            raise BaseExceptionGroup(
                                f"{self.name}: connection scope and cleanup failed",
                                [body_error, cleanup],
                            ) from None
                        raise

    def read_gatt(self, handle: int, *, timeout_s: float = 5.0) -> bytes:
        """Read one attribute on the current target connection and await its value.

        Reopen after an incomplete read before reading again: the firmware's
        asynchronous values cannot be correlated to a later request. A rejected
        command does not require reopening.
        """
        if not isinstance(handle, int) or isinstance(handle, bool) or not 1 <= handle <= 0xFFFF:
            raise ValueError("GATT handle must be between 1 and 65535")
        if not isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("read timeout must be finite and > 0")
        key = f"{handle:04x}"

        def fields(event: BleEvent) -> Mapping[str, Any]:
            if not isinstance(event.payload, Mapping) or "775" not in event.payload:
                return {}
            payload = event.payload
            details = payload.get("evt", payload.get(key, {}))
            if not isinstance(details, Mapping):
                return {}
            if str(details.get("handle", key)).lower() != key:
                return {}
            return details

        def integer(value: object, field: str) -> int:
            if type(value) is int:
                return value
            if isinstance(value, str):
                try:
                    return int(value)
                except ValueError:
                    pass
            raise BleuIOError(f"{self.name}: invalid GATT read {key} {field}")

        def completed(event: BleEvent) -> bool:
            details = fields(event)
            if not details:
                return False
            try:
                status = integer(details.get("readStatus", 0), "status")
            except BleuIOError:
                return True
            return status != 0 or "hex" in details

        with self._operation_lock:
            if self._gatt_read_uncertain:
                raise BleuIOError(
                    f"{self.name}: previous GATT read did not complete; reopen before another read"
                )
            deadline = monotonic() + timeout_s
            requested_order = _next_event_order()
            try:
                self.raw(f"AT+GATTCREAD={key.upper()}", timeout_s=timeout_s)
                event = self.wait_event(
                    lambda event: event._received_order > requested_order and completed(event),
                    timeout_s=max(0.001, deadline - monotonic()),
                )
            except CommandRejected:
                raise
            except BaseException:
                self._gatt_read_uncertain = True
                raise
            data = fields(event)
            status = integer(data.get("readStatus", 0), "status")
            if status != 0:
                raise BleuIOError(f"{self.name}: GATT read {key} failed: {status}")
            raw_hex = data.get("hex")
            if not isinstance(raw_hex, str):
                raise BleuIOError(f"{self.name}: GATT read {key} has no hex payload")
            try:
                value = bytes.fromhex(raw_hex[2:] if raw_hex.startswith(("0x", "0X")) else raw_hex)
            except ValueError as exc:
                raise BleuIOError(f"{self.name}: invalid GATT read {key} hex payload") from exc
            if "len" in data:
                expected_len = integer(data["len"], "length")
                if len(value) != expected_len:
                    raise BleuIOError(f"{self.name}: GATT read {key} length mismatch")
            self._emit("gatt_read", handle=key, size=len(value), value_hex=value.hex())
            return value

    def read_gatt_decoded(
        self,
        handle: int,
        codec: PayloadDecoder,
        *,
        timeout_s: float = 5.0,
    ) -> dict[str, Any]:
        """Read one GATT value and decode its application bytes with a project codec."""
        return codec.decode(self.read_gatt(handle, timeout_s=timeout_s))

    def gatt(
        self,
        attribute: BleGatt | int,
        *,
        codec: PayloadDecoder | BleCodec | None = None,
        timeout_s: float = 5.0,
    ) -> GattValue:
        """Read one GATT value with optional built-in or project decoding.

        ``BleGatt`` identifiers provide the built-in handle and StructWire codec.
        Integer handles keep the raw/advanced path available; pass a project codec
        when that payload has a project-owned schema.
        """
        codec_id: str | None = None
        decoder: PayloadDecoder | None
        if isinstance(attribute, BleGatt):
            if codec is not None:
                raise ValueError("BleGatt identifiers already define their codec")
            handle = attribute.handle
            codec_id = attribute.codec_id.value
            decoder = attribute.codec
        else:
            handle = attribute
            if isinstance(codec, BleCodec):
                codec_id = codec.value
                decoder = builtin_codec(codec)
            else:
                decoder = codec
        if decoder is not None and not callable(getattr(decoder, "decode", None)):
            raise ValueError("codec must be a BleCodec identifier or a payload decoder")
        raw = self.read_gatt(handle, timeout_s=timeout_s)
        decoded = None if decoder is None else decoder.decode(raw)
        return GattValue(handle=handle, raw=raw, decoded=decoded, codec_id=codec_id)

    def reset(self, *, timeout_s: float | None = None) -> None:
        """Observe departure, re-resolve application identity, then verify communication."""
        with self._operation_lock:
            timeout = self._reset_timeout_s if timeout_s is None else timeout_s
            if not isfinite(timeout) or timeout <= 0:
                raise ValueError("reset timeout must be finite and > 0")
            deadline = monotonic() + timeout
            client = self._require_client()
            old_port = self._resolver.resolve(self.selector)
            self._transition(DeviceState.RESETTING, endpoint=old_port.device)
            last_error: Exception | None = None
            try:
                try:
                    client.command("ATR", timeout_s=min(3.0, timeout))
                except (CommandTimeout, DeviceDisconnected) as exc:
                    # ATR can disconnect during write/flush or before its end record.
                    self._emit("reset_disconnect", error=repr(exc))
                finally:
                    self.close(failed=True)
                self._transition(DeviceState.RECOVERING, old_endpoint=old_port.device)
                departed = False
                while monotonic() < deadline:
                    try:
                        port = self._resolver.resolve(self.selector)
                    except PortInventoryChanged as exc:
                        last_error = exc
                        self._emit("reset_inventory_changed", error=repr(exc))
                    except DeviceNotFound as exc:
                        last_error = exc
                        if not departed:
                            departed = True
                            self._emit("reset_departed", old_endpoint=old_port.device)
                    else:
                        if port != old_port and not departed:
                            departed = True
                            self._emit("reset_departed", old_endpoint=old_port.device)
                        if departed:
                            try:
                                self._open_endpoint(
                                    port.device,
                                    timeout_s=min(2.0, max(0.001, (deadline - monotonic()) / 4)),
                                )
                            except (OSError, DeviceDisconnected, CommandTimeout) as exc:
                                self.close(failed=True)
                                last_error = exc
                                self._emit("reset_retry", error=repr(exc))
                                self._transition(DeviceState.RECOVERING)
                            else:
                                self._emit(
                                    "reset_recovered",
                                    old_endpoint=old_port.device,
                                    new_endpoint=port.device,
                                )
                                return
                    sleep(min(0.05, max(0.0, deadline - monotonic())))
                detail = "application did not return" if departed else "departure was not observed"
                raise BleuIOError(
                    f"{self.name} reset failed within {timeout:.3f}s: {detail}"
                ) from last_error
            except BaseException as exc:
                self._state = DeviceState.FAILED
                errors = [exc]
                try:
                    self.close(failed=True)
                except BaseException as cleanup:
                    errors.append(cleanup)
                try:
                    self._transition(DeviceState.FAILED, error=repr(exc))
                except BaseException as recording:
                    errors.append(recording)
                if len(errors) > 1:
                    raise BaseExceptionGroup(
                        f"{self.name}: reset and cleanup failed", errors
                    ) from None
                raise

    def _require_client(self) -> BleuIOClient:
        if self._client is not None and self._client.failure is not None:
            raise DeviceDisconnected(
                f"{self.name}: BleuIO session failed"
            ) from self._client.failure
        if self._state != DeviceState.READY or self._client is None:
            raise BleuIOError(f"{self.name} is not ready (state={self._state.value})")
        return self._client

    def _client_evidence(self, kind: str, fields: Mapping[str, Any]) -> None:
        self._emit(f"serial_{kind}", **fields)

    def _transition(self, state: DeviceState, **fields: Any) -> None:
        self._state = state
        self._emit("state", state=state.value, **fields)

    def _emit(self, kind: str, **fields: Any) -> None:
        if self._evidence is not None:
            self._evidence(kind, {"device": self.name, **fields})


def _manufacturer_id(value: object | None) -> int | None:
    """Decode BleuIO's optional hexadecimal manufacturer identifier."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(str(value), 16)
        except ValueError as exc:
            raise BleuIOError(f"invalid manufacturer identifier {value!r}") from exc
    else:
        raise BleuIOError(f"invalid manufacturer identifier {value!r}")
    if not 0 <= parsed <= 0xFFFF:
        raise BleuIOError(f"invalid manufacturer identifier {value!r}")
    return parsed


def _target_address(
    address: BleAddress | ScanResult | str,
    *,
    address_type: int | BleAddressType | None,
) -> BleAddress:
    explicit_type = None if address_type is None else coerce_address_type(address_type)
    if isinstance(address, ScanResult):
        address = address.address
    if isinstance(address, BleAddress):
        if explicit_type is not None and explicit_type is not address.address_type:
            raise ValueError("address_type conflicts with typed Bluetooth address")
        return address
    if not isinstance(address, str):
        raise ValueError("Bluetooth address must be a scan result, typed address or string")
    raw = address.strip()
    if raw.startswith("["):
        parsed = BleAddress.parse(raw)
        if explicit_type is not None and explicit_type is not parsed.address_type:
            raise ValueError("address_type conflicts with address prefix")
        return parsed
    fallback = BleAddressType.PUBLIC if explicit_type is None else explicit_type
    return BleAddress.parse(raw, fallback_type=fallback)


def _scan_duration(seconds: float, timeout_s: float | None) -> int:
    if isinstance(seconds, bool) or not isfinite(seconds) or seconds <= 0:
        raise ValueError("scan seconds must be finite and > 0")
    if timeout_s is not None and (
        isinstance(timeout_s, bool) or not isfinite(timeout_s) or timeout_s <= 0
    ):
        raise ValueError("scan timeout must be finite and > 0")
    return max(1, int(round(seconds)))


def _record_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value, 10)
        except ValueError:
            pass
    raise BleuIOError(f"invalid scan {key} value {value!r}")
