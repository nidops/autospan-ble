# BLE bench bring-up

## Hardware and tools

The Linux bench profile uses two BleuIO Standard endpoints, chip
`DA14683 (P25Q80LE)`, firmware `2.7.9.78`, and application VID/PID `2dcf:6002`.
The temporary bootloader PID `6001` must not be selected as a ready application.
Keep physical serial numbers, USB locations and tty paths in ignored local files.

Optional passive capture uses a Raytac MDBT50Q-CX-40 running Nordic's
nRF52840-dongle sniffer image `4.1.1`, application VID/PID `1915:522a`.
Its DFU bootloader uses PID `521f` and belongs to the separate maintenance config.

Use Python 3.11 or newer and pyserial for active endpoints. Capture requires a
working Nordic extcap executable and POSIX FIFOs. The known-advertiser assertion
also requires TShark. Nordic's `device` and `ble-sniffer` commands are external
tools used by explicit firmware setup.

## Configure two endpoints

Install BLE as described in the [README](../README.md), then
inventory the connected devices:

```bash
lsusb
ls -l /dev/serial/by-id/
autospan-ble-inventory
cp autospan.yaml ble.local.yaml
```

Replace both placeholder serials in `ble.local.yaml`. The template names the
logical endpoints `black` and `white`. Prefer VID plus unique application serial;
use USB location if the serial is unsuitable. A fixed tty path is a manual
fallback and cannot preserve physical identity through renumbering.

```bash
autospan plan --config ble.local.yaml
autospan console --config ble.local.yaml --hardware --plain --exec "ble status"
autospan test --config ble.local.yaml -- --hardware -q
# Checkout acceptance, including 20 resets per endpoint:
python -m pip install -e tests
autospan test --config ble.local.yaml --set 'autospan.plugins=[ble, ble-acceptance]' -- --hardware -q
```

The hardware gate is checked before devices open. The active scenario reverses
both endpoints' roles and requires:

```text
advertise -> typed scan assertion -> confirmed connection
          -> Device Name read -> confirmed disconnect -> stop advertising
```

Keep the advertising scope open through connection and GATT. The built-in Device
Name is handle `0003`, with the six-byte value `b"BleuIO"` on this profile. Other
firmware and project GATT layouts need their own handles and codecs.

## Endpoint readiness and cleanup

- `ATE0` returns `ECHO OFF` in default mode.
- Repeating an already selected role returns error 2. Role operations query state
  and change it only when necessary.
- Scan rows may lack an index, repeat an address or omit name/manufacturer metadata.
- A connection event can precede GATT target assignment. Connect waits for the
  matching `AT+TARGETCONN` value before permitting reads.
- GATT event `775` carries handle/read status and separate ASCII/hex responses.
  The adapter returns the length-checked hex bytes.
- Disconnect waits for idle connection status and an absent target before releasing
  ownership. Owned advertising cleanup explicitly sends the stop command because
  a peripheral can resume advertising after disconnect.

After repeated role reversals, require both endpoints to be idle and all workers,
serial handles and observer processes to be released. Include failure paths such
as a rejected codec assertion so cleanup is exercised when the test body fails.

### Failed connection recovery

Standard firmware can announce a connection and then fail link establishment or
the GATT read, starting its own retry loop. A successful `AT+CANCELCONNECT` reply
and `connected=false` can still be followed by another retry; a subsequent scan
can return error 9, busy connecting.

Treat the failed read as a test failure. Use explicit cancellation
(`device.raw("AT+CANCELCONNECT")`) and confirm idle state, or use `device.reset()`
before another cycle if retries continue. Reopening the serial transport alone
does not reset radio work.

## Reset recovery

Monitor USB transitions with `udevadm monitor --udev --property` and reset one
endpoint at a time. Repeat the cycle for each logical device:

```bash
autospan console --config ble.local.yaml --hardware --plain --exec "ble reset black"
autospan console --config ble.local.yaml --hardware --plain --exec "ble reset white"
```

Acceptance requires observed application departure, stable application identity
resolution, successful initialization and ATI, and READY within the configured
deadline. The template allows 25 seconds. Application removal, temporary
bootloader enumeration and return to the original application serial are separate
states; a command acknowledgement or tty path alone is insufficient.

Recovery retries transient inventory/open failures, including udev permission
restoration, within the deadline. The old RX worker and serial session close
before rebinding. Verify tty renumbering with the regression fixtures as well as
the USB transitions visible on the bench.

## Advertising payloads and private addresses

Standard firmware prepends three bytes of Flags and truncates custom advertising
data above 28 bytes. The binary API reserves that space and rejects larger custom
payloads before changing radio state.

For payload and privacy checks:

1. Save each endpoint's address mode and advertising data, and restore them even
   when an assertion fails.
2. Keep advertising active, use `scan_target(peer)`, and compare the received
   `AdvertisingReport.payload` with the verbose `ST` record for that peer.
3. Encode and decode a project manufacturer or GATT payload with its StructWire
   codec. Compare raw bytes as well as named values.
4. Set `PRIVATE_RANDOM_RESOLVABLE`, discover the on-air peer and connect using the
   typed `ScanResult` or `BleAddress`. `AT+GETMAC` identifies the device and need
   not match its current private on-air address.
5. Restore the original address mode before the known-MAC sniffer check.

Fake fixtures verify routing and decoding. Physical tests establish the RF and
firmware behavior. Keep private IRKs and proprietary schemas outside this repo.
See [payload decoding](PAYLOAD_DECODING.md) for the framing and codec APIs.

## Add a passive observer

Copy `autospan.raytac.yaml` to ignored `raytac.local.yaml`. Its endpoints are named
`scanner` and `peripheral`, with observer `air`. Configure application serials,
`extcap_command` and `capture_decoder_command: [tshark]` using executable arguments.

```bash
autospan plan --config raytac.local.yaml
autospan console --config raytac.local.yaml --hardware --plain --exec "ble capture air 5"
autospan console --config raytac.local.yaml --hardware --plain --exec "ble sniffer check air peripheral scanner"
autospan test --config raytac.local.yaml -- --hardware -q
# Full checkout acceptance, including 20 capture start/stop windows:
python -m pip install -e tests
autospan test --config raytac.local.yaml --set 'autospan.plugins=[ble, ble-acceptance]' -- --hardware -q
```

READY requires a responding sniffer interface. Capture startup requires a valid
PCAP header. For RF assertions, call `session.wait_for_packets()` while advertising
is active before starting the scan window; the header can precede RF startup.
A valid empty capture remains supported for callers that do not require traffic.

The session owns its data/control FIFOs, process group and copy worker. Stop is
bounded, and abnormal exit or forced termination fails the capture. Repeat capture
windows in both endpoint role directions and verify no owner survives teardown.

Require typed peer discovery and CRC-valid advertising packets with the same MAC
in the passive capture. Nonzero packet counts alone cannot establish the peer.
Endpoint scenarios establish connection/GATT behavior separately. Connection
following, decryption and a normalized packet API are outside this capture contract.

## Explicit sniffer firmware setup

Setup uses a separate `ble.sniffer_setup` component and requires both `--hardware`
and `--destructive`. Normal bench acquisition and capture do not program firmware.
Setup requires POSIX because it verifies firmware through the capture backend;
unsupported platforms are rejected before device access or programming.
Use an official, checksum-pinned, application-only DFU ZIP that preserves the
bootloader. A ready sniffer is verified without another upload.

Copy `autospan.sniffer-setup.yaml` to ignored `sniffer-setup.local.yaml`. Set its
physical USB location without the interface suffix, DFU ZIP path, verified
SHA-256 and vendor executable arguments. Keep the device on that port so identity
survives firmware and serial changes. Use [Raytac's button/reconnect
procedure](https://www.raytac.com/tw/news/ins.php?index_id=149) to enter the bootloader.

```bash
autospan test --config sniffer-setup.local.yaml -- --hardware --destructive -k sniffer_setup -q
autospan console --config sniffer-setup.local.yaml --hardware --destructive --plain --exec "ble sniffer setup"
```

After setup, inventory again and use the application identity in the normal
observer config. Maintenance and active bench ownership stay in separate configs.

## Evidence

Structured commands and assertions retain caller test/step context. Raw serial RX
is session-scoped because the RX worker cannot infer causal attribution. Passive
captures have a separate observer/session owner and are committed as opaque files.

Use the manifest to verify each artifact's completion state, byte size and SHA-256.
Keep generated evidence, physical identities and local tool paths in ignored
configs and `artifacts/`. See [contributing](../CONTRIBUTING.md) for acceptance coverage.
