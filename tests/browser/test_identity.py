"""Identity store: roundtrip, tamper detection, atomicity, metadata."""
from __future__ import annotations

import json
import os

import pytest

from wayfinder.browser import ErrCode, IdentityError, IdentityStore


@pytest.fixture
def store(tmp_path):
    return IdentityStore(root=tmp_path / "idents", key=b"K" * 32)


def test_roundtrip_cookies(store):
    state = {"cookies": [{"name": "sid", "value": "abc", "domain": ".example.test",
                          "path": "/", "expires": -1, "httpOnly": False,
                          "secure": False, "sameSite": "Lax"}],
             "origins": []}
    store.save("alice", state, provider="microsoft", allowed_domains=["example.test"])
    loaded = store.load("alice")
    assert loaded == state
    info = store.info("alice")
    assert info.provider == "microsoft"
    assert info.allowed_domains == ["example.test"]
    assert info.has_storage


def test_list_and_forget(store):
    store.save("a", {"cookies": [], "origins": []})
    store.save("b", {"cookies": [], "origins": []})
    assert {i.name for i in store.list()} == {"a", "b"}
    assert store.forget("a") is True
    assert store.forget("a") is False
    assert {i.name for i in store.list()} == {"b"}


def test_tampered_blob_detected(store, tmp_path):
    store.save("c", {"cookies": [], "origins": []})
    path = tmp_path / "idents" / "c.state.bin"
    data = bytearray(path.read_bytes())
    # Flip a byte in the ciphertext.
    data[20] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(IdentityError) as exc:
        store.load("c")
    assert exc.value.code == ErrCode.playwright_error
    assert "AEAD" in exc.value.detail


def test_aad_prevents_swap(store, tmp_path):
    """Swapping one identity's ciphertext to another's name must fail AEAD."""
    store.save("x", {"cookies": [{"name": "x"}]})
    store.save("y", {"cookies": [{"name": "y"}]})
    xpath = tmp_path / "idents" / "x.state.bin"
    ypath = tmp_path / "idents" / "y.state.bin"
    xbytes = xpath.read_bytes()
    # Overwrite y's blob with x's ciphertext.
    ypath.write_bytes(xbytes)
    with pytest.raises(IdentityError):
        store.load("y")


def test_missing_identity(store):
    with pytest.raises(IdentityError) as exc:
        store.load("nope")
    assert exc.value.code == ErrCode.secret_unknown


def test_invalid_name_rejected(store):
    for bad in ["", "a/b", "..", "../x", ".secret", "x" * 100, "a\x00b"]:
        with pytest.raises(IdentityError):
            store.save(bad, {"cookies": [], "origins": []})


def test_key_length_enforced(tmp_path):
    with pytest.raises(ValueError):
        IdentityStore(root=tmp_path / "x", key=b"short")


def test_update_meta(store):
    store.save("z", {"cookies": []})
    info = store.update_meta("z", provider="google")
    assert info.provider == "google"


def test_blob_ciphertext_does_not_contain_plaintext(store, tmp_path):
    secret_marker = "SUPER_SECRET_COOKIE_XYZ_12345"
    store.save("s", {"cookies": [{"name": "sid", "value": secret_marker}]})
    raw = (tmp_path / "idents" / "s.state.bin").read_bytes()
    assert secret_marker.encode("utf-8") not in raw


def test_meta_is_plaintext(store, tmp_path):
    """Metadata stays human-readable; only blobs are encrypted."""
    store.save("m", {"cookies": []}, provider="github")
    meta = json.loads((tmp_path / "idents" / "m.meta.json").read_text())
    assert meta["provider"] == "github"


def test_state_file_mode(store, tmp_path):
    store.save("p", {"cookies": []})
    mode = os.stat(tmp_path / "idents" / "p.state.bin").st_mode & 0o777
    assert mode == 0o600
