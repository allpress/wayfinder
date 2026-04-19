"""Plain-Playwright Greenhouse submitter.

Drop-in interface-compatible replacement for ``GreenhouseApplicantWayfinder``
that doesn't go through the wayfinder Session / observer / handle layer.
It drives Playwright directly with ``get_by_label`` + ``set_input_files``
and a tiny click-to-open helper for Greenhouse's React custom dropdowns.

Why this exists: the wayfinder-based submitter was found to break
Greenhouse's React form state after a few fills (observer re-injection
triggered re-renders that wiped the DOM). A bare-Playwright baseline
proved the form itself is fine; the wrapper was the problem. This module
is that baseline, wrapped up with the same ``.run(inputs, emit)`` shape
so ``weaver submit apply`` can swap to it via one import.

Interface (unchanged from the wayfinder version)::

    inputs:
        plan:                    dict (weaver JobPlan, serialised)
        resume_pdf_path:         str
        cover_letter_pdf_path:   str | None     (optional)
        headless:                bool = False   (default: headful)
        pause_before_submit:     bool = True    (default: paused)

    output:
        submitted:               bool
        filled:                  int
        flagged:                 list[str]
        unhandled:               list[str]
        final_url:               str
        screenshot_b64:          str | None

The submitter never presses Submit unless ``pause_before_submit`` is
explicitly False — default is fill + leave the browser open so a human
reviews before sending.
"""
from __future__ import annotations

import base64
from typing import Any

from wayfinder.base import (
    EmitFn,
    SecretResolver,
    WayfinderEvent,
    WayfinderReport,
    WayfinderSpec,
)


_SPEC = WayfinderSpec(
    type_name="greenhouse_plain",
    description=(
        "Plain-Playwright Greenhouse applicant. Fills a Greenhouse form "
        "from a weaver JobPlan using Playwright's get_by_label + "
        "set_input_files directly — no accessibility-tree observer, no "
        "handle system. Pauses before Submit by default."
    ),
    required_inputs=frozenset({"plan", "resume_pdf_path"}),
    allowed_inputs=frozenset({
        "plan", "resume_pdf_path", "cover_letter_pdf_path",
        "headless", "pause_before_submit",
    }),
    secret_refs_allowed=False,
)


class GreenhouseApplicantPlain:
    """Greenhouse submitter that talks to Playwright directly."""

    spec = _SPEC

    def run(
        self,
        inputs: dict[str, Any],
        *,
        secret_resolver: SecretResolver | None = None,
        emit: EmitFn,
    ) -> WayfinderReport:
        spawn_id = inputs.get("__spawn_id", "")

        plan = inputs.get("plan")
        if not isinstance(plan, dict):
            return _fail(spawn_id, "`plan` must be a dict")
        url = str(plan.get("url") or "")
        if not url:
            return _fail(spawn_id, "`plan.url` missing")

        resume_path = str(inputs.get("resume_pdf_path") or "")
        cover_path = inputs.get("cover_letter_pdf_path") or None
        headless = bool(inputs.get("headless", False))
        pause = bool(inputs.get("pause_before_submit", True))

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            return _fail(spawn_id, f"playwright not installed: {e}")

        filled = 0
        flagged: list[str] = []
        unhandled: list[str] = []
        final_url = url
        shot_b64: str | None = None
        submitted = False

        emit(WayfinderEvent.now(
            "status", phase="open", url=url, headless=headless,
        ))

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=headless)
            except Exception as e:
                return _fail(spawn_id,
                             f"chrome launch failed: {type(e).__name__}: {e}")
            try:
                ctx = browser.new_context()
                page = ctx.new_page()
                page.set_default_timeout(8000)

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    return _fail(spawn_id,
                                 f"goto {url!r}: {type(e).__name__}: {e}")
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass   # networkidle can time out on perpetually-chatty sites

                emit(WayfinderEvent.now("progress", phase="form_opened",
                                         url=page.url))

                # ---- uploads first, while the DOM is fresh ----
                _upload_resume(page, resume_path, emit)
                if cover_path:
                    _upload_cover(page, cover_path, emit)

                # ---- one pass over the plan questions ----
                for q in plan.get("questions") or []:
                    name = str(q.get("fieldName") or "").strip()
                    label = str(q.get("label") or "").strip()
                    answer = q.get("proposedAnswer") or ""
                    ft = str(q.get("fieldType") or "")
                    strategy = str(q.get("strategy") or "")

                    if strategy == "skipped-file-upload":
                        continue
                    if strategy == "unhandled":
                        unhandled.append(label)
                        emit(WayfinderEvent.now("finding", phase="unhandled",
                                                 label=label))
                        continue
                    if strategy == "ai-disclosure":
                        flagged.append(label)
                        emit(WayfinderEvent.now("finding",
                                                 phase="ai_disclosure_flagged",
                                                 label=label))
                    if not label or not answer:
                        continue

                    try:
                        if ft == "multi_value_single_select":
                            if _fill_react_dropdown(page, label, str(answer)):
                                filled += 1
                                emit(WayfinderEvent.now(
                                    "progress", phase="field_filled",
                                    field=name, strategy=strategy, via="dropdown",
                                ))
                            else:
                                unhandled.append(label)
                                emit(WayfinderEvent.now(
                                    "error", phase="dropdown_not_resolved",
                                    field=name, label=label, answer=str(answer),
                                ))
                        else:
                            page.get_by_label(label, exact=False).first.fill(
                                str(answer), timeout=4000,
                            )
                            filled += 1
                            emit(WayfinderEvent.now(
                                "progress", phase="field_filled",
                                field=name, strategy=strategy,
                            ))
                    except Exception as e:   # noqa: BLE001
                        unhandled.append(label)
                        emit(WayfinderEvent.now(
                            "error", phase="field_fill_failed",
                            field=name, label=label,
                            detail=f"{type(e).__name__}: {str(e)[:100]}",
                        ))

                # ---- screenshot, submit or pause ----
                final_url = page.url
                try:
                    shot_b64 = base64.b64encode(
                        page.screenshot(full_page=True)
                    ).decode("ascii")
                except Exception:
                    shot_b64 = None

                if not pause:
                    try:
                        submit = _find_submit_button(page)
                        submit.click(timeout=6000)
                        page.wait_for_load_state("domcontentloaded", timeout=10000)
                        submitted = True
                        final_url = page.url
                        emit(WayfinderEvent.now("progress", phase="submitted",
                                                 url=final_url))
                    except Exception as e:   # noqa: BLE001
                        emit(WayfinderEvent.now("error", phase="submit_failed",
                                                 detail=f"{type(e).__name__}: {e}"))
                else:
                    emit(WayfinderEvent.now("status", phase="paused_for_review"))
                    # Block until the human closes the window.
                    try:
                        page.wait_for_event("close", timeout=0)
                    except Exception:
                        pass

                return WayfinderReport(
                    spawn_id=spawn_id, type_name=_SPEC.type_name,
                    status="completed",
                    output={
                        "submitted": submitted,
                        "filled": filled,
                        "flagged": flagged,
                        "unhandled": unhandled,
                        "final_url": final_url,
                        "screenshot_b64": shot_b64,
                    },
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass


# ---- helpers ----

def _upload_resume(page: Any, path: str, emit: EmitFn) -> None:
    import os
    if not path or not os.path.exists(path):
        emit(WayfinderEvent.now("error", phase="file_missing",
                                 field="resume", path=path))
        return
    # Greenhouse conventions: id="resume" or name="resume" on a hidden
    # input[type=file]. If neither, fall back to the single file input
    # on the page (only for resume — the mandatory slot).
    tried: list[str] = []
    for sel in ('input#resume',
                'input[type="file"][name*="resume" i]',
                'input[type="file"][id*="resume" i]'):
        tried.append(sel)
        loc = page.locator(sel)
        try:
            if loc.count() > 0:
                loc.first.set_input_files(path, timeout=5000)
                emit(WayfinderEvent.now("progress", phase="file_uploaded",
                                         field="resume", via=sel))
                return
        except Exception:
            continue
    all_files = page.locator('input[type="file"]')
    try:
        if all_files.count() == 1:
            all_files.first.set_input_files(path, timeout=5000)
            emit(WayfinderEvent.now("progress", phase="file_uploaded",
                                     field="resume", via="single"))
            return
    except Exception:
        pass
    emit(WayfinderEvent.now("error", phase="file_handle_not_found",
                             field="resume", tried=tried))


def _upload_cover(page: Any, path: str, emit: EmitFn) -> None:
    """Cover-letter upload. Only fires when a dedicated slot exists —
    never falls back to the ``resume`` input, since that would overwrite
    the résumé we just uploaded."""
    import os
    if not path or not os.path.exists(path):
        emit(WayfinderEvent.now("error", phase="file_missing",
                                 field="cover_letter", path=path))
        return
    for sel in ('input#cover_letter',
                'input[type="file"][name*="cover_letter" i]',
                'input[type="file"][id*="cover_letter" i]',
                'input[type="file"][name*="cover" i]',
                'input[type="file"][id*="cover" i]'):
        loc = page.locator(sel)
        try:
            if loc.count() > 0:
                loc.first.set_input_files(path, timeout=5000)
                emit(WayfinderEvent.now("progress", phase="file_uploaded",
                                         field="cover_letter", via=sel))
                return
        except Exception:
            continue
    emit(WayfinderEvent.now("finding", phase="cover_letter_slot_absent"))


def _fill_react_dropdown(page: Any, label: str, value: str) -> bool:
    """Click-to-open pattern for Greenhouse's React custom selects."""
    try:
        page.get_by_label(label, exact=False).first.click(timeout=4000)
    except Exception:
        return False
    page.wait_for_timeout(200)
    option_locators = [
        lambda: page.get_by_role("option", name=value, exact=True),
        lambda: page.get_by_role("option", name=value, exact=False),
        lambda: page.locator('[role="option"]').filter(has_text=value),
        lambda: page.get_by_text(value, exact=True),
    ]
    for mk in option_locators:
        try:
            mk().first.click(timeout=3000)
            return True
        except Exception:
            continue
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return False


def _find_submit_button(page: Any) -> Any:
    """Greenhouse's final Submit button, across variants."""
    for name_pat in ("Submit application", "Submit Application", "Submit"):
        btn = page.get_by_role("button", name=name_pat, exact=False)
        try:
            if btn.count() > 0:
                return btn.first
        except Exception:
            continue
    raise RuntimeError("no Submit button found")


def _fail(spawn_id: str, error: str) -> WayfinderReport:
    return WayfinderReport(
        spawn_id=spawn_id, type_name=_SPEC.type_name,
        status="failed", error=error,
    )


__all__ = ["GreenhouseApplicantPlain"]
