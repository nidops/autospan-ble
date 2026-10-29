"""BleuIO-specific errors."""


class BleuIOError(RuntimeError):
    """Base error for BleuIO operations."""


class DeviceNotFound(BleuIOError):
    """No serial endpoint currently matches the logical device selector."""


class AmbiguousDevice(BleuIOError):
    """More than one physical serial endpoint matches a selector."""


class DeviceDisconnected(BleuIOError):
    """The serial endpoint disappeared or failed while in use."""


class PortInventoryChanged(BleuIOError):
    """USB metadata vanished during a serial inventory snapshot; retry enumeration."""


class CommandTimeout(BleuIOError):
    """A BleuIO command did not complete within its deadline."""


class CommandRejected(BleuIOError):
    """BleuIO acknowledged a command with a non-zero error."""

    def __init__(self, message: str, *, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
