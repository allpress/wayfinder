"""Persistent browser identities.

An identity is a named Chromium `storage_state` blob (cookies, localStorage,
IndexedDB entries) plus metadata. Blobs are encrypted at rest with AES-GCM.

Layout:
    <root>/
      <name>.state.bin      — nonce || ciphertext || tag, AES-GCM
      <name>.meta.json      — plaintext metadata (provider, domains, timestamps)

The key is supplied by the caller. Inside warden it comes from the Keychain
via `warden.paths` / the capability master. In tests we inject a 32-byte key
directly so we can verify both happy paths and tamper detection.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from wayfinder.browser.errors import ErrCode
from wayfinder.browser.models import IdentityInfo


class IdentityError(Exception):
    """Raised for structural / crypto / policy failures. Session converts to ErrCode."""

    def __init__(self, code: ErrCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(slots=True, frozen=True)
class IdentityStore:
    """Filesystem-backed, AES-GCM encrypted store of Chromium storage_state blobs."""

    root: Path
    key: bytes                          # 32 bytes; caller-supplied

    def __post_init__(self) -> None:
        if len(self.key) != 32:
            raise ValueError("IdentityStore key must be 32 bytes (AES-256-GCM)")
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    # -- public API --

    def save(self, name: str, storage_state: dict, *, provider: str | None = None,
             allowed_domains: list[str] | None = None) -> IdentityInfo:
        _validate_name(name)
        blob = json.dumps(storage_state, separators=(",", ":")).encode("utf-8")
        nonce = secrets.token_bytes(12)
        ct = AESGCM(self.key).encrypt(nonce, blob, _aad(name))
        _atomic_write(self._state_path(name), nonce + ct, mode=0o600)

        meta = self._read_meta(name)
        meta.update({
            "provider": provider if provider is not None else meta.get("provider"),
            "allowed_domains": (
                list(allowed_domains) if allowed_domains is not None
                else meta.get("allowed_domains", [])
            ),
            "last_refresh": time.time(),
            "has_storage": True,
        })
        self._write_meta(name, meta)
        return self._info_from_meta(name, meta)

    def load(self, name: str) -> dict:
        _validate_name(name)
        path = self._state_path(name)
        if not path.exists():
            raise IdentityError(ErrCode.secret_unknown, f"no storage for identity {name!r}")
        raw = path.read_bytes()
        if len(raw) < 12 + 16:
            raise IdentityError(ErrCode.playwright_error,
                                f"corrupt identity blob for {name!r}")
        nonce, ct = raw[:12], raw[12:]
        try:
            plain = AESGCM(self.key).decrypt(nonce, ct, _aad(name))
        except InvalidTag as e:
            raise IdentityError(ErrCode.playwright_error,
                                f"identity blob failed AEAD check for {name!r}") from e
        try:
            return json.loads(plain.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise IdentityError(ErrCode.playwright_error,
                                f"identity blob not JSON for {name!r}: {e}") from e

    def has(self, name: str) -> bool:
        _validate_name(name)
        return self._state_path(name).exists()

    def forget(self, name: str) -> bool:
        _validate_name(name)
        existed = False
        for p in (self._state_path(name), self._meta_path(name)):
            if p.exists():
                p.unlink()
                existed = True
        return existed

    def list(self) -> list[IdentityInfo]:
        out: list[IdentityInfo] = []
        for p in sorted(self.root.glob("*.meta.json")):
            name = p.name[: -len(".meta.json")]
            meta = self._read_meta(name)
            out.append(self._info_from_meta(name, meta))
        return out

    def info(self, name: str) -> IdentityInfo:
        _validate_name(name)
        meta = self._read_meta(name)
        return self._info_from_meta(name, meta)

    def update_meta(self, name: str, **changes: object) -> IdentityInfo:
        _validate_name(name)
        meta = self._read_meta(name)
        meta.update(changes)
        self._write_meta(name, meta)
        return self._info_from_meta(name, meta)

    # -- internals --

    def _state_path(self, name: str) -> Path:
        return self.root / f"{name}.state.bin"

    def _meta_path(self, name: str) -> Path:
        return self.root / f"{name}.meta.json"

    def _read_meta(self, name: str) -> dict:
        p = self._meta_path(name)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write_meta(self, name: str, meta: dict) -> None:
        _atomic_write(
            self._meta_path(name),
            json.dumps(meta, indent=2, sort_keys=True).encode("utf-8"),
            mode=0o600,
        )

    def _info_from_meta(self, name: str, meta: dict) -> IdentityInfo:
        return IdentityInfo(
            name=name,
            provider=meta.get("provider"),
            allowed_domains=list(meta.get("allowed_domains", []) or []),
            last_refresh=meta.get("last_refresh"),
            has_storage=self._state_path(name).exists(),
        )


# ---------- helpers ----------

def _atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _validate_name(name: str) -> None:
    if not name or any(ch in name for ch in ("/", "\\", "..", "\x00")) or name.startswith("."):
        raise IdentityError(ErrCode.bad_argument, f"invalid identity name: {name!r}")
    if len(name) > 64:
        raise IdentityError(ErrCode.bad_argument, "identity name too long (>64)")


def _aad(name: str) -> bytes:
    """Associated data bound into the AEAD tag: version + identity name.

    Prevents swapping one identity's ciphertext for another's.
    """
    return b"wayfinder/identity/v1/" + name.encode("utf-8")


__all__ = ["IdentityStore", "IdentityError"]
