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
        "applicant_profile",
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
        profile = inputs.get("applicant_profile") or {}
        if not isinstance(profile, dict):
            profile = {}

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
                        elif ft == "multi_value_multi_select":
                            # Resolve the list of label strings to click.
                            labels_to_click: list[str] = []
                            opt_val = q.get("optionValue")
                            opts = q.get("options") or []
                            if isinstance(opt_val, list) and opt_val:
                                by_val = {str(o.get("value")): o.get("label")
                                          for o in opts}
                                labels_to_click = [
                                    str(by_val.get(str(v)))
                                    for v in opt_val
                                    if by_val.get(str(v))
                                ]
                            if not labels_to_click:
                                labels_to_click = [s.strip()
                                                    for s in str(answer).split(",")
                                                    if s.strip()]
                            picked = _fill_react_multiselect(
                                page, label, labels_to_click,
                            )
                            if picked > 0:
                                filled += 1
                                emit(WayfinderEvent.now(
                                    "progress", phase="field_filled",
                                    field=name, strategy=strategy,
                                    via="multiselect", picked=picked,
                                ))
                            else:
                                unhandled.append(label)
                                emit(WayfinderEvent.now(
                                    "error", phase="multiselect_not_resolved",
                                    field=name, label=label,
                                    answer=str(answer),
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

                # ---- standard Greenhouse client-side fields ----
                # Country, Gender, Hispanic/Latino, Veteran, Disability,
                # and the phone country-code picker are rendered by
                # Greenhouse's boilerplate template and never show up in
                # the job-board API, so the plan never asks us to fill
                # them. Do it here from the profile instead.
                eeoc_filled, eeoc_missing = _fill_standard_fields(
                    page, profile, emit,
                )
                filled += eeoc_filled

                # ---- post-fill audit: which required fields are still empty ----
                empty_required = _audit_required_empty(page)
                if empty_required:
                    emit(WayfinderEvent.now(
                        "finding", phase="required_unfilled",
                        count=len(empty_required),
                        fields=empty_required[:20],
                    ))

                # ---- screenshot, submit or pause ----
                final_url = page.url
                try:
                    shot_b64 = base64.b64encode(
                        page.screenshot(full_page=True)
                    ).decode("ascii")
                except Exception:
                    shot_b64 = None

                submission_evidence: dict = {}
                if not pause:
                    submission_evidence = _submit_and_verify(
                        page, original_url=url, emit=emit,
                    )
                    submitted = bool(submission_evidence.get("confirmed"))
                    final_url = submission_evidence.get(
                        "final_url", final_url,
                    )
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
                        "submission": submission_evidence,
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


def _fill_standard_fields(page: Any, profile: dict,
                           emit: EmitFn) -> tuple[int, list[str]]:
    """Fill Greenhouse's boilerplate client-side-only fields.

    These fields (``#country``, ``#gender``, ``#hispanic_ethnicity``,
    ``#veteran_status``, ``#disability_status``, and the phone
    country-code picker) are rendered by Greenhouse's template and
    don't appear in the job-board JSON, so the plan builder never
    generates questions for them. We match by stable id and fill from
    the applicant profile. Every entry is best-effort — if the field
    isn't on this form, we skip silently.

    Returns ``(filled_count, missing_fields)`` — ``missing_fields``
    lists ids we tried to fill that weren't present on the page.
    """
    filled = 0
    missing: list[str] = []

    # Mapping: (field_id, label_for_emit, profile_key, default_when_blank)
    # Values must match one of Greenhouse's standard option labels — these
    # are the exact strings the React menu renders, verified against live
    # Scale AI + Anthropic boards.
    plan = [
        ("country",            "Country",           None,                   "United States"),
        ("gender",             "Gender",            "gender",               None),
        ("hispanic_ethnicity", "Hispanic/Latino",   "hispanic_or_latino",   None),
        ("veteran_status",     "Veteran status",    "veteran_status",       None),
        ("disability_status",  "Disability status", "disability_status",
                                                     "I do not want to answer"),
    ]

    for fid, human_label, profile_key, default in plan:
        loc = page.locator(f"#{fid}")
        try:
            if loc.count() == 0:
                continue
        except Exception:
            continue

        value = ""
        if profile_key:
            value = str(profile.get(profile_key) or "").strip()
        if not value and default:
            value = default
        if not value:
            continue  # field present but profile says to skip (e.g. disability)

        # Close any picker that might still be open from a previous field
        # (the phone country-code widget is especially sticky and floods
        # the role=option list with country names).
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(100)
        except Exception:
            pass

        try:
            if _fill_react_dropdown(page, human_label, value):
                emit(WayfinderEvent.now(
                    "progress", phase="standard_field_filled",
                    field=fid, value=value,
                ))
                filled += 1
            else:
                missing.append(fid)
                emit(WayfinderEvent.now(
                    "error", phase="standard_field_failed",
                    field=fid, value=value,
                ))
        except Exception as e:   # noqa: BLE001
            missing.append(fid)
            emit(WayfinderEvent.now(
                "error", phase="standard_field_failed",
                field=fid, detail=f"{type(e).__name__}: {str(e)[:80]}",
            ))

    # Phone country code: Greenhouse uses the intl-tel-input library.
    # Clicking the flag opens a searchable list of countries. Prefer to
    # just set the phone with a +1 prefix (the library auto-detects), but
    # also try the explicit flag-click approach in case the phone field
    # was already filled without prefix.
    cc = str(profile.get("phone_country_code") or "").strip()
    phone = str(profile.get("phone") or "").strip()
    if cc and phone and not phone.startswith(cc):
        try:
            page.locator("#phone").fill(f"{cc} {phone}", timeout=3000)
            emit(WayfinderEvent.now(
                "progress", phase="standard_field_filled",
                field="phone_with_country_code",
                value=f"{cc} {phone}",
            ))
            filled += 1
        except Exception:
            pass

    return filled, missing


def _audit_required_empty(page: Any) -> list[dict]:
    """Return a list of required fields on the page that are still empty.

    Looks at DOM state after all fills have run. Used by the submitter
    to surface anything the pipeline missed before the human is asked
    to review. Inputs with ``required``/``aria-required`` and no value
    (or an unselected combobox) are flagged.
    """
    try:
        return page.evaluate("""
            () => {
              function labelFor(el) {
                if (el.ariaLabel) return el.ariaLabel;
                if (el.id) {
                  const lab = document.querySelector(`label[for="${el.id}"]`);
                  if (lab) return lab.innerText.trim();
                }
                let p = el.parentElement;
                for (let d=0; p && d<5; d++, p=p.parentElement) {
                  const l = p.querySelector('label');
                  if (l) return l.innerText.trim();
                }
                return '';
              }
              function isHidden(el) {
                if (el.type === 'hidden') return true;
                if (el.hidden) return true;
                // Check computed styles through the ancestor chain — a
                // hidden parent hides the input even if the input itself
                // has default styles. Greenhouse's React dropdowns keep
                // a hidden backing input that would otherwise be flagged.
                let cur = el;
                while (cur && cur !== document.body) {
                  const cs = getComputedStyle(cur);
                  if (cs.display === 'none' || cs.visibility === 'hidden') return true;
                  cur = cur.parentElement;
                }
                // File inputs are intentionally display:none in Greenhouse;
                // the click target is a button sibling. Treat them as visible
                // for audit purposes.
                if (el.type === 'file') return false;
                return false;
              }
              function isReactDropdown(el) {
                // Greenhouse custom selects: the <input type="text"> is
                // the dropdown's search box. The selected value lives in
                // the component's internal state — el.value is always
                // empty string. Detect these and check for a *visible*
                // selected-value indicator elsewhere in the widget.
                if (el.type !== 'text' && el.type !== 'search') return null;
                if (el.getAttribute('role') === 'combobox') return el;
                // Greenhouse wraps dropdown inputs in a container with
                // role=combobox or data-testid/class containing "select".
                let p = el.parentElement;
                for (let d=0; p && d<6; d++, p=p.parentElement) {
                  if (p.getAttribute('role') === 'combobox') return p;
                  if ((p.className || '').match(/select-__?single-value|select-__?value-container/)) return p;
                }
                return null;
              }
              function dropdownHasSelection(widget) {
                // Greenhouse's React-select wraps the chosen value in a
                // container that gets the modifier class
                // ``select__value-container--has-value`` when populated.
                // Walk widget + ancestors to find it.
                let node = widget;
                for (let d=0; node && d<8; d++, node=node.parentElement) {
                  const cls = (node.className || '').toString();
                  if (cls.includes('value-container--has-value')) return true;
                  if (cls.includes('singleValue')) return true;
                  // Generic: any descendant that visually shows a picked value.
                  const picked = node.querySelector(
                    '[class*="value-container--has-value"],'
                    + ' [class*="singleValue"],'
                    + ' [class*="single-value"]'
                  );
                  if (picked) return true;
                }
                return false;
              }
              const out = [];
              for (const el of document.querySelectorAll(
                  'input, select, textarea'
              )) {
                const req = el.required || el.getAttribute('aria-required')==='true';
                if (!req) continue;
                if (isHidden(el)) continue;
                if (el.type === 'file') {
                  if (!el.files || el.files.length === 0) {
                    out.push({id: el.id, name: el.name,
                              label: labelFor(el), kind: 'file'});
                  }
                  continue;
                }
                const widget = isReactDropdown(el);
                if (widget) {
                  if (!dropdownHasSelection(widget)) {
                    out.push({id: el.id, name: el.name,
                              label: labelFor(el), kind: 'react-dropdown'});
                  }
                  continue;
                }
                const val = (el.value || '').trim();
                if (val === '') {
                  out.push({id: el.id, name: el.name,
                            label: labelFor(el), kind: el.type || el.tagName.toLowerCase()});
                }
              }
              return out;
            }
        """)
    except Exception:
        return []


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


_CONFIRMATION_PHRASES = (
    # Greenhouse-standard confirmation text variants — the ones this code
    # has actually seen in the wild. Ordered roughly most→least specific.
    "thank you for applying",
    "thank you for your application",
    "thanks for applying",
    "thanks for your application",
    "your application has been submitted",
    "your application has been received",
    "we've received your application",
    "we have received your application",
    "application received",
    "application submitted",
    "application confirmation",
    "successfully submitted",
    "your application is now under review",
    "under review",
    # "You've already applied" is also a confirmation — proves a prior
    # submission was accepted, which is what we're trying to verify.
    "you've already applied",
    "you have already applied",
    "already applied to this",
    "duplicate application",
)


def _submit_and_verify(page: Any, *, original_url: str,
                        emit: EmitFn) -> dict:
    """Click the Submit button, wait for the page to settle, then
    capture enough evidence to confirm (or refute) that the submission
    actually went through.

    Returns a dict with keys:
        clicked              — did the Submit click succeed
        url_changed          — did page.url differ from ``original_url``
        confirmation_found   — did body text match a known confirmation phrase
        matched_phrase       — which phrase matched (empty if none)
        confirmed            — True iff (url_changed OR confirmation_found)
        final_url            — page.url after the wait
        page_title           — document.title after the wait
        response_text        — first ~1500 chars of body innerText
        error                — only present when the click itself failed

    Greenhouse's SPA routing means URL changes aren't guaranteed on
    every board; some boards stay on the same /jobs/<id> URL and swap
    the DOM to a "Thanks" block. That's why we check both signals.
    """
    result: dict = {
        "clicked": False,
        "confirmed": False,
        "url_changed": False,
        "confirmation_found": False,
        "matched_phrase": "",
        "final_url": original_url,
        "page_title": "",
        "response_text": "",
    }

    try:
        submit = _find_submit_button(page)
        submit.click(timeout=6000)
        result["clicked"] = True
    except Exception as e:   # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
        emit(WayfinderEvent.now("error", phase="submit_click_failed",
                                 detail=result["error"]))
        return result

    # Wait for the post-submit page to settle. networkidle catches the
    # AJAX-style boards; an explicit timeout gives React time to render
    # the confirmation block on in-place boards.
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass

    try:
        result["final_url"] = page.url
    except Exception:
        pass
    result["url_changed"] = result["final_url"] != original_url
    try:
        result["page_title"] = page.title() or ""
    except Exception:
        pass
    try:
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
        result["response_text"] = (text or "").strip()[:1500]
    except Exception:
        pass

    low = result["response_text"].lower()
    for phrase in _CONFIRMATION_PHRASES:
        if phrase in low:
            result["confirmation_found"] = True
            result["matched_phrase"] = phrase
            break

    result["confirmed"] = bool(
        result["url_changed"] or result["confirmation_found"]
    )

    emit(WayfinderEvent.now(
        "progress" if result["confirmed"] else "error",
        phase="submit_verified" if result["confirmed"] else "submit_unverified",
        final_url=result["final_url"],
        url_changed=result["url_changed"],
        confirmation_found=result["confirmation_found"],
        matched_phrase=result["matched_phrase"],
        response_preview=result["response_text"][:280],
    ))

    return result


def _fill_react_multiselect(page: Any, label: str,
                             values: list[str]) -> int:
    """Click-each-option pattern for Greenhouse multi-select dropdowns.

    Opens the field, clicks every target option, dismisses. Returns the
    number of options successfully clicked.
    """
    if not values:
        return 0
    try:
        page.get_by_label(label, exact=False).first.click(timeout=4000)
    except Exception:
        return 0
    page.wait_for_timeout(250)

    clicked = 0
    for val in values:
        val = str(val)
        if not val:
            continue
        got_this = False
        for mk in (
            lambda v=val: page.get_by_role("option", name=v, exact=True),
            lambda v=val: page.get_by_role("option", name=v, exact=False),
            lambda v=val: page.locator('[role="option"]').filter(has_text=v),
        ):
            try:
                mk().first.click(timeout=2500)
                got_this = True
                clicked += 1
                page.wait_for_timeout(150)
                break
            except Exception:
                continue
        if not got_this:
            continue
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return clicked


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
