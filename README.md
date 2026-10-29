# AutoSpan BLE

An AutoSpan plugin for BleuIO endpoint tests and optional Nordic nRF Sniffer capture.
AutoSpan owns resource acquisition, gates and evidence; the plugin owns BLE operations
and stable USB identity. Python 3.11 or newer is required.

## Install

Clone with HTTPS, create a virtual environment and install BLE:

```bash
git clone https://github.com/nidops/autospan-ble.git
cd autospan-ble
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[test]'
```

On Windows, create the environment with `python -m venv .venv`, then activate
with `.venv\Scripts\Activate.ps1` in PowerShell.
Pip installs AutoSpan and StructWire from PyPI, each at version `>=1.0.0,<2.0.0`. The `test`
extra includes pytest, HTML reports and AutoSpan's Textual console. No companion clones are needed.
StructWire supplies the shared binary codec engine. Common BLE
layouts ship in the wheel; project configuration remains outside the package.

## Try the fake bench

Save this as `fake.yaml` in an empty project directory:

```yaml
autospan:
  plugins: [ble]
ble:
  devices:
    scanner:
      backend: fake
      selector: {serial_number: FAKE-SCANNER}
      fake_address: 12:34:56:78:9A:01
      fake_scan_results:
        - {addr: "[0]12:34:56:78:9A:BC", rssi: -48}
    peripheral:
      backend: fake
      selector: {serial_number: FAKE-PERIPHERAL}
      fake_address: 12:34:56:78:9A:BC
      fake_scan_results:
        - {addr: "[0]12:34:56:78:9A:01", rssi: -45}
```

Run the installed scenario without hardware:

```bash
autospan plugins
autospan plan --config fake.yaml
autospan test --config fake.yaml -- -q
autospan console --config fake.yaml --plain --exec "ble scan scanner 1"
```

The scenario reverses scanner/peripheral roles and verifies discovery, connection,
Device Name read and cleanup. Fixtures validate host behavior; they do not establish RF behavior.
The line console needs no TUI extra; add `autospan[tui]` for the interactive Textual console.

## Write a BLE test

AutoSpan collects tests from installed scenario packages. In the directory containing
`fake.yaml`, save this as `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=77", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "ble-checks"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["autospan-ble>=0.1,<0.2"]

[project.entry-points."autospan.plugins"]
ble-checks = "ble_checks:PLUGIN"

[tool.setuptools.packages.find]
include = ["ble_checks*"]
```

Create a `ble_checks` directory and save this as `ble_checks/__init__.py`:

```python
from autospan.api import PluginDescriptor, ScenarioContribution


class Plugin:
    descriptor = PluginDescriptor(name="ble-checks", version="0.1.0", api_version=1)

    def register(self, registrar):
        registrar.scenarios(ScenarioContribution(package="ble_checks", relative_path="."))


PLUGIN = Plugin()
```

This registers your tests; the selected `ble` plugin still provides the bench.
Bind your bench names once in `ble_checks/conftest.py`:

```python
import pytest

from autospan_ble import BleBench


@pytest.fixture
def ble(runtime):
    return BleBench.from_runtime(runtime)


@pytest.fixture
def scanner(ble):
    return ble.device("scanner")


@pytest.fixture
def peripheral(ble):
    return ble.device("peripheral")


@pytest.fixture
def connected_scanner(scanner, peripheral):
    """Find and connect to our peer; disconnect and stop advertising afterward."""
    with peripheral.advertising():
        peer = scanner.scan(1).expect(address=peripheral.mac_address())
        with scanner.connected(peer):
            yield scanner
```

Then write `ble_checks/test_ble.py`:

```python
from autospan_ble import BleGatt


def test_device_name(connected_scanner):
    """Read the peer's name and check that it says BleuIO."""
    connected_scanner.gatt(BleGatt.DEVICE_NAME).expect(name="BleuIO")
```

```bash
python -m pip install -e .
autospan test --config fake.yaml --set 'autospan.plugins=[ble, ble-checks]' -- ble_checks/test_ble.py -q
```

These are ordinary pytest fixtures using the same objects available in Python scripts
and the console. AutoSpan owns device acquisition and teardown. The yielding fixture
keeps advertising and the connection alive through each test and releases both even
if an assertion fails. Choose role names that match your project; no framework naming
convention is required.

For discovery or connection tests, request `scanner` and `peripheral` directly and
write those operations in the test. Load reusable project codecs in a fixture too.
See the [test examples](https://github.com/nidops/autospan-ble/blob/master/docs/API.md#endpoint-api)
for explicit flows, project codecs and optional named report steps. AutoSpan still
records test outcomes and device events without explicit step wrappers.

## Hardware and limits

Numeric device settings use the following formats:

| Setting | Accepted values | Default |
|---|---|---|
| `selector.vid` | USB integer from 0 to 65535, or an integer string such as `"0x2DCF"` | Unset |
| `selector.product_ids` | One USB ID or a list of IDs, using the same format as `vid` | Empty |
| `baud` | Positive integer or decimal integer string | 57600 |
| `reset_timeout_s` | Positive finite number or numeric string | 20 seconds |

Booleans and floating-point values are rejected for integer fields. Timeouts also
reject booleans, NaN and infinity. Observer selectors accept the same USB formats;
firmware maintenance uses the restricted Nordic selector in its separate template.
Observer startup/stop timeouts use the same timeout format and default to 5/3
seconds. Maintenance overall/startup timeouts default to 120/8 seconds. Shipped
physical bench templates override device reset timeouts to 25 seconds and observer
startup to 8 seconds.

The client retains at most 4096 unsolicited events per device, including events
deferred by scans and event waits. Overflow fails the session explicitly. Close
and reopen the device before continuing; in the console, exit and start a new
session. This fixed event-count limit is separate from the serial byte limit below.

Each physical BleuIO device accepts an optional `max_rx_line_bytes` YAML
setting. The default is 65536 bytes (64 KiB), including the newline delimiter;
the maximum configurable value is 1048576 bytes (1 MiB). Omission uses the
default, while invalid values fail validation.

The serial transport bounds each read as well as its accumulated buffer.
Frames exceeding the limit, including unterminated input exceeding it, fail
the session rather than returning truncated protocol data. Close and reopen
the device before further commands. This is a byte limit, not a deadline for
receiving a complete line. The setting belongs to the BLE plugin, not Workspace
Kconfig.

Physical acquisition requires `--hardware`; firmware programming also requires
`--destructive` and a separate maintenance configuration. Store serials and tool paths
in ignored `*.local.yaml` files. See the bench guide for two-dongle and Raytac setup.

The built-in Device Name codec targets the BleuIO Standard six-byte value `b"BleuIO"`.
Other firmware/GATT profiles require their own handles and codecs. Passive capture
requires POSIX FIFOs and external Nordic tools; known-peer RF assertions need TShark.
Connection following, decryption and automatic reconnection are unsupported.

Runs retain TX/RX and capture artifacts in AutoSpan's evidence manifest. Use manifest
paths for committed captures; raw asynchronous RX is session-scoped.

## Documentation and development

The software CI tests installed wheels on Linux, Windows and macOS with Python
3.11 to 3.14. Passive capture and sniffer firmware setup require POSIX; the bench
guide describes the Linux hardware setup.

- [Endpoint and console API](https://github.com/nidops/autospan-ble/blob/master/docs/API.md)
- [Bench bring-up and firmware setup](https://github.com/nidops/autospan-ble/blob/master/docs/BENCH_BRINGUP.md)
- [Payload decoding](https://github.com/nidops/autospan-ble/blob/master/docs/PAYLOAD_DECODING.md)
- [Architecture](https://github.com/nidops/autospan-ble/blob/master/docs/ARCHITECTURE.md)
- [Contributing and verification](https://github.com/nidops/autospan-ble/blob/master/CONTRIBUTING.md)
- [Console and Python helpers](https://github.com/nidops/autospan-ble/blob/master/docs/CONSOLE.md)

From a checkout, install `.[dev]` and follow the contributor guide.
`tests/unit/` contains the plugin's software tests, including fake AutoSpan and
StructWire integration and synthetic capture fixtures. `tests/hardware/` contains
physical bench acceptance; every test carries the `hardware` marker and uses
AutoSpan's runtime and gates. Device names and USB identities come from your config.

```bash
python -m pytest tests/unit
python -m pip install -e tests
autospan test --config raytac.local.yaml --set 'autospan.plugins=[ble, ble-acceptance]' -- --hardware -q -p metadata -p html --html=artifacts/report.html --self-contained-html
```

The separate `tests` package registers checkout acceptance with AutoSpan. The
hardware command also runs the installed BLE scenarios. It qualifies both
endpoint directions, GATT with a project codec, target scans, assertion cleanup,
20 resets per endpoint and 20 optional observer capture windows. A normal
`python -m pytest` run skips physical tests before acquisition. BLE remains a
source-installed integration plugin; local wheels support isolated validation.

With a Workspace catalog pinned to a BLE revision containing
`autospan-workspace.yaml`, the optional `lab` profile prepares both `autospan-ble`
and its `tests/` acceptance distribution in one managed environment. Place your
configured private bench at `bench.local.yaml` in the Workspace root or ordinary
BLE checkout, then run these commands from Workspace:

```bash
make select PLUGIN=ble PROFILE=lab
make bootstrap
make doctor
make hw-list
make hw-list TEST=test_discovery_connection_and_project_gatt_codec
make hw
make hw TEST=test_discovery_connection_and_project_gatt_codec
make console
```

`hw-list` collects without acquiring devices; `hw` shows collection before
execution. `TEST=` also selects ordinary, unmarked scenarios. `CONFIG=` overrides
the profile bench; missing configuration fails before test dispatch. Firmware
maintenance remains separately authorized. The `fake` profile runs software tests
and uses `autospan.fake.yaml`; it cannot supply an implicit hardware bench.
The standalone checkout commands above remain available for development.

Named steps retain their test, outcome, duration and error in the evidence. Render
a readable report after a run with `python scripts/report_steps.py RUN_DIRECTORY
--output RUN_DIRECTORY/steps.md`; see [contributing](https://github.com/nidops/autospan-ble/blob/master/CONTRIBUTING.md)
for the report and artifact contract.
Checkout pytest runs and the hardware command above write `artifacts/report.html`:
a self-contained browser report with results, durations, captured output and failures.
The next run replaces it; use `--html=artifacts/my-run.html` to keep another report.
The workspace's `make hw` also embeds named steps, timings, errors and capture
links in each expandable HTML test row. Use that report as the main view;
Markdown is an optional export of the same evidence for issues or text-based review.
Standalone `autospan test` with the flags above produces the pytest summary.

Security issues should be reported privately; see the
[security policy](https://github.com/nidops/autospan-ble/blob/master/SECURITY.md).

MIT. See [LICENSE](https://github.com/nidops/autospan-ble/blob/master/LICENSE).
