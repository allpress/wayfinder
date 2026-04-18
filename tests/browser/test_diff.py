"""ObservationDiff correctness."""
from __future__ import annotations

from wayfinder.browser.diff import diff
from wayfinder.browser.models import (
    Interactable,
    NetEvent,
    Observation,
    TextBlock,
)


def mk(handle, **kw) -> Interactable:
    d = dict(role="textbox", name="", value=None, label=None,
             placeholder=None, required=False, disabled=False,
             checked=None, editable=True, in_form=None, landmark=None,
             ordinal=0, bbox=None)
    d.update(kw)
    return Interactable(handle=handle, **d)


def obs(url="u", title="t", handles=(), text_blocks=(), net=(), console=()):
    return Observation(
        url=url, title=title,
        handles=list(handles),
        text_blocks=list(text_blocks),
        network_tail=list(net),
        console_tail=list(console),
    )


def test_diff_none_before_treats_as_addition():
    after = obs(handles=[mk("h1"), mk("h2")])
    d = diff(None, after)
    assert set(d.added_handles) == {"h1", "h2"}
    assert d.url_changed
    assert d.removed_handles == []


def test_diff_none_after_treats_as_removal():
    before = obs(handles=[mk("h1")])
    d = diff(before, None)
    assert d.removed_handles == ["h1"]
    assert d.url_changed


def test_added_removed_changed():
    before = obs(handles=[mk("a", value="1"), mk("b", value="x")])
    after = obs(handles=[mk("a", value="2"), mk("c")])
    d = diff(before, after)
    assert d.added_handles == ["c"]
    assert d.removed_handles == ["b"]
    assert d.changed_handles == ["a"]


def test_bbox_drift_not_a_change():
    before = obs(handles=[mk("a", bbox=(0, 0, 1, 1))])
    after = obs(handles=[mk("a", bbox=(0, 100, 1, 1))])
    d = diff(before, after)
    assert d.changed_handles == []


def test_ordinal_drift_not_a_change():
    before = obs(handles=[mk("a", ordinal=0)])
    after = obs(handles=[mk("a", ordinal=3)])
    d = diff(before, after)
    assert d.changed_handles == []


def test_added_text_blocks():
    before = obs(text_blocks=[TextBlock(handle="t1", tag="p", text="a")])
    after = obs(text_blocks=[
        TextBlock(handle="t1", tag="p", text="a"),
        TextBlock(handle="t2", tag="h1", text="b"),
    ])
    d = diff(before, after)
    assert d.added_text == ["t2"]


def test_new_network_events_picked_up():
    before_net = [NetEvent(ts=1.0, event="request", url="u", method="GET")]
    after_net = before_net + [NetEvent(ts=2.0, event="response", url="u", status=200)]
    before = obs(net=before_net)
    after = obs(net=after_net)
    d = diff(before, after)
    assert len(d.new_network) == 1
    assert d.new_network[0].event == "response"


def test_url_title_changes():
    before = obs(url="a", title="t1")
    after = obs(url="b", title="t2")
    d = diff(before, after)
    assert d.url_changed and d.title_changed
    assert d.url_before == "a" and d.url_after == "b"


def test_both_none_empty_diff():
    d = diff(None, None)
    assert not d.url_changed and d.added_handles == []
