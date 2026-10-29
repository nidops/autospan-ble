# BLE records, advertising data and StructWire payloads

StructWire is the **required shared binary codec engine** for `autospan-ble`.

```text
BleuIO JSON/hex -> autospan-ble routing and framing -> bytes
                                                     |
                                           StructWire codecs
                                             /           \
                                bundled common YAML    project YAML
                                                     |
                                                named values
```

The plugin owns Bluetooth routing, vendor JSON, text syntax and variable-length packet framing.
StructWire owns fixed byte layouts, endianness, signed values, bit fields and application scaling.
Common BLE definitions belong to the plugin and ship as YAML; projects can add their own YAML
without duplicating the binary codec engine. StructWire remains independently installable and
has no dependency back on BLE or AutoSpan.

## What the BleuIO layer decodes

BleuIO verbose mode emits JSON records. `AT+GAPSCAN` uses `S` records with fields such as RSSI,
address and optional name/manufacturer ID. `AT+SCANTARGET` and `AT+FINDSCANDATA` use `ST`/`SF`
records containing raw advertising or scan-response data as hex. `AT+GATTCREAD` completes later
with the characteristic value as ASCII/hex.

The plugin therefore exposes:

- `ScanResult` for typed discovery rows.
- `BleAddress` for `[0]` public and `[1]` random/private addresses.
- `BleAddress.kind` for public, random-static, RPA, non-resolvable private, or reserved-random
  classification.
- `BleuIOAddressMode` for the five local modes returned by `AT+GAPADDRTYPE`.
- `AdvertisingReport` for `AT+SCANTARGET` advertising/scan-response rows.
- `AdvertisingData` / `AdStructure` for standard Bluetooth length/type/value AD structures.
- `ManufacturerData` for AD type `0xFF`, with the two-byte Company Identifier separated from the
  company-specific payload.
- `read_gatt(handle) -> bytes` for raw application data.

The Bluetooth Core specification defines the random-address subtype in the two most-significant
address bits. The plugin uses those bits to distinguish random-static, RPA and non-resolvable
private addresses after BleuIO reports an address as type `[1]`.

This classification reads the address bits; it does not resolve an RPA to an identity or verify
its cryptographic hash. Addresses with the same six octets and different types are distinct peers.

## Typed scan addresses

`ScanResult.address` returns `BleAddress`. Compare its normalized octets with
`row.address.value == mac.upper()`. Use `row.address.bleuio` when logging or
serializing the prefixed string. A `ScanResult` itself,
or its `row.address`, can be passed directly to `connected()` or `scan_target()` to preserve type.
Bare strings still default to public addresses in connection/target APIs; an explicit
`address_type` must agree with a typed address or `[0]`/`[1]` prefix.

## Discovery and address types

```python
rows = scanner.scan(3)
peer = rows.expect(address="12:34:56:78:9A:BC")

for row in rows:
    print(row.address.value)
    print(row.address.address_type)
    print(row.address.kind)
    print(row.rssi)
    print(row.name)
    print(row.manufacturer_id)
```

Discovery metadata is optional. The qualified Standard firmware can omit manufacturer IDs
even while transmitting manufacturer AD structures. `ScanResults.expect(manufacturer_id=...)`
matches only reported metadata; `scan_target(peer).expect(manufacturer_id=...)` also inspects
the received AD bytes. All criteria apply to one row, and the first matching row is returned.

`row.address.bleuio` returns the exact target form accepted by BleuIO, for example:

```text
[0]12:34:56:78:9A:BC
[1]4C:C1:EE:7B:71:96
```

A typed scan address can be passed directly to connection APIs, so a private/RPA peer does not lose
its address type between scan and connect:

```python
peer = rows.expect(address="12:34:56:78:9A:BC")

with scanner.connected(peer):
    value = scanner.read_gatt(0x0032)
```

Do not assume `device.mac_address()` identifies an RPA visible on air. `AT+GETMAC` reports the
BleuIO device MAC/identity, while private non-static modes can rotate their on-air address. Use the
address from the scan result when connecting to an observed random/private peer.

The configured local mode is available separately:

```python
from autospan_ble import BleuIOAddressMode

assert advertiser.address_mode() is BleuIOAddressMode.PUBLIC_STATIC
advertiser.set_address_mode(BleuIOAddressMode.PRIVATE_RANDOM_RESOLVABLE)
```

Changing address mode is a real radio configuration operation and is subject to BleuIO's normal
restrictions while connected or advertising. BleuIO also documents that private non-resolvable mode
(code 4) cannot initiate connections and can advertise only non-connectably. Non-static local
addresses rotate, so tests should use the typed address observed by the peer rather than treating
`AT+GETMAC` as the current on-air address.

## Read complete advertising data

`AT+GAPSCAN` is intentionally a lightweight discovery operation. To inspect the actual advertising
or scan-response bytes of a discovered peer, use `scan_target()`:

```python
peer = scanner.scan(3).expect(address="12:34:56:78:9A:BC")
reports = scanner.scan_target(peer, seconds=2)

for report in reports:
    print(report.address.kind)
    print(report.is_advertisement)
    print(report.is_scan_response)
    print(report.payload.hex())

    for item in report.data.structures:
        print(hex(item.ad_type), item.data.hex())
```

BleuIO's documented `AT+SCANTARGET` examples use data type `0` for advertising and `4` for scan
response; the raw numeric `data_type` is retained so unknown/new values are never silently lost.
`is_advertisement` covers legacy advertising types 0 through 3; `is_scan_response` covers type 4.

`AdvertisingData.parse()` understands the standard Bluetooth AD structure framing:

```text
[length][AD type][AD data] [length][AD type][AD data] ...
```

A zero length terminates padded legacy data. Malformed declared lengths are rejected instead of
being guessed around.

Scans serialize their whole operation and retain unrelated asynchronous records. A timeout or
record-parsing failure interrupts the radio scan with Ctrl+C and waits up to three seconds for
the indexed completion event. If stopping also fails, both errors are reported and an unconfirmed
stop invalidates the command session. Reopen the device before retrying. The scan duration is
rounded to at least one whole second, matching the vendor command; `timeout_s` controls the wait
after command acceptance, with the bounded stop attempt following a failed wait.

## Common definitions included with the package

Installing `autospan-ble` automatically installs `structwire>=1.0.0,<2.0.0`; no extra or separate plugin
activation is needed. Pip resolves the dependencies when installing
this checkout, as shown in the README.

The wheel includes `bleuio/common-codecs.yaml`, compiled once into the immutable `BleCodec`/`codec(...)`
catalog. It defines AD headers, company IDs, first-octet Flags, TX power, 16/32/128-bit service UUID
entries and the qualified BleuIO Standard built-in Device Name. The driver uses these layouts;
it does not merely declare an unused dependency.

```python
from autospan_ble import BleCodec, BleGatt, codec

for report in scanner.scan_target(peer, seconds=2):
    print(report.data.flags)
    print(report.data.tx_power_dbm)
    print(report.data.service_uuids)
    print(report.data.local_name)

assert BleGatt.DEVICE_NAME.value == BleCodec.DEVICE_NAME.value

with scanner.connected(peer):
    scanner.gatt(BleGatt.DEVICE_NAME).expect(name="BleuIO")
```

Flags expose named booleans plus `unassigned_bits` for the first octet, including bit 4's formerly
used position. Additional octets stay in `AdStructure.data`. Absent Flags or TX power return
`None`, meaning unknown; an empty Flags value means all known flags are false. TX power rejects
malformed lengths and -128, outside the specified -127..127 dBm range. Common advertising helpers
interpret the first Flags/TX power structure if duplicated; all original structures remain available.
Service UUID lists use little-endian entries, return canonical UUID strings, and reject partial
entries. `AdvertisingData.service_uuids` merges lists in first-seen order without duplicates;
inspect individual structures to retain complete/incomplete-list AD types and original duplicates.

The `bleuio_device_name` layout is explicitly the qualified six-byte ASCII value. Arbitrary GATT
Device Name characteristics can contain variable-length UTF-8; this layout is not a universal
GATT text decoder. Local advertising names continue to use Python's UTF-8 decoding. Service or
characteristic selection remains explicit; there is no automatic GATT UUID discovery or hidden
codec registry.

## Project-defined application payloads

A StructWire `Codec` satisfies the small `PayloadDecoder` protocol directly. Projects load and
select their own layouts while using the same engine as the common definitions. Raw bytes remain
available for codec-specific options and evidence. Decoder exceptions propagate unchanged;
an enclosing `connected()` scope still releases its owned connection after a schema mismatch.

### GATT example

```python
from structwire import Field, Layout

telemetry = Layout(
    Field("version", "u8"),
    Field("sensor_id", "u16le"),
    Field("temperature_c", "i16le", scale=0.01),
    Field("battery_pct", "u8"),
).compile()

with scanner.connected(peer):
    value = scanner.gatt(0x0032, codec=telemetry)
    value.expect(version=1)

assert value.raw
```

This is equivalent to the more explicit form:

```python
with scanner.connected(peer):
    raw = scanner.read_gatt(0x0032)

values = telemetry.decode(raw)
```

The explicit form remains available because raw bytes are part of the BLE API contract.

### Manufacturer-specific advertising data

Bluetooth Manufacturer Specific Data (AD type `0xFF`) begins with the assigned two-byte Company
Identifier. `autospan-ble` strips only that standard envelope and leaves the remaining bytes to the
project codec:

```python
reports = scanner.scan_target(peer, seconds=2)

for report in reports:
    for vendor in report.data.manufacturer_data(MY_ASSIGNED_COMPANY_ID):
        values = vendor.decode(telemetry)
        assert values["sensor_id"] == 42
```

Use the real Bluetooth SIG Company Identifier assigned to the payload owner. The plugin does not
invent or reserve IDs for application projects.

## StructWire YAML catalogs

Routing belongs to the project, while StructWire YAML contains only payload layouts. For example:

```yaml
version: 1
codecs:
  sensor_telemetry:
    size: 6
    fields:
      - {name: version, type: u8}
      - {name: sensor_id, type: u16le}
      - {name: temperature_c, type: i16le, scale: 0.01}
      - {name: battery_pct, type: u8}
```

Project code decides that a GATT handle or Company Identifier uses that codec:

```python
from structwire import catalog_from_yaml

catalog = catalog_from_yaml("codecs.yaml")
telemetry = catalog["sensor_telemetry"]

with scanner.connected(peer):
    values = scanner.read_gatt_decoded(0x0032, telemetry)
```

For manufacturer data:

```python
for report in scanner.scan_target(peer, seconds=2):
    for vendor in report.data.manufacturer_data(MY_ASSIGNED_COMPANY_ID):
        values = vendor.decode(telemetry)
```

Keep routing selectors (GATT handles, peer addresses, company-ID filters and AT commands) in the
BLE/project layer. A company ID that is actually encoded in an AD prefix belongs in its binary
layout; a company ID used to select a project codec is routing. The same distinction applies
to other protocol selectors. StructWire schemas describe bytes, while the caller selects a schema.

## Advertising custom bytes

`advertising()` accepts bytes as well as BleuIO's raw string form, so a project does not need to
manually build colon-separated AT-command text:

```python
from autospan_ble import AdvertisingData, ManufacturerData

payload = telemetry.encode(
    {
        "version": 1,
        "sensor_id": 42,
        "temperature_c": 23.75,
        "battery_pct": 91,
    }
)

# Use your assigned Company Identifier here.
ad = AdvertisingData.build(
    ManufacturerData(MY_ASSIGNED_COMPANY_ID, payload).to_ad_structure()
)

with advertiser.advertising(adv_data=ad.raw):
    ...
```

The driver converts bytes to the BleuIO `AT+ADVDATA` representation. The test never manipulates the
AT command itself.

Binary input must contain 1–28 bytes of custom legacy advertising data. Standard
firmware prepends a three-byte Flags AD structure and truncates custom data above 28 bytes
on air, even when the vendor command accepts it. The binary API rejects that range before
changing radio state. The raw string form remains a vendor escape hatch;
qualify its on-air bytes with `scan_target()` for the firmware you use.
`AdvertisingData.build()` itself is a framing helper and is not limited to a 31-byte radio packet.

## Upstream references

- BleuIO command reference: <https://www.bleuio.com/getting_started/docs/commands/>
- BleuIO verbose-mode records: <https://www.bleuio.com/getting_started/docs/verbose_mode/>
- Bluetooth LE random-address rules: <https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core-62/out/en/low-energy-controller/link-layer-specification.html>
- Bluetooth advertising data types: <https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/CSS_v14/out/en/core-supplementary-features/data-types-specification.html>
- Bluetooth Assigned Numbers: <https://www.bluetooth.com/specifications/assigned-numbers/>

## Enum-backed project identifiers

For project schemas, keep the YAML identifier as the contract and mirror it with a small
`StrEnum` in project code. This keeps configuration readable while giving tests autocomplete and
static names instead of repeated string literals:

```yaml
version: 1
codecs:
  sensor_telemetry:
    size: 6
    fields:
      - {name: version, type: u8}
      - {name: sensor_id, type: u16le}
      - {name: temperature_c, type: i16le, scale: 0.01}
      - {name: battery_pct, type: u8}
```

```python
from enum import StrEnum
from structwire import catalog_from_yaml


class PayloadId(StrEnum):
    SENSOR_TELEMETRY = "sensor_telemetry"


catalog = catalog_from_yaml("payloads.yaml")
assert {item.value for item in PayloadId} == set(catalog)

telemetry = catalog[PayloadId.SENSOR_TELEMETRY]
with scanner.connected(peer):
    scanner.gatt(0x0032, codec=telemetry).expect(
        version=1,
        sensor_id=42,
    )
```

The enum is deliberately explicit rather than generated dynamically from YAML: IDE completion,
mypy and code navigation remain useful, while one conformance test prevents code/database drift.
