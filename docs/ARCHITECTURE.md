# BLE plugin architecture

## Ownership

AutoSpan owns component lifecycle, resource claims, gates and evidence. `autospan-ble` owns serial resolution, BleuIO protocol semantics, BLE role/state and recovery.

No BLE or BleuIO concept is added to AutoSpan core.

## Public API rule

The implementation may be complicated because real hardware is complicated; normal tests must not be.
The scenario-facing API therefore keeps robustness below a small domain layer:

```text
BleuIO / USB / workers / StructWire / evidence
                    ↓
              BleuIODevice
                    ↓
       ScanResults / GattValue / contexts
                    ↓
                  test
```

`BleBench.from_runtime(...)`, `scan(...).expect(...)` and `gatt(...).expect(...)` are thin helpers, not a second test
framework. They are the same objects used from pytest, scripts and an interactive runtime. Raw
bytes, raw AT commands and the primitive read/connect methods remain available underneath.

Built-in `BleCodec` and `BleGatt` values are stable symbolic identifiers. Their string values match
the packaged StructWire YAML codec identifiers exactly, and tests enforce that contract. Normal
scenarios therefore use enums instead of repeating schema-name strings. Project-owned schemas stay
project-owned and can pass their compiled StructWire codec directly to `gatt(...)`.

## Physical identity

A selector is converted to a canonical claim key before hardware opens. Two logical device configs that name the same stable selector are rejected by plugin configuration, and AutoSpan receives the same canonical claim for runtime ownership.

At acquisition the bench resolves every selector before opening any endpoint and
rejects two selectors resolving to the same device, including serial/location/path
aliases. POSIX serial opens also request exclusive ownership. Planning stays
hardware-free. Use application VID/PID plus serial identity on the qualified bench.
PID `6001` temporarily appears during reset with a generic bootloader serial; it
is excluded from the application selector.

## Concurrency

The client retains at most 4096 unsolicited records, including records deferred
by event waits and scans. Exceeding this limit fails the session explicitly;
records are not silently dropped and the receive worker does not wait for space.
Close and reopen the device before continuing. Deferred records retain the
existing append-to-tail order when restored.

One logical dongle has one serialized command transaction at a time. Its reader continuously separates command records (`C/A/R/E`) from asynchronous records (`S/SF/ST/SE` and vendor events). N dongles therefore have N independent command lanes and N independent RX workers.

Transport open/close coordinate with separate RX and TX locks. Reads use finite
polling intervals and writes use pyserial's write timeout; shutdown waits for
in-flight I/O without closing a descriptor underneath it. Writes do not call
POSIX `tcdrain`, which has no write-timeout guarantee.

The endpoint serializes complete operations: two scans cannot consume each other's
events, and reset/close cannot replace a session during an operation. A command
deadline includes waiting for its command lane. A response timeout invalidates that
lane until reopening, because a late frame cannot safely identify a subsequent
identical command. Unindexed completion events retain their receive time; link,
advertising-stop and GATT operations reject records received before the current
request. Old passive-role events therefore cannot complete a later active operation.

## Reset

Reset is a lifecycle transition, not an AT helper:

```text
READY -> RESETTING -> ATR -> close old endpoint -> RECOVERING
      -> observe departure -> resolve stable application identity -> open current endpoint
      -> AT / ATE0 / ATV1 / ATI -> READY
```

The recovery loop waits on repeatedly observable device availability. It does not sleep for a fixed boot time and then reopen a remembered tty.

One total deadline bounds reset/recovery. Expected write/reader disconnects and
missing application endpoints are retryable. Rejection, ambiguity and programming
errors propagate. A narrowly identified pyserial Linux metadata-disappearance race
is retried as an inventory change, without claiming the selected device departed.
Transient serial open failures are recorded and retried within the same deadline.
READY requires successful initialization and an ATI identity reply.

## Evidence and cleanup

Lifecycle events, completed commands and scan results use logical device sources
and the caller's test/step context. Raw TX/RX is written under a lock to a per-device
session JSONL artifact, committed after RX stops and the file closes. It does not
invent causal test/step attribution for asynchronous RX. RX failures remain
structured session events. AutoSpan does not interpret the vendor artifact format.

Partial initialization closes its transport. Bench startup rolls back every
attempted owner; teardown attempts every owner even if another close fails.
Startup errors and rollback errors are retained together. Failed serial cleanup
retains the client and reports FAILED; cleanup must succeed before a replacement
session can open. Evidence failures cannot bypass startup rollback. Serial shutdown attempts
the final RX-worker join even when closing the transport raises, and reports both
errors if the worker also fails to stop within the bounded joins.
Endpoint close stops advertising started through its high-level API. Raw commands
are an escape hatch whose additional BLE state remains the caller's responsibility.

High-level connection/disconnection and advertising stop wait for actual completion
events. Disconnect also waits for an idle link status and an absent GATT target before
releasing ownership, because a completion event can precede firmware target cleanup.
`advertising()` and `connected(...)` are scoped convenience APIs over those
primitive operations: they preserve body failures, clean up partial acquisition and
report scope+cleanup failures together. They reject overlapping owned lifetimes before
changing radio state. `set_role(...)` stays explicit because role
selection has no symmetric release lifetime. Close disconnects a link requested through
the endpoint API before closing the transport, and cancels an owned pending connection attempt if no link has
established. Idle cancellation error 5 is an observed vendor case; other errors
propagate. `scan()` decodes BleuIO discovery rows into typed Bluetooth addresses while
`scan_target()` decodes `ST` rows into raw advertising/scan-response bytes and standard
Bluetooth AD structures. Envelope routing and variable-length framing belong to `autospan-ble`;
fixed binary layouts come from its packaged YAML catalog and are decoded/encoded by the required
StructWire engine. GATT reads return
bytes from a successful, length-checked asynchronous read event. Project application payloads remain project-owned: `gatt(...)`,
`read_gatt_decoded(...)` and manufacturer-data helpers accept compiled StructWire codecs through a
small decoder protocol. `BleGatt` supplies the built-in handle+codec pair where qualified. Raw bytes
remain available for exact evidence and codec-specific options. StructWire is installed as a required library dependency; it is not
a separately registered AutoSpan component. A read timeout blocks
another read until reopening so late unindexed data cannot satisfy it.

Firmware can publish a connection event and then fail link establishment, starting its own
retry loop. A failed GATT completion remains a failure even if an earlier connection event
and target query succeeded. On Standard firmware, a successful
`AT+CANCELCONNECT` reply followed by `connected=false` can still be followed by a later
retry: the next scan can be rejected with error 9 (busy connecting). The adapter
does not silently retry a failed GATT assertion. Use explicit cancellation or `reset()`
to recover this observed firmware state; serial reopen alone does not reset radio work.
See [failed connection recovery](BENCH_BRINGUP.md#failed-connection-recovery).

Standard firmware can emit `connected` before selecting its GATT target. Connect
queries `AT+TARGETCONN` until the target matches the fresh connection index, within
the completion deadline. It does not depend on an optional target-change event or
assume an accepted connection request is ready for GATT. Advertising cleanup sends
an explicit stop for its owned lifetime even if an earlier status was idle: a
peripheral can resume advertising after peer disconnect. Error 5 from an idle stop
is accepted only after another status confirms no advertising or connection.

## Passive observation

Endpoints act. Observers observe. `BleObserver` is a separate contract from
`BleuIODevice`; an observer exposes resolution, readiness and capture sessions,
not scans, connections or GATT operations.

`NrfSnifferObserver` translates Nordic extcap discovery/capture on POSIX. The Raytac
MDBT50Q-CX-40 profile identifies compatible USB firmware. `RaytacObserver` is a
profile-facing alias for that backend.
Physical USB resolution is reused from the existing selector module. Its current
location under `bleuio` does not introduce BleuIO commands into the observer.

Bench startup resolves all physical claims before opening any owner. Observer
readiness probes precede active endpoint opens, since Nordic discovery can inspect
other serial ports. A resolved tty alone is not READY: extcap must report exactly one
matching responding interface. Interface names and versions are discovered, not guessed.
Extcap wrappers that report the firmware version include it in the interface ID.
When nRF Util lists a bare tty from USB identity, the adapter sends a bounded
Nordic UART version request and validates its reply before READY. The temporary
serial probe closes before capture or active endpoint acquisition.

A capture session owns private data/control FIFOs, one copy worker, one extcap process group
and one output stream. The public session handle remains owned by its observer: direct
`session.stop()`, context exit and `observer.stop_capture()` all converge on the same
owner transition, clear the active session, and leave the observer READY or FAILED. A failed cleanup retains the active
session for retry; it cannot be reopened while that owner remains.
Its startup deadline requires a valid classic-PCAP header.
Stop signals the whole process group, reaps the leader and joins the worker before
validating records and returning counts. Crashes, truncated records and forced kills
remain failures. Existing output files are never overwritten. Missing traffic can
produce a valid header-only capture with zero packets; this is not evidence of RF.
`session.wait_for_packets(timeout_s=None)` optionally waits for a complete PCAP
record, using the configured startup timeout by default. RF scenarios call it while
the peer advertises, before beginning their bounded scan window. A PCAP header can
precede Nordic's RF initialization by several seconds. Ordinary opaque captures
still permit zero packets; the later MAC/CRC check establishes peer capture.
The session sends extcap's Initialized control message, keeps the control input open,
and drains toolbar updates so current Nordic extcap keeps capturing. Vendor control
errors fail the session. These pipes belong to the capture lifetime, without GUI ownership.

`BleBench.capture(name, evidence)` owns a window around scenario operations. AutoSpan
reserves and commits the opaque PCAP, including hashes and completion status. Start,
stop and failure events run in the caller's test/step scope. The worker copies bytes
without emitting packet events or inventing causal attribution. A failed scenario
retains an incomplete artifact for diagnosis. Evidence controls the final filename.

Teardown attempts endpoints and observers in reverse acquisition order, including
partial startup. `check_sniffer` is shared by the installed RF testcase and console
helper. It runs endpoint discovery inside a capture window, then uses external TShark
to assert CRC-valid packets from that advertiser's MAC. Decoding finishes while the
evidence reservation still owns its temporary path. That check may finalize the session
early; session stop is idempotent so the surrounding evidence context can still close it
without creating a second ownership path. Normalized packet APIs, capture
filters and connection following remain deferred. StructWire is a required runtime library,
with no dependency back on BLE or AutoSpan. The BLE plugin owns its common layouts and routing;
StructWire handles fixed binary fields and application payloads. JSON, Bluetooth address syntax,
variable-length framing and PCAP recording remain adapter concerns. The installed discovery
scenario uses the common six-byte BleuIO Device Name codec.

## Explicit firmware maintenance

`ble.sniffer_setup` is a separate, exclusive maintenance component with hardware and
destructive gates. Its configuration cannot also create endpoint/observer owners.
Acquisition has no firmware side effects; only its explicit setup operation writes.
`BleObserver` remains passive and exposes no flashing method.

The Raytac adapter validates the configured DFU ZIP checksum and application-only
manifest, selects one supported USB bootloader through Nordic inventory, and runs
`nrfutil device program` with that device's serial and `nordicDfu` trait. It never
erases/replaces the bootloader or invokes recovery. A bounded deadline covers vendor
execution, application resolution by physical USB location, and a sniffer version
response. The old serial/tty is not reused as identity. Vendor output and final readiness
are recorded in the caller's evidence context; owned processes are always reaped.

## Extraction rule

The serial transport stays inside `autospan-ble`. Reuse alone does not make device
I/O an orchestration responsibility. Extract a shared library only when independent
users need the same contract; keep vendor translation and BLE behavior in this package.
