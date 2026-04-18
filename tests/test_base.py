from __future__ import annotations

import pytest

from wayfinder.base import (
    WayfinderEvent,
    WayfinderReport,
    WayfinderSpec,
    validate_inputs,
)


def test_event_factory_populates_ts() -> None:
    e = WayfinderEvent.now("progress", url="https://x/", status=200)
    assert e.kind == "progress"
    assert e.data["status"] == 200
    assert e.ts > 0


def test_report_ok_requires_completed_and_no_error() -> None:
    r = WayfinderReport(spawn_id="s", type_name="t", status="completed")
    assert r.ok
    assert not WayfinderReport(spawn_id="s", type_name="t", status="failed").ok
    assert not WayfinderReport(
        spawn_id="s", type_name="t", status="completed", error="x"
    ).ok


def test_validate_inputs_accepts_expected_shape() -> None:
    spec = WayfinderSpec(
        type_name="x",
        description="",
        required_inputs=frozenset({"targets"}),
        allowed_inputs=frozenset({"targets", "policy"}),
    )
    validate_inputs(spec, {"targets": []})
    validate_inputs(spec, {"targets": [], "policy": {}})


def test_validate_inputs_rejects_missing_required() -> None:
    spec = WayfinderSpec(
        type_name="x", description="",
        required_inputs=frozenset({"targets"}),
    )
    with pytest.raises(ValueError, match="missing required"):
        validate_inputs(spec, {})


def test_validate_inputs_rejects_unknown_keys() -> None:
    spec = WayfinderSpec(
        type_name="x", description="",
        required_inputs=frozenset({"a"}),
        allowed_inputs=frozenset({"a"}),
    )
    with pytest.raises(ValueError, match="unknown inputs"):
        validate_inputs(spec, {"a": 1, "b": 2})


def test_validate_inputs_no_allowed_means_anything_goes() -> None:
    spec = WayfinderSpec(
        type_name="x", description="",
        required_inputs=frozenset({"a"}),
        allowed_inputs=frozenset(),       # empty → no whitelist
    )
    validate_inputs(spec, {"a": 1, "extra": 2})   # doesn't raise
