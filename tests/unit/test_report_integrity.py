"""Markdown exports must reject incomplete and mixed-run evidence."""

import importlib.util
import json
from pathlib import Path
import sys

import pytest
from autospan._core.evidence_store import LocalEvidenceStore

REPORTER_PATH = Path(__file__).resolve().parents[2] / "scripts/report_steps.py"
spec = importlib.util.spec_from_file_location("ble_report_integrity", REPORTER_PATH)
assert spec is not None and spec.loader is not None
reporter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = reporter
spec.loader.exec_module(reporter)


def _completed(tmp_path: Path) -> Path:
    store = LocalEvidenceStore(tmp_path)
    with store.scope(test_id="test_complete"):
        store.event("pytest_phase_finished", phase="call", outcome="passed")
    store.close(outcome="passed")
    return store.root


@pytest.mark.parametrize(
    "mutation",
    [
        "mixed",
        "missing_finish",
        "wrong_finish",
        "failed_phase",
        "duplicate_finish",
        "empty",
        "trailing_event",
        "non_object",
        "invalid_attributes",
    ],
)
def test_report_rejects_invalid_final_evidence(tmp_path, mutation):
    root = _completed(tmp_path)
    file = root / "events.jsonl"
    events = [json.loads(row) for row in file.read_text().splitlines()]
    if mutation == "mixed":
        events[0]["run_id"] = "another-run"
    elif mutation == "missing_finish":
        events.pop()
    elif mutation == "wrong_finish":
        events[-1]["attributes"]["outcome"] = "failed"
    elif mutation == "failed_phase":
        for event in events:
            if event["name"] == "pytest_phase_finished":
                event["attributes"]["outcome"] = "failed"
    elif mutation == "duplicate_finish":
        events.append(dict(events[-1]))
    elif mutation == "empty":
        events.clear()
    elif mutation == "trailing_event":
        events.append(dict(events[0]))
    elif mutation == "non_object":
        events[0] = None
    elif mutation == "invalid_attributes":
        events[-1]["attributes"] = None
    file.write_text("".join(json.dumps(event) + "\n" for event in events))
    with pytest.raises(ValueError, match="finalized evidence|contradicts"):
        reporter.render_report(root)


def test_unmodified_completed_evidence_still_renders(tmp_path):
    root = _completed(tmp_path)
    result = reporter.render_report(root)
    assert "Final verdict: **PASSED**" in result
    assert "test_complete" in result


@pytest.mark.parametrize("manifest", [None, {}, {"run_id": "", "outcome": "passed"}])
def test_report_rejects_invalid_manifest(tmp_path, manifest):
    root = _completed(tmp_path)
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="finalized evidence"):
        reporter.render_report(root)


@pytest.mark.parametrize("include_passed", [False, True])
def test_successful_run_can_contain_skipped_tests(tmp_path, include_passed):
    store = LocalEvidenceStore(tmp_path)
    if include_passed:
        with store.scope(test_id="test_complete"):
            store.event("pytest_phase_finished", phase="call", outcome="passed")
    with store.scope(test_id="test_gated"):
        store.event("pytest_phase_finished", phase="setup", outcome="skipped")
        store.event("pytest_phase_finished", phase="teardown", outcome="passed")
    store.close(outcome="passed")
    result = reporter.render_report(store.root)
    assert "Final verdict: **PASSED**" in result
    assert "1 skipped" in result
    assert "test_gated\n\nResult: **SKIPPED**" in result


@pytest.mark.parametrize("unfinished", [False, True])
def test_passed_run_cannot_contain_failed_or_unfinished_steps(tmp_path, unfinished):
    store = LocalEvidenceStore(tmp_path)
    with store.scope(test_id="test_contradiction"):
        if unfinished:
            store.event("step_started", step="Wait for packet")
        else:
            with pytest.raises(AssertionError):
                with store.step("Wait for packet"):
                    raise AssertionError("Missing packet")
        store.event("pytest_phase_finished", phase="call", outcome="passed")
    store.close(outcome="passed")
    with pytest.raises(ValueError, match="unfinished steps|contradicts"):
        reporter.render_report(store.root)


def test_repeated_phase_cannot_hide_a_failure(tmp_path):
    store = LocalEvidenceStore(tmp_path)
    with store.scope(test_id="test_repeated_phase"):
        store.event("pytest_phase_finished", phase="call", outcome="failed")
        store.event("pytest_phase_finished", phase="call", outcome="passed")
    store.close(outcome="passed")
    with pytest.raises(ValueError, match="Duplicate test phase"):
        reporter.render_report(store.root)


def test_unknown_phase_outcome_cannot_hide_behind_a_passing_call(tmp_path):
    store = LocalEvidenceStore(tmp_path)
    with store.scope(test_id="test_unknown_phase"):
        store.event("pytest_phase_finished", phase="call", outcome="passed")
        store.event("pytest_phase_finished", phase="teardown", outcome="unknown")
    store.close(outcome="passed")
    with pytest.raises(ValueError, match="Invalid test phase outcome"):
        reporter.render_report(store.root)
