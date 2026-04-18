"""wait_for, extract_text/attribute, screenshot, recent_requests."""
from __future__ import annotations

import base64

import pytest

from wayfinder.browser import ErrCode


def _find(obs, role, name_contains=None):
    for h in obs.handles:
        if h.role != role:
            continue
        if name_contains is None or name_contains.lower() in h.name.lower():
            return h
    raise AssertionError(f"no handle: role={role} name~={name_contains}")


def test_wait_for_text(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/dynamic.html").ok
    r = s.wait_for(text_contains="arrived", timeout_s=5)
    assert r.ok, (r.error, r.error_detail)


def test_wait_for_handle_role(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/modal.html").ok
    obs = s.observe()
    open_btn = _find(obs, "button", "Open dialog")
    # After opening the dialog, wait until a confirm button exists.
    s.click(open_btn.handle)
    r = s.wait_for(handle_role="button", handle_name="Confirm", timeout_s=5)
    assert r.ok


def test_wait_for_url(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/index.html").ok
    # Navigate to form and wait for the URL to reflect it.
    s.goto(test_server.base + "/form.html")
    r = s.wait_for(url_contains="/form.html", timeout_s=5)
    assert r.ok


def test_wait_for_timeout(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/index.html").ok
    r = s.wait_for(text_contains="NEVER_APPEARS_ZZZ", timeout_s=1)
    assert not r.ok
    assert r.error == ErrCode.timeout


def test_wait_for_needs_condition(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/index.html").ok
    r = s.wait_for(timeout_s=1)
    assert r.error == ErrCode.bad_argument


def test_extract_text(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/index.html").ok
    obs = s.observe()
    title = next(t for t in obs.text_blocks if t.tag == "h1")
    # text_block handles aren't valid for interaction; use an interactable.
    link = _find(obs, "link", "Form")
    er = s.extract_text(link.handle)
    assert er.ok
    assert "Form" in er.text


def test_extract_attribute(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/index.html").ok
    obs = s.observe()
    link = _find(obs, "link", "Form")
    er = s.extract_attribute(link.handle, "href")
    assert er.ok
    assert "/form.html" in er.text


def test_screenshot_returns_b64_png(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/index.html").ok
    r = s.screenshot()
    assert r.ok
    assert r.b64
    assert base64.b64decode(r.b64)[:8].startswith(b"\x89PNG")


def test_recent_requests_captures_fixture_host(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/index.html").ok
    evs = s.recent_requests()
    assert any("127.0.0.1" in (e.host or "") for e in evs)
