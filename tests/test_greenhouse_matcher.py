"""Unit tests for the Greenhouse submitter's label-matching logic.

These are pure-Python tests (no Playwright) that lock in the behaviour of
``_norm_label`` and ``_find_handle_by_field_name`` against the label
shapes Greenhouse actually produces: trailing asterisks, (optional) tags,
humanised field names, and opaque ``question_NNNN`` ids.
"""
from __future__ import annotations

from dataclasses import dataclass

from wayfinder.walkers.greenhouse_submitter import (
    _find_handle_by_field_name,
    _norm_label,
)


@dataclass
class _H:
    """Shaped like ``wayfinder.browser.models.Interactable`` for the fields
    the matcher looks at. Using a plain dataclass keeps this a pure unit test.
    """
    handle: str
    role: str
    name: str = ""
    label: str = ""


# -- _norm_label --

def test_norm_label_strips_trailing_asterisk():
    assert _norm_label("First Name *") == "first name"
    assert _norm_label("First Name*") == "first name"
    assert _norm_label("First Name  *  ") == "first name"
    assert _norm_label("First Name ✱") == "first name"


def test_norm_label_strips_required_marker():
    assert _norm_label("Email (required)") == "email"
    assert _norm_label("Email required") == "email"
    assert _norm_label("Email Required") == "email"


def test_norm_label_strips_optional_marker():
    assert _norm_label("(Optional) Personal Preferences") == "personal preferences"
    assert _norm_label("Website (optional)") == "website"


def test_norm_label_collapses_whitespace():
    assert _norm_label("First    Name") == "first name"
    assert _norm_label("  First\tName  ") == "first name"


def test_norm_label_empty_inputs():
    assert _norm_label("") == ""
    assert _norm_label(None) == ""   # type: ignore[arg-type]


# -- _find_handle_by_field_name --

def _mk_handles(*specs):
    return [_H(*s) for s in specs]


def test_matches_exact_label_with_required_marker():
    """Greenhouse renders required-field labels as 'First Name *'."""
    hs = _mk_handles(
        ("h1", "textbox", "first_name_input", "First Name *"),
    )
    assert _find_handle_by_field_name(hs, "first_name") == "h1"


def test_matches_humanised_field_name_when_no_label_passed():
    hs = _mk_handles(
        ("h1", "textbox", "", "Last Name"),
        ("h2", "textbox", "", "Email"),
    )
    assert _find_handle_by_field_name(hs, "last_name") == "h1"


def test_matches_plan_label_over_field_name():
    """When fieldname is opaque (question_NNNN), plan label is what matters."""
    hs = _mk_handles(
        ("h1", "textbox", "", "Why Anthropic?"),
    )
    assert _find_handle_by_field_name(
        hs, "question_14566142008", label="Why Anthropic?"
    ) == "h1"


def test_matches_label_with_optional_prefix():
    hs = _mk_handles(
        ("h1", "textbox", "", "(Optional) Personal Preferences"),
    )
    assert _find_handle_by_field_name(
        hs, "question_14566136008", label="(Optional) Personal Preferences"
    ) == "h1"


def test_prefers_interactable_role_on_exact_match():
    """A heading that happens to contain 'First Name' must not outrank
    the actual textbox."""
    hs = _mk_handles(
        ("h1", "heading", "", "First Name"),
        ("h2", "textbox", "", "First Name *"),
    )
    assert _find_handle_by_field_name(hs, "first_name") == "h2"


def test_widened_roles_match_radio_and_checkbox():
    hs_radio = _mk_handles(
        ("h1", "radio", "", "Do you require visa sponsorship?"),
    )
    assert _find_handle_by_field_name(
        hs_radio, "question_x", label="Do you require visa sponsorship?"
    ) == "h1"
    hs_checkbox = _mk_handles(
        ("h1", "checkbox", "", "AI Policy for Application"),
    )
    assert _find_handle_by_field_name(
        hs_checkbox, "question_y", label="AI Policy for Application"
    ) == "h1"


def test_name_fallback_catches_file_inputs():
    """File inputs often lack proper labels. The name fallback catches
    them when either field_name or hinted label appears in the name."""
    hs = _mk_handles(
        ("h1", "button", "resume-upload-btn", ""),
    )
    assert _find_handle_by_field_name(hs, "resume", label="resume") == "h1"


def test_returns_none_when_no_match():
    hs = _mk_handles(
        ("h1", "textbox", "", "City"),
    )
    assert _find_handle_by_field_name(hs, "first_name") is None


def test_containment_match_for_longer_labels():
    """A label 'Website URL' should match a field_name of 'website'."""
    hs = _mk_handles(
        ("h1", "textbox", "", "Website URL"),
    )
    assert _find_handle_by_field_name(hs, "website") == "h1"
