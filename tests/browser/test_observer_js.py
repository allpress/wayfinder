"""Tests the injected JS observer against real HTML fixtures in Chromium.

These tests rely on the session-scoped ``executor`` fixture (LocalExecutor)
running a real Chromium headless.
"""
from __future__ import annotations

import pytest

from wayfinder.browser.observer import (
    load_observer_script,
    parse_snapshot,
)


@pytest.fixture
def goto_and_snapshot(session_factory, test_server):
    def _f(path: str, *, viewport_only: bool = True):
        s = session_factory()
        r = s.goto(test_server.base + path)
        assert r.ok, (r.error, r.error_detail)
        obs = s.observe(viewport_only=viewport_only)
        return s, obs
    return _f


def test_form_fixture_has_expected_interactables(goto_and_snapshot):
    s, obs = goto_and_snapshot("/form.html")
    roles = {h.role for h in obs.handles}
    assert "button" in roles
    assert "textbox" in roles
    assert "combobox" in roles
    assert "checkbox" in roles
    assert "form" in roles
    # Button name resolves from the button text.
    submits = [h for h in obs.handles if h.role == "button" and "Create account" in h.name]
    assert submits, "did not find submit button by accessible name"


def test_login_fixture_is_detected_as_login_wall(goto_and_snapshot):
    # URL-based detection wouldn't fire for the fixture server, so this
    # exercises the structural fallback: password field + submit button.
    s, obs = goto_and_snapshot("/login.html")
    assert obs.login_hint is not None
    assert obs.login_hint.provider == "generic"


def test_password_field_carries_marker(goto_and_snapshot):
    s, obs = goto_and_snapshot("/form.html")
    pwd = [h for h in obs.handles
           if h.role == "textbox" and "__wf_password__" in (h.label or "")]
    assert pwd, "native password input should carry __wf_password__ marker"


def test_landmarks_extracted(goto_and_snapshot):
    s, obs = goto_and_snapshot("/index.html")
    roles = {lm.role for lm in obs.landmarks}
    # <header>, <nav>, <main>, <footer>.
    assert {"banner", "navigation", "main", "contentinfo"}.issubset(roles)


def test_text_blocks_include_headings(goto_and_snapshot):
    s, obs = goto_and_snapshot("/form.html")
    tags = [t.tag for t in obs.text_blocks]
    assert "h1" in tags


def test_fingerprint_stable_across_snapshots_on_same_page(goto_and_snapshot):
    s, obs1 = goto_and_snapshot("/form.html")
    obs2 = s.observe()
    assert obs1.fingerprint == obs2.fingerprint


def test_fingerprint_changes_when_dom_changes(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/dynamic.html").ok
    obs_before = s.observe()
    add = [h for h in obs_before.handles if h.role == "button" and "Add row" in h.name][0]
    r = s.click(add.handle)
    assert r.ok
    obs_after = s.observe()
    assert obs_before.fingerprint != obs_after.fingerprint


def test_snapshot_injection_survives_navigation(session_factory, test_server):
    # After navigation, add_init_script re-installs the observer. Confirm
    # we can snapshot twice in a row without reinjecting.
    s = session_factory()
    assert s.goto(test_server.base + "/index.html").ok
    o1 = s.observe()
    assert s.goto(test_server.base + "/form.html").ok
    o2 = s.observe()
    assert o1.url.endswith("/index.html")
    assert o2.url.endswith("/form.html")
    assert len(o2.handles) > 0


def test_truncation_flag_false_for_small_pages(goto_and_snapshot):
    s, obs = goto_and_snapshot("/form.html")
    assert obs.truncated is False


def test_observer_script_loads():
    js = load_observer_script()
    assert "window.__wayfinder__" in js


def test_parse_snapshot_adds_snapshot_id():
    raw = {
        "url": "u", "title": "t",
        "handles": [{"handle": "h1", "role": "button", "name": "OK",
                     "required": False, "disabled": False, "editable": False,
                     "ordinal": 0}],
        "landmarks": [], "text_blocks": [],
        "fingerprint": "ff", "truncated": False,
    }
    obs = parse_snapshot(raw, console_tail=[], network_tail=[])
    assert obs.snapshot_id
    assert len(obs.handles) == 1
