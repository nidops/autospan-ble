"""Render a readable Markdown step report from a completed AutoSpan evidence run."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from urllib.parse import quote


@dataclass
class Step:
    label: str
    started_ns: int
    depth: int
    source: str
    finished_ns: int | None = None
    outcome: str = "incomplete"
    error: str = ""
    artifact_ids: list[str] = field(default_factory=list)


@dataclass
class Test:
    steps: list[Step] = field(default_factory=list)
    active: list[Step] = field(default_factory=list)
    phases: dict[str, str] = field(default_factory=dict)

    @property
    def outcome(self) -> str:
        if "failed" in self.phases.values() or any(s.outcome == "failed" for s in self.steps):
            return "failed"
        if any(s.outcome == "incomplete" for s in self.steps):
            return "incomplete"
        if "skipped" in self.phases.values():
            return "skipped"
        if self.phases.get("call") == "passed":
            return "passed"
        return "incomplete"


def cell(value: object) -> str:
    """Keep names/errors on one Markdown table row."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "\\|")
        .replace("\n", "<br>")
        .replace("\r", "")
    )


def render_report(directory: Path, *, output: Path | None = None) -> str:
    directory = directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    # A Markdown export is evidence, not a best-effort log viewer. Refuse to
    # summarize mismatched or unfinished runs as a successful qualification.
    if (
        not events
        or not isinstance(manifest, dict)
        or not isinstance(manifest.get("run_id"), str)
        or not manifest["run_id"]
        or not isinstance(manifest.get("outcome"), str)
        or not manifest["outcome"]
        or any(
            not isinstance(event, dict)
            or event.get("run_id") != manifest["run_id"]
            or not isinstance(event.get("attributes"), dict)
            for event in events
        )
        or events[-1].get("name") != "run_finished"
        or events[-1].get("attributes", {}).get("outcome") != manifest["outcome"]
        or sum(event.get("name") == "run_finished" for event in events) != 1
    ):
        raise ValueError("Missing or inconsistent finalized evidence events")
    tests: dict[str, Test] = {}
    for event in events:
        test_id = event.get("test_id")
        if test_id is None:
            continue
        test = tests.setdefault(test_id, Test())
        attributes = event["attributes"]
        if event["name"] == "step_started":
            step = Step(
                attributes["step"], event["monotonic_ns"], len(test.active), event["source"]
            )
            test.steps.append(step)
            test.active.append(step)
        elif event["name"] == "step_finished":
            if not test.active:
                raise ValueError(f"Step finished without a start: {test_id}: {attributes['step']}")
            step = test.active.pop()
            if (step.label, step.source) != (attributes["step"], event["source"]):
                raise ValueError(f"Mismatched nested step: {test_id}: {attributes['step']}")
            step.finished_ns = event["monotonic_ns"]
            step.outcome = attributes["outcome"]
            step.error = attributes.get("error", "")
        elif event["name"] == "pytest_phase_finished":
            phase, outcome = attributes["phase"], attributes["outcome"]
            if phase in test.phases:
                raise ValueError(f"Duplicate test phase: {test_id}: {phase}")
            if outcome not in {"passed", "failed", "skipped"}:
                raise ValueError(f"Invalid test phase outcome: {test_id}: {outcome}")
            test.phases[phase] = outcome
        elif event["name"] == "artifact_committed":
            # BLE capture commits while its owning step is active. Repeated step
            # labels must not put both captures on both rows.
            for active in reversed(test.active):
                if active.label == event.get("step_id"):
                    active.artifact_ids.append(attributes["artifact_id"])
                    break

    if manifest["outcome"] == "passed" and any(test.active for test in tests.values()):
        raise ValueError("Passed evidence contains unfinished steps")
    if manifest["outcome"] == "passed" and any(
        test.outcome not in {"passed", "skipped"} for test in tests.values()
    ):
        raise ValueError("Passed run contradicts recorded test phases or steps")

    def artifact_link(artifact: dict) -> str:
        path = (directory / artifact["path"]).resolve()
        if not path.is_relative_to(directory):
            raise ValueError(f"Artifact escapes evidence run: {artifact['path']}")
        relative = os.path.relpath(path, output.resolve().parent if output else directory)
        relative = Path(relative).as_posix()
        state = "complete" if artifact["complete"] else "incomplete"
        return f"[{cell(path.name)}]({quote(relative, safe='/')}) ({state}, {artifact['size']} B)"

    artifacts = manifest["artifacts"]
    counts = Counter(test.outcome for test in tests.values())
    lines = [
        "# BLE step report",
        "",
        f"Run: {cell(manifest['run_id'])}. Final verdict: **{cell(manifest['outcome']).upper()}**.",
        "",
        ", ".join(f"{count} {outcome}" for outcome, count in sorted(counts.items())) + ".",
        "",
        "Steps include setup and teardown. Indented rows are nested; parent durations include "
        "their children. A skipped test may have no steps because its gate prevented acquisition.",
    ]
    for test_id, test in tests.items():
        lines.extend(["", f"## {cell(test_id)}", "", f"Result: **{test.outcome.upper()}**.", ""])
        if not test.steps:
            lines.append("No steps recorded.")
        else:
            lines.extend(
                [
                    "| Step | Result | Seconds | Error | Artifacts |",
                    "| --- | --- | ---: | --- | --- |",
                ]
            )
        for step in test.steps:
            duration = (
                f"{(step.finished_ns - step.started_ns) / 1e9:.3f}"
                if step.finished_ns is not None
                else "—"
            )
            links = [
                artifact_link(artifact)
                for artifact in artifacts
                if artifact["artifact_id"] in step.artifact_ids
            ]
            label = "↳ " * step.depth + cell(step.label)
            lines.append(
                f"| {label} | {cell(step.outcome).upper()} | {duration} | "
                f"{cell(step.error)} | {'<br>'.join(links)} |"
            )
        assigned = {artifact_id for step in test.steps for artifact_id in step.artifact_ids}
        unscoped = [
            artifact_link(artifact)
            for artifact in artifacts
            if artifact["context"].get("test_id") == test_id
            and artifact["artifact_id"] not in assigned
        ]
        if unscoped:
            lines.extend(["", "Test artifacts: " + "; ".join(unscoped)])
    session_artifacts = [
        artifact_link(artifact)
        for artifact in artifacts
        if artifact["context"].get("test_id") is None
    ]
    if session_artifacts:
        lines.extend(
            [
                "",
                "## Session artifacts",
                "",
                "Raw asynchronous serial RX is session-scoped and has no causal test/step attribution.",
                "",
            ]
        )
        lines.extend(f"- {link}" for link in session_artifacts)
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path, help="Directory containing manifest.json")
    parser.add_argument("--output", type=Path, help="Write Markdown to this file; default stdout")
    args = parser.parse_args()
    report = render_report(args.run_directory, output=args.output)
    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(args.output)
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
