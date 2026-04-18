"""Observation diffing.

Compute what changed between a `before` and `after` Observation so callers
don't need to re-parse the full snapshot. Diffs are always safe to compute
even when either side is ``None`` (we represent that as an empty, fully-added
or fully-removed diff).
"""
from __future__ import annotations

from wayfinder.browser.models import Interactable, NetEvent, Observation, ObservationDiff


def diff(before: Observation | None, after: Observation | None) -> ObservationDiff:
    if before is None and after is None:
        return ObservationDiff(url_changed=False, title_changed=False,
                               url_before="", url_after="")
    if before is None:
        assert after is not None
        return ObservationDiff(
            url_changed=True,
            title_changed=True,
            url_before="",
            url_after=after.url,
            added_handles=[h.handle for h in after.handles],
            added_text=[t.handle for t in after.text_blocks],
            new_network=list(after.network_tail),
            new_console=list(after.console_tail),
        )
    if after is None:
        return ObservationDiff(
            url_changed=True,
            title_changed=True,
            url_before=before.url,
            url_after="",
            removed_handles=[h.handle for h in before.handles],
        )

    before_idx: dict[str, Interactable] = {h.handle: h for h in before.handles}
    after_idx: dict[str, Interactable] = {h.handle: h for h in after.handles}

    added = [h for h in after_idx if h not in before_idx]
    removed = [h for h in before_idx if h not in after_idx]
    changed: list[str] = []
    for handle, post in after_idx.items():
        pre = before_idx.get(handle)
        if pre is None:
            continue
        if _meaningful_change(pre, post):
            changed.append(handle)

    before_text = {t.handle for t in before.text_blocks}
    added_text = [t.handle for t in after.text_blocks if t.handle not in before_text]

    new_network = _suffix_new(before.network_tail, after.network_tail)
    new_console = _suffix_new_str(before.console_tail, after.console_tail)

    return ObservationDiff(
        url_changed=before.url != after.url,
        title_changed=before.title != after.title,
        url_before=before.url,
        url_after=after.url,
        added_handles=added,
        removed_handles=removed,
        changed_handles=changed,
        added_text=added_text,
        new_network=new_network,
        new_console=new_console,
    )


def _meaningful_change(pre: Interactable, post: Interactable) -> bool:
    # Deliberately ignore bbox drift (scroll) and ordinal changes — those are
    # noise. Everything else that affects interaction counts as a change.
    return (
        pre.value != post.value
        or pre.disabled != post.disabled
        or pre.checked != post.checked
        or pre.required != post.required
        or pre.editable != post.editable
        or pre.name != post.name
        or pre.placeholder != post.placeholder
        or pre.label != post.label
    )


def _suffix_new(before: list[NetEvent], after: list[NetEvent]) -> list[NetEvent]:
    """Return after-events that weren't in before, preserving order.

    Compared by (ts, event, url, method, status) — ts makes this deterministic
    even when the tails are the same length but rotating.
    """
    before_keys = {(e.ts, e.event, e.url, e.method, e.status) for e in before}
    return [e for e in after if (e.ts, e.event, e.url, e.method, e.status) not in before_keys]


def _suffix_new_str(before: list[str], after: list[str]) -> list[str]:
    bset = set(before)
    return [s for s in after if s not in bset]


__all__ = ["diff"]
