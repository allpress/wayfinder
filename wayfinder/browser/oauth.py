"""OAuth / OIDC helpers.

Scope: detection + the refresh-token flow. The interactive login dance itself
runs inside the Session (headful Chromium tab) — this module provides the
pieces that need to be testable and provider-aware without a live browser:

  * ``Provider`` — known-issuer metadata (authorize URL matcher, token URL,
    scope default).
  * ``detect_provider(url)`` — best-effort mapping from a URL to a Provider.
  * ``capture_tokens_from_url(url)`` — pull access_token / id_token /
    refresh_token out of a final-redirect URL (query or fragment).
  * ``refresh_token(provider, refresh_token, client_id, ...)`` — POST to
    the provider's token endpoint; return a normalised ``TokenBundle``.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable


# ---------- Provider registry ----------

@dataclass(frozen=True, slots=True)
class Provider:
    name: str                        # "microsoft" / "google" / "github" / "generic"
    authorize_hosts: tuple[str, ...] # substrings that identify an authorize page
    token_url: str                   # OIDC token endpoint
    default_scopes: tuple[str, ...] = ()


PROVIDERS: dict[str, Provider] = {
    "microsoft": Provider(
        name="microsoft",
        authorize_hosts=("login.microsoftonline.com", "login.live.com"),
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        default_scopes=("openid", "profile", "offline_access"),
    ),
    "google": Provider(
        name="google",
        authorize_hosts=("accounts.google.com",),
        token_url="https://oauth2.googleapis.com/token",
        default_scopes=("openid", "profile", "email"),
    ),
    "github": Provider(
        name="github",
        authorize_hosts=("github.com/login/oauth",),
        token_url="https://github.com/login/oauth/access_token",
        default_scopes=(),
    ),
    "generic": Provider(
        name="generic",
        authorize_hosts=(),
        token_url="",                # must be supplied by caller
    ),
}


def detect_provider(url: str) -> Provider | None:
    lower = (url or "").lower()
    for name, prov in PROVIDERS.items():
        if name == "generic":
            continue
        for hint in prov.authorize_hosts:
            if hint in lower:
                return prov
    return None


# ---------- Token capture from redirect URL ----------

@dataclass(frozen=True, slots=True)
class TokenBundle:
    access_token: str | None = None
    id_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: float | None = None   # epoch seconds
    scope: str = ""
    raw: dict[str, Any] | None = None

    def is_empty(self) -> bool:
        return not (self.access_token or self.id_token or self.refresh_token)


_TOKEN_PARAMS = ("access_token", "id_token", "refresh_token", "expires_in",
                 "token_type", "scope")


def capture_tokens_from_url(url: str, *, now: Callable[[], float] = time.time) -> TokenBundle:
    """Parse an OAuth redirect URL for tokens (fragment- or query-encoded)."""
    if not url:
        return TokenBundle()
    parsed = urllib.parse.urlparse(url)
    # Implicit grant puts tokens in the fragment; code grant puts them in the
    # query only once the CLIENT exchanges the code — we don't see that here,
    # but we can still capture ?access_token=... when a provider puts it there
    # directly (e.g. some SPA-style redirects).
    merged: dict[str, str] = {}
    for src in (parsed.fragment, parsed.query):
        if not src:
            continue
        for k, v in urllib.parse.parse_qsl(src, keep_blank_values=False):
            if k in _TOKEN_PARAMS:
                merged[k] = v
    if not merged:
        return TokenBundle()
    expires_at: float | None = None
    if "expires_in" in merged:
        try:
            expires_at = now() + float(merged["expires_in"])
        except (TypeError, ValueError):
            expires_at = None
    return TokenBundle(
        access_token=merged.get("access_token"),
        id_token=merged.get("id_token"),
        refresh_token=merged.get("refresh_token"),
        token_type=merged.get("token_type", "Bearer") or "Bearer",
        expires_at=expires_at,
        scope=merged.get("scope", ""),
        raw=dict(merged),
    )


# ---------- Refresh ----------

class OAuthError(Exception):
    pass


def refresh_token(
    provider: Provider,
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str | None = None,
    scope: tuple[str, ...] = (),
    http_post: Callable[[str, dict[str, str], dict[str, str]], tuple[int, bytes]] | None = None,
    now: Callable[[], float] = time.time,
) -> TokenBundle:
    """Perform the OIDC ``grant_type=refresh_token`` exchange.

    ``http_post(url, form_fields, headers) -> (status, body_bytes)`` is
    injectable so tests don't need the network. The default implementation
    uses httpx (already a wayfinder dep).
    """
    if not provider.token_url:
        raise OAuthError(f"provider {provider.name!r} has no token_url")

    body: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        body["client_secret"] = client_secret
    if scope:
        body["scope"] = " ".join(scope)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    post = http_post or _default_post
    status, raw = post(provider.token_url, body, headers)
    if status < 200 or status >= 300:
        snippet = raw[:300].decode("utf-8", "replace")
        raise OAuthError(f"refresh failed [{status}]: {snippet}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise OAuthError(f"refresh: non-JSON response from {provider.token_url}: {e}") from e

    if not isinstance(payload, dict):
        raise OAuthError("refresh: response not a JSON object")

    expires_at: float | None = None
    if "expires_in" in payload:
        try:
            expires_at = now() + float(payload["expires_in"])
        except (TypeError, ValueError):
            expires_at = None

    return TokenBundle(
        access_token=payload.get("access_token"),
        id_token=payload.get("id_token"),
        refresh_token=payload.get("refresh_token") or refresh_token,   # many IdPs rotate; some don't
        token_type=payload.get("token_type") or "Bearer",
        expires_at=expires_at,
        scope=payload.get("scope", "") or "",
        raw=payload,
    )


def _default_post(url: str, fields: dict[str, str], headers: dict[str, str]) -> tuple[int, bytes]:
    import httpx
    resp = httpx.post(url, data=fields, headers=headers, timeout=15.0)
    return resp.status_code, resp.content


__all__ = [
    "Provider", "PROVIDERS", "TokenBundle", "OAuthError",
    "detect_provider", "capture_tokens_from_url", "refresh_token",
]
