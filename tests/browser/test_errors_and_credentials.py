"""Exception classification + credential heuristic."""
from __future__ import annotations

import pytest

from wayfinder.browser import ErrCode, classify_exception, is_credential_shaped
from wayfinder.browser.models import Interactable


def mk(**kw) -> Interactable:
    defaults = dict(handle="h", role="textbox", name="", value=None,
                    label=None, placeholder=None,
                    required=False, disabled=False, checked=None,
                    editable=True, in_form=None, landmark=None,
                    ordinal=0, bbox=None)
    defaults.update(kw)
    return Interactable(**defaults)


@pytest.mark.parametrize("field,val", [
    ("label", "Password"),
    ("placeholder", "password"),
    ("name", "One-time code"),
    ("label", "Enter OTP"),
    ("placeholder", "Card Number"),
    ("name", "CVV"),
    ("label", "API Key"),
    ("placeholder", "api-key"),
    ("label", "Access token"),
    ("name", "Bearer token"),
    ("name", "Social Security"),
    ("placeholder", "Secret"),
    ("label", "MFA"),
    ("name", "2FA code"),
])
def test_credential_detected(field, val):
    assert is_credential_shaped(mk(**{field: val}))


@pytest.mark.parametrize("field,val", [
    ("name", "Email"),
    ("label", "Full name"),
    ("placeholder", "Search"),
    ("name", "Pinboard"),     # must not match bare "pin" substring
    ("label", "Opinion"),     # must not match bare "pin"
])
def test_non_credential_not_flagged(field, val):
    assert not is_credential_shaped(mk(**{field: val}))


def test_password_marker_always_detected():
    el = mk(label="__wf_password__")
    assert is_credential_shaped(el)


def test_classify_timeout_from_pw_timeout_like():
    class TimeoutError(Exception):
        pass
    assert classify_exception(TimeoutError("boom")) == ErrCode.timeout


def test_classify_not_visible_from_message():
    assert classify_exception(RuntimeError("element is not visible now")) == ErrCode.not_visible


def test_classify_disabled_from_message():
    assert classify_exception(RuntimeError("element is disabled at the moment")) == ErrCode.disabled


def test_classify_network_dead():
    assert classify_exception(RuntimeError("net::ERR_NAME_NOT_RESOLVED")) == ErrCode.network_dead


def test_classify_fallback():
    assert classify_exception(RuntimeError("some other problem")) == ErrCode.playwright_error
