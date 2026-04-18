"""OAuth helpers: provider detection, token capture, refresh flow."""
from __future__ import annotations

import json

import pytest

from wayfinder.browser import (
    OAuthError,
    PROVIDERS,
    capture_tokens_from_url,
    detect_provider,
    refresh_token,
)


def test_detect_microsoft():
    p = detect_provider("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?...")
    assert p is not None and p.name == "microsoft"


def test_detect_google():
    p = detect_provider("https://accounts.google.com/o/oauth2/auth?...")
    assert p is not None and p.name == "google"


def test_detect_github():
    p = detect_provider("https://github.com/login/oauth/authorize?...")
    assert p is not None and p.name == "github"


def test_detect_unknown():
    assert detect_provider("https://example.test/") is None


def test_capture_from_fragment():
    url = ("https://app.test/cb#access_token=AT123&id_token=IT456&"
           "token_type=Bearer&expires_in=3600&scope=openid%20profile")
    now = lambda: 1000.0  # noqa: E731
    bundle = capture_tokens_from_url(url, now=now)
    assert bundle.access_token == "AT123"
    assert bundle.id_token == "IT456"
    assert bundle.token_type == "Bearer"
    assert bundle.expires_at == 1000.0 + 3600
    assert "openid" in bundle.scope


def test_capture_from_query():
    url = "https://app.test/cb?access_token=AT123"
    bundle = capture_tokens_from_url(url)
    assert bundle.access_token == "AT123"


def test_capture_empty_when_no_tokens():
    assert capture_tokens_from_url("https://app.test/cb?code=xyz").is_empty()
    assert capture_tokens_from_url("").is_empty()


def test_refresh_success():
    calls = []

    def fake_post(url, fields, headers):
        calls.append((url, fields, headers))
        body = json.dumps({
            "access_token": "new-AT", "id_token": "new-IT",
            "expires_in": 60, "token_type": "Bearer",
            "scope": "openid",
        }).encode("utf-8")
        return 200, body

    prov = PROVIDERS["google"]
    bundle = refresh_token(
        prov, refresh_token="rt-old", client_id="cid",
        http_post=fake_post, now=lambda: 100.0,
        scope=("openid", "profile"),
    )
    assert bundle.access_token == "new-AT"
    assert bundle.expires_at == 160.0
    # Refresh token retained when server didn't send a new one.
    assert bundle.refresh_token == "rt-old"
    _, fields, _ = calls[0]
    assert fields["grant_type"] == "refresh_token"
    assert fields["scope"] == "openid profile"


def test_refresh_rotates_token_when_server_provides():
    def fake_post(url, fields, headers):
        return 200, json.dumps({
            "access_token": "AT",
            "refresh_token": "new-RT",
            "expires_in": 30,
        }).encode("utf-8")

    bundle = refresh_token(PROVIDERS["google"], refresh_token="old-RT",
                           client_id="cid", http_post=fake_post,
                           now=lambda: 0.0)
    assert bundle.refresh_token == "new-RT"


def test_refresh_non_200_raises():
    def fake_post(url, fields, headers):
        return 400, b'{"error": "invalid_grant"}'

    with pytest.raises(OAuthError) as exc:
        refresh_token(PROVIDERS["google"], refresh_token="x",
                      client_id="c", http_post=fake_post)
    assert "400" in str(exc.value)


def test_refresh_non_json_raises():
    def fake_post(url, fields, headers):
        return 200, b"not json"

    with pytest.raises(OAuthError):
        refresh_token(PROVIDERS["google"], refresh_token="x",
                      client_id="c", http_post=fake_post)


def test_refresh_generic_needs_token_url():
    with pytest.raises(OAuthError):
        refresh_token(PROVIDERS["generic"], refresh_token="x", client_id="c")
