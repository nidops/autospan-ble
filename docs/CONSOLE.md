# BLE console

The console uses the same runtime-owned bench as pytest and Python scripts.
Install this checkout with `python -m pip install -e ".[test]"` to include the
AutoSpan package and its Textual interface. BLE commands use plugin API 1.

## Start a session

From the BLE checkout:

```bash
autospan console --config autospan.fake.yaml
autospan console --config autospan.fake.yaml --plain --exec "ble status"
autospan console --config autospan.fake.yaml --exec "ble scan scanner 1"
autospan console --config autospan.fake.yaml --plain --exec "ble reset scanner"
```

The fake configuration names its endpoints `scanner` and `peripheral`. It has no
passive observer or firmware setup component. Fake observations test command
behavior; physical RF checks require the corresponding hardware configuration.

For hardware, copy `autospan.raytac.yaml` to `bench.local.yaml`, set the USB
selectors and executable paths using [bench bring-up](BENCH_BRINGUP.md), then run:

```bash
autospan console --config bench.local.yaml --hardware
```

## Commands

Core commands include `help`, `components`, `evidence` and `exit`.

| Command | Purpose |
|---|---|
| `ble status` | Show logical endpoint and observer states |
| `ble scan DEVICE [SECONDS]` | Print typed scan observations; default 2 seconds |
| `ble reset DEVICE` | Reset one BleuIO and recover its stable identity |
| `ble capture OBSERVER [SECONDS]` | Record a passive PCAP window; default 5 seconds |
| `ble sniffer check OBSERVER ADVERTISER SCANNER` | Verify discovery and CRC-valid captured packets from the advertiser |
| `ble sniffer setup` | Program or verify firmware using a separate maintenance configuration |

Device and observer names come from your configuration. The Raytac example uses
`scanner`, `peripheral` and `air`. For example, `ble sniffer check air peripheral
scanner` checks the known advertiser using the configured capture decoder.

This check requires the advertiser's on-air address to match `AT+GETMAC`; it
does not resolve rotating/private addresses. Restore the known-MAC address mode
after privacy tests before running it. See
[private-address checks](BENCH_BRINGUP.md#advertising-payloads-and-private-addresses).

Firmware setup requires reviewed firmware hashes and tool paths in a copy of
`autospan.sniffer-setup.yaml`. Run it only as an intentional maintenance operation:

```bash
autospan console --config sniffer-setup.local.yaml --hardware --destructive --exec "ble sniffer setup"
```

## Python helpers

Tests and Python frontends can import `BleBench` from `autospan_ble` and
`check_sniffer` from `autospan_ble.checks`. Use `BleBench.from_runtime(runtime)`
to obtain the connected bench and pass it to helpers. See the [endpoint API](API.md)
for advertising, connection and GATT operations.

## Cancellation and evidence

In Textual, Ctrl+C requests cancellation and Ctrl+Q quits after the active command
finishes. `ble capture` closes its capture context before acknowledging cancellation.
Scan, reset and sniffer operations use synchronous drivers; a stop request waits
for safe completion. The runtime stays open until the command thread finishes.
Plain mode exits with EOF, `exit`, or Ctrl+C at an idle prompt. In Textual, use
Ctrl+Q to leave the session.

Commands record evidence steps, protocol/capture artifacts and a final session
verdict. Missing prerequisites fail the command and session. See [contributing](../CONTRIBUTING.md)
for hardware checks and report generation. Passive capture requires POSIX; terminal
input, clipboard and export behavior must be checked on the terminal in use.
