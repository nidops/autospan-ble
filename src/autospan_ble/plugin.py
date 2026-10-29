"""AutoSpan project plugin for BleuIO endpoints and optional passive observers."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any, TextIO
from threading import Lock
from math import isfinite
from time import monotonic_ns
import json

from autospan.api import (
    AutoSpanPlugin,
    CommandContribution,
    CommandContext,
    ComponentContext,
    ComponentSpec,
    EvidenceContext,
    PlanBuilder,
    PluginDescriptor,
    PluginRegistrar,
    ResourceClaim,
    ScenarioContribution,
)

from autospan_ble.bleuio.device import BleuIODevice, TransportBuilder
from autospan_ble.bleuio.client import EvidenceCallback
from autospan_ble.bleuio.selector import PySerialPortEnumerator, SerialResolver
from autospan_ble.bleuio.transport import PySerialLineTransport
from autospan_ble.config import DeviceConfig, ObserverConfig, parse_config, parse_sniffer_setup
from autospan_ble.checks import check_sniffer
from autospan_ble import __version__
from autospan_ble.facade import BleBench
from autospan_ble.nrf_sniffer import NrfSnifferObserver
from autospan_ble.raytac.setup import SnifferSetup
from autospan_ble.simulation.fake import (
    FakeBleuIOEnvironment,
    FakeLineTransport,
    FakePortEnumerator,
)

TYPE_ID = "ble.bench"
COMPONENT = BleBench.COMPONENT
SETUP_COMPONENT = "ble.sniffer_setup"


def _evidence_callback(
    context: ComponentContext, logical_name: str, protocol_log: TextIO | None = None
) -> EvidenceCallback:
    lock = Lock()

    def emit(kind: str, fields: Mapping[str, Any]) -> None:
        if kind in {"serial_rx", "serial_tx"} and protocol_log is not None:
            # A serial worker cannot infer the test/step that caused unsolicited RX.
            with lock:
                protocol_log.write(
                    json.dumps(
                        {
                            "monotonic_ns": monotonic_ns(),
                            "logical_device": logical_name,
                            "event": kind,
                            **dict(fields),
                        }
                    )
                    + "\n"
                )
            return
        context.evidence.event(
            "bleuio",
            source=f"ble.{logical_name}",
            event=kind,
            logical_device=logical_name,
            **dict(fields),
        )

    return emit


def _build_device(
    context: ComponentContext, config: DeviceConfig, protocol_log: TextIO | None = None
) -> BleuIODevice:
    evidence = _evidence_callback(context, config.name, protocol_log)
    transport_builder: TransportBuilder
    if config.backend == "fake":
        pid = config.selector.product_ids[0] if config.selector.product_ids else 0x6002
        environment = FakeBleuIOEnvironment(
            endpoint=config.selector.path or f"fake://{config.name}",
            vid=config.selector.vid if config.selector.vid is not None else 0x2DCF,
            pid=pid,
            serial_number=config.selector.serial_number,
            location=config.selector.location,
            scan_results=config.fake_scan_results,
            address=config.fake_address,
        )
        resolver = SerialResolver(FakePortEnumerator(environment))
        transport_builder = partial(FakeLineTransport, environment)
    else:
        resolver = SerialResolver(PySerialPortEnumerator())
        transport_builder = partial(
            PySerialLineTransport,
            baud=config.baud,
            max_rx_line_bytes=config.max_rx_line_bytes,
        )
    return BleuIODevice(
        config.name,
        config.selector,
        resolver,
        transport_builder,
        evidence=evidence,
        reset_timeout_s=config.reset_timeout_s,
    )


def _build_observer(config: ObserverConfig) -> NrfSnifferObserver:
    if config.backend not in {"nrf-sniffer", "raytac-nrf-extcap"}:  # validated by config parser
        raise ValueError(f"unsupported observer backend: {config.backend}")
    return NrfSnifferObserver(
        config.name,
        config.selector,
        SerialResolver(PySerialPortEnumerator()),
        extcap_command=config.extcap_command,
        extcap_version=config.extcap_version,
        startup_timeout_s=config.startup_timeout_s,
        stop_timeout_s=config.stop_timeout_s,
    )


class _BleBenchFactory:
    @contextmanager
    def open(
        self,
        context: ComponentContext,
        dependencies: Mapping[str, object],
        config: Mapping[str, Any],
    ) -> Iterator[BleBench]:
        del dependencies
        parsed = parse_config(config)
        with ExitStack() as stack:
            devices = {}
            for device in parsed.devices:
                source = f"ble.{device.name}"
                with context.evidence.bind_context(
                    EvidenceContext(run_id=context.evidence.current_context.run_id, source=source)
                ):
                    artifact = stack.enter_context(
                        context.evidence.reserve_artifact(
                            kind="protocol",
                            source=source,
                            suffix=".jsonl",
                            format="bleuio-lines",
                            media_type="application/x-ndjson",
                            name=f"{device.name}-protocol",
                            attributes={"logical_device": device.name, "correlation": "session"},
                        )
                    )
                log = stack.enter_context(artifact.path.open("w", encoding="utf-8"))
                devices[device.name] = _build_device(context, device, log)
            observers = {observer.name: _build_observer(observer) for observer in parsed.observers}
            bench = BleBench(
                devices, observers, capture_decoder_command=parsed.capture_decoder_command
            )
            context.evidence.event(
                "ble_bench_opening",
                devices=list(bench.names()),
                observers=list(bench.observer_names()),
            )
            body_error: BaseException | None = None
            try:
                bench.open()
                context.evidence.event("ble_bench_ready", status=bench.status())
                yield bench
            except BaseException as exc:
                body_error = exc
                raise
            finally:
                try:
                    bench.close()
                except BaseException as cleanup:
                    if body_error is not None:
                        raise BaseExceptionGroup(
                            "BLE bench operation and cleanup failed", [body_error, cleanup]
                        ) from None
                    raise
                context.evidence.event("ble_bench_closed")


class _SnifferSetupFactory:
    @contextmanager
    def open(
        self,
        context: ComponentContext,
        dependencies: Mapping[str, object],
        config: Mapping[str, Any],
    ) -> Iterator[SnifferSetup]:
        del context, dependencies
        setup = SnifferSetup(parse_sniffer_setup(config), SerialResolver(PySerialPortEnumerator()))
        try:
            yield setup
        finally:
            setup.close()


def _compile(config: Mapping[str, Any], plan: PlanBuilder) -> None:
    if "sniffer_setup" in config:
        setup = parse_sniffer_setup(config)
        plan.add_component(
            ComponentSpec(
                name=SETUP_COMPONENT,
                type_id=SETUP_COMPONENT,
                claims=(ResourceClaim(setup.selector.claim_key),),
                gates=("hardware", "destructive"),
                config=dict(config),
            )
        )
        return
    parsed = parse_config(config)
    plan.add_component(
        ComponentSpec(
            name=COMPONENT,
            type_id=TYPE_ID,
            claims=tuple(ResourceClaim(device.selector.claim_key) for device in parsed.devices)
            + tuple(ResourceClaim(observer.selector.claim_key) for observer in parsed.observers),
            gates=("hardware",) if parsed.uses_hardware else (),
            config=dict(config),
        )
    )


def _status(ctx: CommandContext, args: tuple[str, ...]) -> int:
    del args
    bench = ctx.runtime.require(COMPONENT, BleBench)
    for name, status in bench.status().items():
        ctx.write(f"{name}: {status['kind']} {status['state']} endpoint={status['endpoint']}")
    return 0


def _scan(ctx: CommandContext, args: tuple[str, ...]) -> int:
    if len(args) not in {1, 2}:
        raise ValueError("expected DEVICE and optional SECONDS")
    name = args[0]
    seconds = float(args[1]) if len(args) > 1 else 2.0
    ctx.check_cancelled()
    bench = ctx.runtime.require(COMPONENT, BleBench)
    for row in bench.device(name).scan(seconds=seconds):
        ctx.write(f"{row.address} rssi={row.rssi} index={row.index}")
    return 0


def _capture(ctx: CommandContext, args: tuple[str, ...]) -> int:
    if len(args) not in {1, 2}:
        raise ValueError("expected OBSERVER and optional SECONDS")
    seconds = float(args[1]) if len(args) == 2 else 5.0
    if not isfinite(seconds) or seconds <= 0:
        raise ValueError("capture duration must be finite and > 0")
    ctx.check_cancelled()
    bench = ctx.runtime.require(COMPONENT, BleBench)
    with bench.capture(args[0], ctx.runtime.evidence) as session:
        ctx.wait(seconds)
    assert session.result is not None
    ctx.write(
        f"{args[0]}: {session.result.packet_count} packets, {session.result.byte_count} bytes"
    )
    ctx.check_cancelled()
    return 0


def _reset(ctx: CommandContext, args: tuple[str, ...]) -> int:
    if len(args) != 1:
        raise ValueError("expected DEVICE")
    ctx.check_cancelled()
    bench = ctx.runtime.require(COMPONENT, BleBench)
    device = bench.device(args[0])
    old = device.endpoint
    device.reset()
    ctx.write(f"{args[0]}: {old} -> {device.endpoint} ({device.state.value})")
    return 0


def _sniffer_setup(ctx: CommandContext, args: tuple[str, ...]) -> int:
    if args:
        raise ValueError("takes no arguments")
    ctx.check_cancelled()
    result = ctx.runtime.require(SETUP_COMPONENT, SnifferSetup).prepare(ctx.runtime.evidence)
    ctx.write(
        f"sniffer READY: firmware={result.firmware_version}, programmed={result.programmed}, "
        f"endpoint={result.endpoint}"
    )
    return 0


def _sniffer_check(ctx: CommandContext, args: tuple[str, ...]) -> int:
    if len(args) != 3:
        raise ValueError("expected OBSERVER ADVERTISER SCANNER")
    ctx.check_cancelled()
    result = check_sniffer(ctx.runtime.require(COMPONENT, BleBench), ctx.runtime.evidence, *args)
    ctx.write(
        f"sniffer PASS: {result.advertiser_packets} CRC-valid packets from {result.address} "
        f"({result.packet_count} captured packets)"
    )
    return 0


@dataclass(frozen=True)
class _Plugin(AutoSpanPlugin):
    descriptor: PluginDescriptor = PluginDescriptor(name="ble", version=__version__, api_version=1)

    def register(self, registrar: PluginRegistrar) -> None:
        registrar.component_type(TYPE_ID, _BleBenchFactory())
        registrar.component_type(SETUP_COMPONENT, _SnifferSetupFactory())
        registrar.plan_compiler(_compile)
        registrar.scenarios(ScenarioContribution(package="autospan_ble"))
        registrar.command(CommandContribution(("ble", "status"), _status, "show BLE device states"))
        registrar.command(
            CommandContribution(
                ("ble", "scan"),
                _scan,
                "scan from a logical BLE device",
                usage="DEVICE [SECONDS]",
            )
        )
        registrar.command(
            CommandContribution(
                ("ble", "reset"),
                _reset,
                "reset and rebind a BleuIO device",
                usage="DEVICE",
            )
        )
        registrar.command(
            CommandContribution(
                ("ble", "capture"),
                _capture,
                "capture from a passive observer",
                usage="OBSERVER [SECONDS]",
            )
        )
        registrar.command(
            CommandContribution(
                ("ble", "sniffer", "setup"),
                _sniffer_setup,
                "program/verify configured sniffer application",
            )
        )
        registrar.command(
            CommandContribution(
                ("ble", "sniffer", "check"),
                _sniffer_check,
                "assert a known advertiser in a sniffer capture",
                usage="OBSERVER ADVERTISER SCANNER",
            )
        )


PLUGIN = _Plugin()
