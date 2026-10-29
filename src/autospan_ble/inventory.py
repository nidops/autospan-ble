"""Non-destructive serial inventory helper for BLE bench bring-up."""

from __future__ import annotations

import json

from autospan_ble.bleuio.selector import PySerialPortEnumerator


def main() -> int:
    rows = []
    for port in PySerialPortEnumerator().ports():
        rows.append(
            {
                "device": port.device,
                "vid": f"0x{port.vid:04X}" if port.vid is not None else None,
                "pid": f"0x{port.pid:04X}" if port.pid is not None else None,
                "serial_number": port.serial_number,
                "location": port.location,
                "manufacturer": port.manufacturer,
                "product": port.product,
            }
        )
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
