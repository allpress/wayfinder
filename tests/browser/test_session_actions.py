"""End-to-end Session verbs against the fixture site."""
from __future__ import annotations

import json
import urllib.parse

import pytest

from wayfinder.browser import ErrCode


# -- navigation --

def test_goto_and_current_url(session_factory, test_server):
    s = session_factory()
    r = s.goto(test_server.base + "/index.html")
    assert r.ok
    assert r.url_after.endswith("/index.html")
    assert r.navigated is True


def test_scope_violation(session_factory, test_server):
    s = session_factory(allowed_domains=["only-this-host.test"])
    r = s.goto(test_server.base + "/index.html")
    assert not r.ok
    assert r.error == ErrCode.scope_violation


def test_goto_unknown_host_is_network_dead_or_timeout(session_factory):
    s = session_factory(allowed_domains=["does-not-exist.invalid"])
    r = s.goto("http://does-not-exist.invalid/")
    assert not r.ok
    assert r.error in {ErrCode.network_dead, ErrCode.timeout, ErrCode.playwright_error}


def test_back_and_reload(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/index.html").ok
    assert s.goto(test_server.base + "/form.html").ok
    r = s.back()
    assert r.ok
    assert r.url_after.endswith("/index.html")
    assert s.reload().ok


# -- click / fill / submit --

def _find(obs, role, name_contains=None):
    for h in obs.handles:
        if h.role != role:
            continue
        if name_contains is None or name_contains.lower() in h.name.lower():
            return h
    raise AssertionError(f"no handle: role={role} name~={name_contains}")


def test_fill_and_submit_form(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/form.html").ok
    obs = s.observe()
    email = _find(obs, "textbox", "email")
    assert s.fill(email.handle, value_literal="dev@example.test").ok
    name = _find(obs, "textbox", "full name")
    assert s.fill(name.handle, value_literal="Dev User").ok
    # Selecting a dropdown option.
    country = _find(obs, "combobox", "country")
    assert s.select(country.handle, option="UK").ok
    tos = _find(obs, "checkbox")
    assert s.check(tos.handle, state=True).ok
    # Password must use value_ref; literal is refused.
    pwd = [h for h in obs.handles
           if h.role == "textbox" and "__wf_password__" in (h.label or "")][0]
    refused = s.fill(pwd.handle, value_literal="hunter2")
    assert not refused.ok
    assert refused.error == ErrCode.secret_denied

    # value_ref happy path with an injected resolver (warden does this
    # inside the process; locally we pass one explicitly).
    def resolver(ref):
        assert ref == "secret://test/local/password"
        return "hunter2"
    assert s.fill(pwd.handle, value_ref="secret://test/local/password",
                  secret_resolver=resolver).ok

    submit = _find(obs, "button", "Create account")
    r = s.click(submit.handle)
    assert r.ok
    # Server responds with the echoed form.
    obs_after = s.observe()
    assert any(t.text.startswith("Submitted") for t in obs_after.text_blocks) or \
           "Submitted" in obs_after.title


def test_fill_secret_ref_without_resolver_is_bad_argument(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/form.html").ok
    obs = s.observe()
    email = _find(obs, "textbox", "email")
    r = s.fill(email.handle, value_ref="secret://x/y/z")
    assert r.error == ErrCode.bad_argument


def test_fill_both_refuses(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/form.html").ok
    obs = s.observe()
    email = _find(obs, "textbox", "email")
    r = s.fill(email.handle, value_ref="x", value_literal="y")
    assert r.error == ErrCode.bad_argument


def test_fill_neither_refuses(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/form.html").ok
    obs = s.observe()
    email = _find(obs, "textbox", "email")
    r = s.fill(email.handle)
    assert r.error == ErrCode.bad_argument


def test_secret_resolver_missing_raises_secret_unknown(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/form.html").ok
    obs = s.observe()
    email = _find(obs, "textbox", "email")

    def resolver(ref):
        raise LookupError("no such secret")

    r = s.fill(email.handle, value_ref="secret://x/y/z", secret_resolver=resolver)
    assert r.error == ErrCode.secret_unknown


def test_handle_stale_after_navigation(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/form.html").ok
    obs = s.observe()
    email = _find(obs, "textbox", "email")
    # Navigate away; old handle should no longer resolve.
    assert s.goto(test_server.base + "/index.html").ok
    r = s.fill(email.handle, value_literal="x@y")
    # handle_not_found (if we use stale snapshot) or handle_stale (if resolver
    # runs against fresh snapshot that lacks it) — both are acceptable here.
    assert r.error in {ErrCode.handle_not_found, ErrCode.handle_stale}


def test_handle_not_found(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/form.html").ok
    s.observe()
    r = s.click("hNNNN_nonsense")
    assert r.error == ErrCode.handle_not_found


def test_modal_interaction_diff(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/modal.html").ok
    obs_before = s.observe()
    open_btn = _find(obs_before, "button", "Open dialog")
    r = s.click(open_btn.handle)
    assert r.ok
    # After opening, the confirm button becomes visible (in viewport); diff
    # should surface at least one added or changed handle, or new text.
    diff = r.diff
    assert diff is not None
    # Status text "idle" remains; "confirmed" only on confirm click.
    obs_after = s.observe()
    confirm = _find(obs_after, "button", "Confirm")
    r2 = s.click(confirm.handle)
    assert r2.ok
    obs_final = s.observe()
    assert any("confirmed" in t.text for t in obs_final.text_blocks)


def test_dynamic_list_adds_rows(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/dynamic.html").ok
    obs = s.observe()
    add = _find(obs, "button", "Add row")
    assert s.click(add.handle).ok
    assert s.click(add.handle).ok
    obs2 = s.observe()
    count_text = [t.text for t in obs2.text_blocks if "count:" in t.text]
    assert count_text and "count: 2" in count_text[-1]


def test_press_without_handle(session_factory, test_server):
    s = session_factory()
    assert s.goto(test_server.base + "/form.html").ok
    r = s.press(None, key="Tab")
    assert r.ok
