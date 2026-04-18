"""Identity + storage_state round-trip through Session.save_storage."""
from __future__ import annotations

from wayfinder.browser import IdentityStore, Session


def test_storage_roundtrip_cookies_persist(executor, tmp_path, test_server):
    store = IdentityStore(root=tmp_path / "idents", key=b"K" * 32)

    # First session: set a cookie via document.cookie, then save.
    s1 = Session(executor=executor, store=store)
    r = s1.open(identity="alice", allowed_domains=["127.0.0.1"])
    assert r.ok
    try:
        assert s1.goto(test_server.base + "/index.html").ok
        # Set a cookie from JS (domain scoped to 127.0.0.1).
        executor.run(lambda p: p.evaluate(
            "document.cookie = 'sid=abc; path=/'"
        ), s1._state.page)
        save = s1.save_storage()
        assert save.ok
    finally:
        s1.close()

    # Second session: open with load_storage, expect cookie present.
    s2 = Session(executor=executor, store=store)
    r = s2.open(identity="alice", allowed_domains=["127.0.0.1"], load_storage=True)
    assert r.ok and r.loaded_storage
    try:
        assert s2.goto(test_server.base + "/index.html").ok
        cookie = executor.run(lambda p: p.evaluate("document.cookie"), s2._state.page)
        assert "sid=abc" in cookie
    finally:
        s2.close()


def test_save_storage_without_store_is_bad_argument(executor, test_server):
    s = Session(executor=executor)
    assert s.open(identity="nostore", allowed_domains=["127.0.0.1"]).ok
    try:
        r = s.save_storage()
        assert not r.ok
        from wayfinder.browser import ErrCode
        assert r.error == ErrCode.bad_argument
    finally:
        s.close()


def test_open_with_load_storage_for_missing_identity_is_ok(executor, tmp_path):
    store = IdentityStore(root=tmp_path / "idents2", key=b"K" * 32)
    s = Session(executor=executor, store=store)
    r = s.open(identity="neverexisted", allowed_domains=["127.0.0.1"],
               load_storage=True)
    assert r.ok
    assert r.loaded_storage is False
    s.close()
