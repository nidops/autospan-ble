"""Public contract for passive BLE observers."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Protocol

from autospan_ble.observer.types import CaptureResult, ObserverState
from autospan_ble.bleuio.types import PortInfo


class CaptureSession(Protocol):
    """One observer-owned capture window; stopping it also updates observer state."""

    @property
    def result(self) -> CaptureResult | None:
        """Return capture statistics after a successful stop, or None before then."""
        ...

    def wait_for_packets(self, timeout_s: float | None = None) -> None:
        """Wait for a complete packet, using the startup timeout when omitted.

        Raise TimeoutError when the deadline expires, or RuntimeError if capture
        fails or stops without a packet. A packet alone does not identify its sender.
        """
        ...

    def stop(self) -> CaptureResult:
        """Stop capture and return its statistics; repeated successful stops reuse them.

        Capture or cleanup failures propagate. Evidence may move the captured file
        when committing it, so use the manifest for its final path.
        """
        ...

    def __enter__(self) -> CaptureSession: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class BleObserver(Protocol):
    """A passive BLE capture source with a lifetime independent of BLE endpoints."""

    @property
    def state(self) -> ObserverState:
        """Return the current closed, ready, capturing or failed state."""
        ...

    @property
    def endpoint(self) -> str | None:
        """Return the resolved endpoint while open, or None when closed."""
        ...

    def resolve(self) -> PortInfo:
        """Find the selected physical device without starting capture.

        Missing or ambiguous devices raise selector errors.
        """
        ...

    def open(self) -> None:
        """Resolve and probe the observer without starting capture.

        An already open observer stays open. Close a failed observer before
        reopening it. Missing tools, unsupported platforms and probe failures
        propagate to the caller.
        """
        ...

    def start_capture(self, path: Path) -> CaptureSession:
        """Start one capture at path and return its owning session.

        Requires READY; overlapping captures raise RuntimeError. Startup verifies
        the capture header. Use wait_for_packets when traffic is required.
        """
        ...

    def stop_capture(self) -> CaptureResult:
        """Stop the active session; raise RuntimeError if there is no session."""
        ...

    def close(self) -> None:
        """Stop any active capture and release owned resources.

        Cleanup failures propagate; the observer must not be reused until its
        resources have been released.
        """
        ...
