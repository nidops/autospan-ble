# Contributing

## Software and documentation

From this checkout, install BLE and its development tools in an activated virtual
environment, then install the Git hook once per checkout. Pip resolves AutoSpan
and StructWire from PyPI:

```bash
python -m pip install -e '.[dev]'
python -m pre_commit install
```

Format code and run the software checks:

```bash
python -m ruff format src tests scripts
python -m pre_commit run --all-files
python -m pytest tests/unit
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python -m mypy src
python -m compileall -q src tests scripts
python scripts/check_readme.py
```

These `python -m` commands also work in PowerShell. With GNU Make, the formatting
and hook shortcuts are:

```bash
make format
make lint
make pre-commit
make pre-commit-install
```

`make help` lists these commands. They use the active Python environment; override
it with `PYTHON=path/to/python` if needed. Run them from this BLE checkout.

The pre-commit hook runs [Ruff lint fixes followed by formatting](https://docs.astral.sh/ruff/integrations/#pre-commit)
on staged Python files under `src`, `tests`, and `scripts`. If it changes files,
review and stage the changes before committing again. `make pre-commit` runs the
same hooks on all tracked Python files in those directories. CI runs these hooks
alongside the existing type, documentation, and software checks.

Keep Ruff's pinned version in `.pre-commit-config.yaml` aligned with the `dev`
extra in `pyproject.toml`. The first hook run downloads its isolated environment.

The README fake configuration, installed scenario and Python test run in an empty
project directory. Tests also check package catalog/enum agreement, entry-point
loading, hardware/destructive gates before acquisition, rollback and capture cleanup.
No software fixture proves RF behavior.

The software suite lives in `tests/unit/` and carries the `unit` marker. It includes
plugin integration using fake transports and real subprocesses with synthetic PCAP
data. `tests/hardware/` carries the `hardware` marker. Plain pytest skips that folder;
physical acceptance runs through AutoSpan with an explicit configuration and gate.

## Installed distributions

Build into an empty `dist/` directory, then inspect wheel/sdist agreement and run
the complete suite against each isolated installation:

```bash
python -m build
python -m twine check --strict dist/*.whl dist/*.tar.gz
python scripts/check_dist.py dist
python scripts/verify_install.py dist
python scripts/verify_install.py dist --distribution sdist
```

CI installs dependencies from PyPI and runs Python 3.11–3.14 on Linux.
The installation verifier uses a fresh environment, checks that companion imports
come from installed packages rather than local checkouts, and runs the full BLE
suite. The test and development extras include Textual so console tests run too.

The installed smoke checks typing data, codec YAML loading, plugin API 1 discovery
and the fake scenario. Textual is installed for headless console acceptance.
The complete suite runs from the unpacked sdist with `python -I`, importlib mode
and no checkout import path.
The verifier also installs the README's own scenario package with pip, then
installs the separate acceptance distribution from the sdist. It collects the
14 hardware cases with a fake config and confirms they skip without the hardware
gate; neither operation accesses physical devices. Workspace's cross-repository
link check validates its links to this checkout and this guide's Workspace anchor.

## Hardware acceptance

Use [bench bring-up](docs/BENCH_BRINGUP.md) for setup.
Keep measured hardware results separate from fake/subprocess validation. Restore
changed address modes/advertising data; keep private identities and captured artifacts
outside distributions. Passive capture requires POSIX FIFOs and external Nordic tools;
known-advertiser assertions additionally need TShark.

From this checkout, with two configured physical endpoints, install the separate
acceptance package so AutoSpan can discover its scenarios:

```bash
python -m pip install -e tests
autospan test --config ble.local.yaml --set 'autospan.plugins=[ble, ble-acceptance]' -- --hardware -q -p metadata -p html --html=artifacts/report.html --self-contained-html
# Include a configured Nordic observer and TShark for RF acceptance:
autospan test --config raytac.local.yaml --set 'autospan.plugins=[ble, ble-acceptance]' -- --hardware -q -p metadata -p html --html=artifacts/report.html --self-contained-html
```

Selecting `ble-acceptance` includes its fixtures and hardware tests. Append
`-k test_discovery_connection_and_project_gatt_codec` to run the matching test
in both directions, `-x` to stop on failure, or `-s` to show captured output live.
Use `--collect-only` to list the selection without acquiring hardware.
Workspace can install acceptance from the pinned checkout's `tests/` directory
into the same managed environment using the BLE-owned `lab` profile. Pin a
revision containing `autospan-workspace.yaml`, configure `bench.local.yaml` in
the Workspace root or ordinary BLE checkout, then run from Workspace:

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

Do not install acceptance by hand into a managed environment. Select and bootstrap
again when switching between `lab` and `fake`, since their package sets differ.
During software review use collection only for physical scenarios. The standalone
checkout workflow above remains supported; see Workspace's
[generic hardware interface](https://github.com/nidops/autospan-workspace/blob/master/README.md#tests-hardware-and-reports).

These commands also run the installed BLE scenarios. Endpoint names are arbitrary;
the suite reverses the first two configured devices. It verifies real discovery,
connection, raw and codec-decoded Device Name, target-scan bytes, failed-assertion
cleanup, close/reopen and 20 reset/rebind cycles per endpoint. With an observer it
also checks known-peer CRC-valid packets in both directions and 20 capture windows
while endpoint commands remain available. Observer checks skip if none is configured.
Fake endpoints fail hardware acceptance. Tests preserve address modes and advertising
data and never select firmware maintenance. Run firmware setup separately only with
explicit maintenance authorization and both gates.

Retain the evidence manifests, serial logs and PCAP artifacts for the physical run.
Inspect the final verdict, completed artifact hashes, reset departure/recovery events
and teardown; a passing fake run is insufficient. Local hardware reports belong in
ignored `artifacts/` so physical identities and tool paths stay out of source packages.

## HTML reports

The test and development extras include `pytest-html`. Checkout pytest runs and
the hardware commands above write a self-contained `artifacts/report.html`.
Open it in a browser to filter results and inspect durations, captured output and
failure tracebacks. The next run replaces it; pass `--html=artifacts/my-run.html`
to keep a separate report. AutoSpan uses its own pytest configuration, so direct
`autospan test` commands need the explicit HTML flags shown above. Hosted runs
disable plugin autoload; `-p metadata -p html` explicitly loads the report plugins.

In the workspace, `make hw` and `make test CONFIG=...` add finalized evidence to
this HTML automatically: expand a test for named steps, nested timings, errors
and capture links. The final run verdict includes teardown failures. Session-wide
serial logs remain separate from individual tests. Keep the linked evidence tree
with the HTML when sharing files; step details are embedded, logs and PCAPs are not.
Standalone pytest/AutoSpan commands produce the pytest summary without this
workspace enrichment.

## Step reports

Hardware tests and installed scenarios use `with runtime.evidence.step("meaningful phase"):`
for operations and their assertions. Hardware fixture validation and teardown also
have named steps. Lifecycle steps encompass advertising/connection scopes so a
cleanup exception is recorded in that lifecycle's verdict. Nested steps separate
scan, raw GATT, codec decoding, RF readiness, PCAP finalization and CRC checks.
Reset/capture iteration labels include the endpoint/observer and cycle number.

Markdown is an optional export for issue descriptions or text-based review; it is
not needed to inspect the workspace HTML report. After the AutoSpan command
finishes, replace `RUN_ID` with its evidence directory (linked from the HTML):

```bash
python scripts/report_steps.py artifacts/evidence/RUN_ID --output artifacts/evidence/RUN_ID/steps.md
```

This checkout utility renders Markdown with a final run verdict, a per-test verdict,
step results, measured seconds, error details and links to captures. Parent durations
include nested work. It keeps raw asynchronous serial logs under session artifacts;
those logs cannot infer causal test/step attribution. Skipped gated tests remain
visible, and an unfinished step is marked incomplete. The utility reads the committed
manifest and events; it does not acquire hardware or change core evidence.
Both the Markdown export and workspace HTML details read the same finalized events
and manifest. JSON events and manifests remain available for other consumers.

Unit acceptance executes the hardware endpoint functions with fake transports,
including every reset iteration, and checks the actual emitted test/step context.
Report regressions cover passed/failed steps, nested durations, repeated labels,
complete/incomplete captures, skipped/unfinished work and final teardown failures.
A subprocess regression runs an actual failing hosted BLE assertion and renders
its failed verdict and named error. These software cases validate reporting; RF
acceptance still requires the physical bench.

For the intentional assertion-cleanup test, `pytest.raises(...)` sits **inside** the
step. The acceptance check therefore passes when the expected assertion occurs and
both radio scopes clean up. Unexpected body or teardown failures remain failed.

For interactive hardware checks, use the [BLE console](docs/CONSOLE.md). Confirm
that status identifies the intended devices, reset preserves USB identity, and
scan/capture evidence matches the operations. During a long capture, test Ctrl+C
and Ctrl+Q: capture must close before runtime teardown, with no serial handles or
extcap workers left. During synchronous scans, verify the UI remains responsive
and Quit waits for completion. Check input/completion, log search, clipboard and
UTF-8 export on each supported terminal; passive capture requires POSIX.
Missing hardware/maintenance gates must reject acquisition before enumeration.

## Regression coverage

The unit suite covers stable selectors, ambiguity and duplicate physical claims;
typed public/random addresses; fragmented serial replies and command correlation;
timeout invalidation; independent command lanes; and reset departure/rebind.
Radio scopes verify advertising and connection cleanup, including failed assertions,
late state changes, target readiness and failed disconnects. Accepted AT commands
alone do not establish a successful connection or GATT read.

Payload cases cover manufacturer data, project codecs, malformed bytes, UUID byte
order and bundled YAML resources. Custom advertising data reserves three bytes
for firmware Flags and rejects oversized values before changing the device role.

Observer tests exercise real subprocesses and FIFOs with synthetic PCAP records:
startup rollback, delayed packets, truncated output, stderr pressure, process
failures, cancellation and complete/incomplete artifacts. These fixtures verify
lifecycle behavior; known-advertiser CRC checks require the physical bench.
Connection following and decryption are unsupported.

Firmware-setup tests verify hashes and manifests, exact device selection, port
changes, both authorization gates, vendor failures and evidence without flashing
physical hardware. Do not enable firmware maintenance during ordinary qualification.

Public examples must contain redistributable schemas and placeholder selectors.
Keep private customer payloads, credentials, captures and serial identities in
ignored local configuration and artifacts.
