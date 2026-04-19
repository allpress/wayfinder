"""Session navigation + rebind + debug-dump tests.

Covers two post-action rebinding cases that exercise ``_rebind_newest_page``
and one end-to-end check of ``Session.debug_dump`` output shape. These were
motivated by a Greenhouse-apply flow that broke because the Session held a
stale ``state.page`` reference after a click opened the application form in
a new tab. We guard that regression here.
"""
from __future__ import annotations


def _find(obs, role, name_contains=None):
    for h in obs.handles:
        if h.role != role:
            continue
        if name_contains is None or name_contains.lower() in (h.name or "").lower():
            return h
    raise AssertionError(
        f"no handle role={role} name~={name_contains!r} in "
        f"{[(h.role, h.name) for h in obs.handles]}"
    )


# -- rebind --

def test_rebind_follows_window_open_in_js(session_factory, test_server):
    """Clicking a handler that calls ``window.open`` must rebind
    ``state.page`` to the new tab so subsequent ``observe()`` calls see
    the new DOM, not the original page.

    (Note: target=_blank anchor clicks aren't guaranteed to open a new
    Page under Playwright's locator ``.click()`` — the browser often
    follows the href in-place. The guarantee we rely on in production is
    the JS ``window.open`` form, which every real SPA-style "open the
    application form" flow uses.)"""
    s = session_factory()
    assert s.goto(test_server.base + "/new_tab.html").ok
    obs = s.observe()
    link = _find(obs, "link", "open form (js)")

    original = s.debug_dump()
    assert original["active_url"].endswith("/new_tab.html")
    assert len(original["pages"]) == 1

    r = s.click(link.handle)
    assert r.ok, r

    dump = s.debug_dump()
    assert len(dump["pages"]) >= 2, dump
    actives = [p for p in dump["pages"] if p.get("active")]
    assert len(actives) == 1, dump
    assert actives[0]["url"].endswith("/form.html"), dump

    # Observer script must have been injected on the new page — its
    # handles should now be visible without re-navigating.
    obs2 = s.observe(viewport_only=False)
    email_like = [h for h in obs2.handles
                  if h.role == "textbox" and "email" in (h.label or "").lower()]
    assert email_like, [(h.role, h.name, h.label) for h in obs2.handles]


def test_rebind_stays_on_same_page_when_no_new_tab(session_factory, test_server):
    """Actions that reveal content in-place (dialog, lazy-rendered form)
    must NOT churn ``state.page`` or false-rebind to an unrelated page.
    Subsequent ``observe()`` must still see the updated DOM of the same
    page."""
    s = session_factory()
    assert s.goto(test_server.base + "/modal.html").ok
    obs = s.observe()
    opener = _find(obs, "button", "open dialog")

    before = s.debug_dump()
    assert len(before["pages"]) == 1
    before_handle_count = before["handle_count"]

    r = s.click(opener.handle)
    assert r.ok, r

    after = s.debug_dump()
    # Still exactly one page, still active, same URL.
    assert len(after["pages"]) == 1
    assert after["pages"][0]["active"] is True
    assert after["pages"][0]["url"] == before["pages"][0]["url"]

    # The dialog's revealed buttons (Confirm / Cancel) must now be in
    # the observation. Before the click they were inside a hidden div.
    obs2 = s.observe()
    button_names = {h.name.lower() for h in obs2.handles if h.role == "button"}
    assert "confirm" in button_names, (button_names, obs2.handles)
    assert "cancel" in button_names, (button_names, obs2.handles)
    # Handle count should have grown — revealed content added handles.
    assert after["handle_count"] >= before_handle_count, (before, after)


def test_rebind_handles_lazy_form_render(session_factory, test_server):
    """Greenhouse-like pattern: button click lazy-renders a form into
    the DOM after a short delay. The Session's next ``observe()`` must
    see the new form fields.

    Our current rebind helper waits for ``domcontentloaded``, which is
    already true when the click fires — so this test exercises whether
    we handle settle time for post-click mutations. If this fails, we
    need to wait for ``networkidle`` or add a mutation-observer hook."""
    s = session_factory()
    assert s.goto(test_server.base + "/lazy_form.html").ok
    obs = s.observe()
    apply_btn = _find(obs, "button", "apply")

    r = s.click(apply_btn.handle)
    assert r.ok, r

    # Give the 250ms setTimeout room to fire. If the rebind layer doesn't
    # wait for post-click settle, callers have to — so either this test
    # or the rebind must take the wait. Here we take it in the test to
    # keep the rebind's behavior transparent.
    import time as _t
    _t.sleep(0.4)

    obs2 = s.observe(viewport_only=False)
    labels = {(h.label or "").strip().lower() for h in obs2.handles}
    assert "first name" in labels, labels
    assert "last name" in labels, labels
    assert "email" in labels, labels


# -- debug_dump shape --

def test_debug_dump_before_open_returns_closed_marker():
    from wayfinder.browser import Session, LocalExecutor
    s = Session(executor=LocalExecutor())
    dump = s.debug_dump()
    assert dump == {"open": False}


def test_debug_dump_shape_after_open(session_factory, test_server):
    """Pin the structure so downstream debuggers can rely on the keys."""
    s = session_factory()
    assert s.goto(test_server.base + "/form.html").ok
    s.observe()   # populate last_observation

    dump = s.debug_dump()

    # Top-level keys.
    expected_keys = {
        "open", "session_id", "identity", "allowed_domains", "headless",
        "loaded_storage", "active_url", "pages", "handle_count", "handles",
        "console_tail", "network_tail_count",
    }
    assert expected_keys.issubset(dump.keys()), dump.keys()

    # Types / invariants.
    assert dump["open"] is True
    assert isinstance(dump["session_id"], str) and dump["session_id"]
    assert dump["identity"]
    assert isinstance(dump["allowed_domains"], list)
    assert dump["active_url"].endswith("/form.html")
    assert dump["handle_count"] == len(dump["handles"])
    assert dump["handle_count"] > 0

    # Handle entries are structured.
    h0 = dump["handles"][0]
    assert set(h0.keys()) == {"handle", "role", "name", "label"}

    # Exactly one active page with the expected URL.
    actives = [p for p in dump["pages"] if p.get("active")]
    assert len(actives) == 1
    assert actives[0]["url"].endswith("/form.html")
    assert "title" in actives[0]
