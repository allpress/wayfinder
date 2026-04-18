"""Round-trips for the JSON helpers on every public dataclass."""
from __future__ import annotations

from wayfinder.browser import ErrCode
from wayfinder.browser.models import (
    ActResult,
    ExtractResult,
    IdentityInfo,
    Interactable,
    Landmark,
    LoginHint,
    NetEvent,
    OAuthResult,
    Observation,
    ObservationDiff,
    OpenResult,
    SaveResult,
    ScreenshotResult,
    TextBlock,
    from_dict,
    to_dict,
)


def test_interactable_roundtrip():
    i = Interactable(
        handle="h1", role="button", name="OK",
        value=None, label="OK", placeholder="",
        required=True, disabled=False, checked=None,
        editable=False, in_form=None, landmark="main",
        ordinal=0, bbox=(1, 2, 3, 4),
    )
    d = to_dict(i)
    assert d["handle"] == "h1" and d["bbox"] == [1, 2, 3, 4]
    back = from_dict(Interactable, d)
    assert back == i


def test_observation_with_nested_lists_roundtrip():
    obs = Observation(
        url="https://example.test/", title="t",
        handles=[Interactable(handle="h1", role="button", name="Go")],
        landmarks=[Landmark(handle="l1", role="main", name="")],
        text_blocks=[TextBlock(handle="t1", tag="h1", text="hi")],
        console_tail=["[log] hi"],
        network_tail=[NetEvent(ts=1.0, event="request", url="https://example.test/")],
        fingerprint="abcd", truncated=False,
        login_hint=LoginHint(provider="microsoft", reason="x"),
        screenshot_b64=None, snapshot_id="snap1",
    )
    d = to_dict(obs)
    assert d["login_hint"]["provider"] == "microsoft"
    back = from_dict(Observation, d)
    assert back == obs


def test_actresult_roundtrip_with_err():
    a = ActResult(
        ok=False, error=ErrCode.timeout, error_detail="timed out",
        url_before="a", url_after="a", navigated=False, diff=None,
    )
    d = to_dict(a)
    assert d["error"] == "timeout"
    back = from_dict(ActResult, d)
    assert back == a


def test_observationdiff_roundtrip():
    diff = ObservationDiff(
        url_changed=True, title_changed=False,
        url_before="a", url_after="b",
        added_handles=["h1", "h2"], removed_handles=[],
        changed_handles=["h3"], added_text=["t1"],
        new_network=[NetEvent(ts=1.0, event="request", url="u", method="GET")],
        new_console=["[log] x"],
    )
    d = to_dict(diff)
    back = from_dict(ObservationDiff, d)
    assert back == diff


def test_misc_result_roundtrips():
    for cls, obj in [
        (OpenResult, OpenResult(ok=True, session_id="s", identity="i",
                                allowed_domains=["a.test"], headless=True,
                                loaded_storage=False)),
        (SaveResult, SaveResult(ok=True, identity="i", bytes_written=10)),
        (ExtractResult, ExtractResult(ok=True, text="hi", truncated=False)),
        (ScreenshotResult, ScreenshotResult(ok=True, b64="zz", width=10, height=20)),
        (OAuthResult, OAuthResult(ok=True, identity="i", provider="google",
                                  stored_tokens=["secret://c/google/access_token"],
                                  expires_at=1234.5)),
        (IdentityInfo, IdentityInfo(name="i", provider="google",
                                    allowed_domains=["g.test"],
                                    last_refresh=1.0, has_storage=True)),
    ]:
        assert from_dict(cls, to_dict(obj)) == obj
