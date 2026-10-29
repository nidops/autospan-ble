"""Scenario-facing BLE bench facade."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from os.path import realpath

from autospan.api import Evidence, Runtime
from autospan_ble.bleuio.device import BleuIODevice
from autospan_ble.observer.api import BleObserver, CaptureSession


class BleBench:
    """Runtime-owned BLE devices and observers with a small scenario-facing API."""

    COMPONENT = "ble.bench"

    @classmethod
    def from_runtime(cls, runtime: Runtime) -> "BleBench":
        """Return the configured BLE bench from pytest, scripts or an interactive runtime."""
        return runtime.require(cls.COMPONENT, cls)

    def __init__(
        self,
        devices: Mapping[str, BleuIODevice],
        observers: Mapping[str, BleObserver] | None = None,
        *,
        capture_decoder_command: tuple[str, ...] = ("tshark",),
    ) -> None:
        self._devices = dict(devices)
        self._observers = dict(observers or {})
        self.capture_decoder_command = capture_decoder_command
        if self._devices.keys() & self._observers.keys():
            raise ValueError("BLE endpoints and observers must have distinct logical names")

    def open(self) -> None:
        """Resolve identities, then open observers and endpoints without starting capture.

        Duplicate physical claims raise ``ValueError`` before acquisition. If
        startup fails, every attempted owner is closed in reverse order; startup
        and rollback errors are reported together in a ``BaseExceptionGroup``.
        AutoSpan normally calls this after checking hardware gates.
        """
        endpoints: dict[str, str] = {}
        for name, device in self._devices.items():
            endpoint = realpath(device.resolve().device)
            if endpoint in endpoints:
                raise ValueError(
                    f"physical device claimed by both {endpoints[endpoint]!r} and {name!r}"
                )
            endpoints[endpoint] = name
        for name, observer in self._observers.items():
            endpoint = realpath(observer.resolve().device)
            if endpoint in endpoints:
                raise ValueError(
                    f"physical device claimed by both {endpoints[endpoint]!r} and {name!r}"
                )
            endpoints[endpoint] = name
        # Probe passive observers before opening active endpoints: extcap discovery
        # can enumerate other serial ports. No capture is started by bench.open().
        opened: list[BleObserver | BleuIODevice] = []
        try:
            for observer in self._observers.values():
                opened.append(observer)
                observer.open()
            for device in self._devices.values():
                opened.append(device)
                device.open()
        except BaseException as exc:
            errors: list[BaseException] = [exc]
            for owner in reversed(opened):
                try:
                    owner.close()
                except BaseException as cleanup_error:
                    errors.append(cleanup_error)
            if len(errors) > 1:
                raise BaseExceptionGroup("BLE bench startup and cleanup failed", errors) from None
            raise

    def close(self) -> None:
        """Close all endpoints, then observers, attempting every owner despite failures.

        Closing releases owned radio work, transports and active capture sessions.
        Cleanup failures are raised together in a ``BaseExceptionGroup``.
        """
        errors: list[BaseException] = []
        for device in reversed(tuple(self._devices.values())):
            try:
                device.close()
            except BaseException as exc:
                errors.append(exc)
        for observer in reversed(tuple(self._observers.values())):
            try:
                observer.close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("BLE bench cleanup failed", errors)

    def device(self, name: str) -> BleuIODevice:
        """Return a runtime-owned endpoint; raise ``KeyError`` for unknown names."""
        try:
            return self._devices[name]
        except KeyError as exc:
            raise KeyError(
                f"unknown BLE device {name!r}; available: {', '.join(self.names())}"
            ) from exc

    def observer(self, name: str) -> BleObserver:
        """Return a runtime-owned observer; raise ``KeyError`` for unknown names."""
        try:
            return self._observers[name]
        except KeyError as exc:
            raise KeyError(
                f"unknown BLE observer {name!r}; available: {', '.join(self.observer_names())}"
            ) from exc

    def names(self) -> tuple[str, ...]:
        """Return configured endpoint names in configuration order."""
        return tuple(self._devices)

    def observer_names(self) -> tuple[str, ...]:
        """Return configured observer names in configuration order."""
        return tuple(self._observers)

    @contextmanager
    def capture(self, name: str, evidence: Evidence) -> Iterator[CaptureSession]:
        """Own a capture window and record it in the caller's test/step context.

        Session.result contains counts after exit. Its temporary path belongs to
        the evidence reservation; use the evidence manifest for the final path.
        """
        observer = self.observer(name)
        source = f"ble.observer.{name}"
        with evidence.reserve_artifact(
            kind="capture",
            source=source,
            suffix=".pcap",
            format="pcap",
            media_type="application/vnd.tcpdump.pcap",
            name=f"{name}-capture",
            attributes={"logical_observer": name, "correlation": "capture-window"},
        ) as artifact:
            try:
                session = observer.start_capture(artifact.path)
                body_error: BaseException | None = None
                try:
                    evidence.event("ble_capture_started", source=source, logical_observer=name)
                    yield session
                except BaseException as exc:
                    body_error = exc
                    raise
                finally:
                    try:
                        result = session.stop()
                    except BaseException as cleanup:
                        if body_error is not None and cleanup is not body_error:
                            raise BaseExceptionGroup(
                                "capture body and cleanup failed", [body_error, cleanup]
                            ) from None
                        raise
                    evidence.event(
                        "ble_capture_stopped",
                        source=source,
                        logical_observer=name,
                        byte_count=result.byte_count,
                        packet_count=result.packet_count,
                        duration_s=result.duration_s,
                    )
            except BaseException as exc:
                evidence.event("ble_capture_failed", source=source, error=repr(exc))
                raise

    def status(self) -> dict[str, dict[str, object]]:
        """Return each owner's kind, lifecycle state and resolved endpoint."""
        result: dict[str, dict[str, object]] = {
            name: {"kind": "endpoint", "state": device.state.value, "endpoint": device.endpoint}
            for name, device in self._devices.items()
        }
        result.update(
            {
                name: {
                    "kind": "observer",
                    "state": observer.state.value,
                    "endpoint": observer.endpoint,
                }
                for name, observer in self._observers.items()
            }
        )
        return result
