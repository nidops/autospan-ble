# BLE endpoint and console API

Examples assume a runtime acquired with the BLE configuration. The [README](../README.md)
contains a complete fake bench and runnable test; hardware acquisition additionally
requires `--hardware`. Bench endpoints and observers remain owned by the runtime.

## Console

See the [BLE console guide](CONSOLE.md) for commands, Python helpers, cancellation
and hardware setup. The console and tests use the endpoint API below.

## Endpoint API

Use the `scanner`, `peripheral` and `connected_scanner` fixtures from the
[README](../README.md#write-a-ble-test). Bind bench roles once in `conftest.py`;
each test requests the starting state it needs. For discovery and connection,
keep those operations visible:

```python
from autospan_ble import BleGatt


def test_peer_is_visible(scanner, peripheral):
    """Find our peer, connect, and read its name."""
    with peripheral.advertising():
        peer = scanner.scan(1).expect(address=peripheral.mac_address())
        with scanner.connected(peer):
            scanner.gatt(BleGatt.DEVICE_NAME).expect(name="BleuIO")
```

For a test that starts connected, request `connected_scanner` and go straight to
the read/assertion. Use a short docstring to explain the intent. Pytest assertions
and `expect(...)` both work; no base class or test decorator is required.

Pytest, scripts and interactive Python use the same runtime-owned objects.
Outside pytest, retrieve the bench with `BleBench.from_runtime(runtime)` and
select endpoints with `bench.device(name)`.

`scan()` returns an immutable tuple-like `ScanResults`, so normal iteration/indexing still
works. `expect(...)` adds useful test/console ergonomics and reports the peers that were
actually observed on failure:

```python
rows = scanner.scan(3)
peer = rows.expect("BleuIO")                 # convenience: local name
peer = rows.expect(address="AA:BB:CC:DD:EE:FF")
peer = rows.expect(manufacturer_id=0x075B, min_rssi=-70)
```

Keep the advertiser's scope open through connection and GATT reads; exiting it stops
advertising. `expect(...)` matches all supplied criteria on one row and returns the first
match. Repeated scan rows remain available for exact evidence.
Manufacturer matching on `ScanResults` requires that metadata in the firmware's discovery
rows. When it is absent, inspect the payload with `scan_target(peer).expect(manufacturer_id=...)`.

`ScanResult` carries a typed `BleAddress`, preserving public/random address type and
classifying random-static, resolvable-private (RPA), non-resolvable-private and reserved
random forms. A `ScanResult` can be passed directly to `connected(...)` or `scan_target(...)`.
`address_mode()` / `set_address_mode(...)` expose BleuIO's local `AT+GAPADDRTYPE` setting.

Built-in codec identifiers are `StrEnum` values whose **values match the packaged YAML
identifiers exactly**. CI/tests enforce the mapping:

```python
from autospan_ble import BleCodec, BleGatt, codec

assert BleGatt.DEVICE_NAME.value == BleCodec.DEVICE_NAME.value
name_codec = codec(BleCodec.DEVICE_NAME)
```

Normal code should prefer the higher-level GATT helper:

```python
with scanner.connected(peer):
    value = scanner.gatt(BleGatt.DEVICE_NAME)
    value.expect(name="BleuIO")

print(value.raw)      # b"BleuIO"
print(value.decoded)  # {"name": "BleuIO"}
```

Raw and project-owned paths remain available. A project may define any StructWire layout
and pass its compiled codec while the BLE layer continues to own the transport:

```python
from structwire import Field, Layout

telemetry = Layout(
    Field("version", "u8"),
    Field("temperature_c", "i16le", scale=0.01),
).compile()

with scanner.connected(peer):
    value = scanner.gatt(0x0032, codec=telemetry)
    value.expect(version=1, temperature_c=23.5)

# Full escape hatch:
raw = value.raw
```

For a test suite, compile reusable layouts once in `conftest.py`. This runnable
example decodes the demo's six-byte name with a project-owned field name:

```python
import pytest
from structwire import Field, Layout


@pytest.fixture(scope="session")
def name_codec():
    return Layout(Field("device_name", "ascii", length=6)).compile()
```

The test then deals only with the read and its expected value:

```python
def test_name_with_project_codec(connected_scanner, name_codec):
    """Read the peer's name using our own payload definition."""
    value = connected_scanner.gatt(0x0003, codec=name_codec)
    value.expect(device_name="BleuIO")
```

YAML catalogs can be loaded in the same session-scoped fixture; see
[project layouts](PAYLOAD_DECODING.md#structwire-yaml-catalogs). For a larger
project, put repeated handle/codec bindings in ordinary project helpers. Keep
those helpers independent of pytest so scripts and console sessions can reuse them.

`scan_target(...)` returns tuple-like advertising reports. Standard BLE AD framing and
common fields are decoded by this plugin through StructWire. Custom manufacturer/application
bytes stay project-owned:

```python
report = scanner.scan_target(peer, seconds=2).expect(manufacturer_id=0x075B)
for manufacturer in report.data.manufacturer_data(0x075B):
    values = manufacturer.decode(telemetry)
```

The same helpers work from an interactive runtime session; there is no pytest-only BLE API.
Endpoints also provide explicit `set_role(...)`, reset, `advertising()` and `connected(...)`
context managers, bounded event waits, and `device.raw("AT+GAPSTATUS")` as the vendor escape
hatch. `bench.status()` exposes lifecycle state.

## Passive capture

A configured observer records PCAP files separately from the active endpoints.
Use `bench.capture(...)` to keep the capture session and its evidence artifact in
the same scope. This example needs endpoints named `scanner` and `peripheral`, an
observer named `air`, and the hardware gate:

```python
from autospan_ble import BleBench


def test_capture(runtime):
    bench = BleBench.from_runtime(runtime)
    with bench.device("peripheral").advertising():
        with bench.capture("air", runtime.evidence) as capture:
            capture.wait_for_packets(timeout_s=5)
            bench.device("scanner").scan(2)
    assert capture.result is not None
```

`wait_for_packets()` waits for a complete packet and raises `TimeoutError` if
none arrives before the deadline. It does not verify which device sent the
packet. Capture startup checks the file header; it does not require traffic.

After a successful stop, `capture.result` contains the byte count, packet count
and duration. Its path may move when evidence is committed; use the manifest
for the final file location. Repeated `capture.stop()` calls return the completed
result. Capture and cleanup failures propagate to the test.

`bench.observer(name)` exposes the lower-level `BleObserver` interface. Opening
probes readiness; starting capture requires the READY state. A failed observer
must be closed before reopening. AutoSpan normally owns that lifecycle. See
[bench bring-up](BENCH_BRINGUP.md#add-a-passive-observer) for tool and hardware setup.

## Optional report steps

AutoSpan records test outcomes and device events without explicit steps. When a
test benefits from named phases in its report, add this fixture to `conftest.py`:

```python
@pytest.fixture
def step(runtime):
    return runtime.evidence.step
```

Then label the meaningful operation:

```python
def test_device_name_with_report(connected_scanner, step):
    """Check the peer's name and give that check a label in the report."""
    with step("Read device name"):
        connected_scanner.gatt(BleGatt.DEVICE_NAME).expect(name="BleuIO")
```

For an operation whose cleanup belongs to the same phase, combine context managers:
`with step("Connect and check name"), scanner.connected(peer):`.
The connection exits before the step, so disconnect errors still fail that step.
Setup in `connected_scanner` happens before the test's step; label it in the fixture
if you also need setup phases in the report.

See [architecture](ARCHITECTURE.md) for lifecycle/timeout behavior and
[payload decoding](PAYLOAD_DECODING.md) for the StructWire boundary.
