"""Credential-shape heuristic for fill() safety.

`fill(value_literal=...)` is refused on fields whose role+label+name+placeholder
suggest a secret. Callers must use `value_ref` for those, which is dereferenced
inside warden without exposing the value to the sandbox.

The heuristic is deliberately substring-based and case-insensitive. False
positives are acceptable — the caller always has `value_ref` as an escape hatch.
False negatives are the concern, so the list errs on the side of catching more.
"""
from __future__ import annotations

from wayfinder.browser.models import Interactable

# Each entry is a substring we look for in role/label/name/placeholder/value.
# Keep sorted by specificity: narrower terms first so tests can pinpoint matches.
_CREDENTIAL_FRAGMENTS: tuple[str, ...] = (
    "password",
    "passwd",
    "passphrase",
    "pass phrase",
    "pin code",
    " pin",           # leading space to avoid "opinion"; endswith/startswith are below
    "otp",
    "totp",
    "2fa",
    "two-factor",
    "mfa",
    "one-time code",
    "verification code",
    "auth code",
    "secret",
    "api key",
    "api-key",
    "apikey",
    "access token",
    "access-token",
    "bearer token",
    "auth token",
    "card number",
    "credit card",
    "cc number",
    "cvv",
    "cvc",
    "ssn",
    "social security",
)

# Edge terms: match if any haystack field starts with or equals these (to catch
# bare "pin" without false-matching "pinboard").
_CREDENTIAL_EXACT: frozenset[str] = frozenset({"pin", "otp", "cvv", "cvc", "mfa", "2fa"})


def is_credential_shaped(el: Interactable) -> bool:
    """Return True if this interactable looks like a credential input.

    Rule: any of role, name, label, placeholder, value must either
    - contain one of the fragment substrings (case-insensitive), or
    - equal one of the exact terms when split on word boundaries.

    Input `type="password"` is detected via Playwright's default role surfacing
    (the role will come back as `textbox` but the placeholder/label still carry
    the signal); additionally the observer tags native password inputs with the
    magic label ``__wf_password__`` so this check always triggers on them.
    """
    haystacks = [
        el.role or "",
        el.name or "",
        el.label or "",
        el.placeholder or "",
        el.value or "",
    ]
    # Magic marker from the observer when the native type is "password".
    if "__wf_password__" in haystacks:
        return True

    for raw in haystacks:
        lower = raw.lower()
        if not lower:
            continue
        for frag in _CREDENTIAL_FRAGMENTS:
            if frag in lower:
                return True
        for word in _tokenise(lower):
            if word in _CREDENTIAL_EXACT:
                return True
    return False


def _tokenise(s: str) -> list[str]:
    """Split on any non-alphanumeric run. Cheap, no regex import."""
    out: list[str] = []
    cur: list[str] = []
    for ch in s:
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


__all__ = ["is_credential_shaped"]
