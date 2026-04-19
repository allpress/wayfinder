"""GreenhouseApplicantWayfinder — submit a Greenhouse application via the AI-facing browser.

Inputs (JSON-serialisable, per the Wayfinder protocol)::

    {
      "plan": {...},          # weaver.submitter.JobPlan as dict (same as
                              #   contexts/<ctx>/plans/<prefix>-<slug>.json)
      "resume_pdf_path": str,
      "cover_letter_pdf_path": str | None,

      # optional overrides:
      "identity":         str   (default: "greenhouse-<company-slug>"),
      "allowed_domains":  [str] (default: derived from plan.url)
      "headless":         bool  (default: false — humans review before submit)
      "pause_before_submit": bool (default: true — only ever click the
                                    final Submit button when false)
    }

Output::

    {
      "submitted":       bool,
      "filled":          int,          # number of fields we successfully set
      "flagged":         [...],        # fields we paused on (AI-policy, etc.)
      "unhandled":       [...],        # plan questions we could not map
      "final_url":       str,
      "screenshot_b64":  str | None,
    }

This worker NEVER presses Submit on its own by default. It fills the form,
takes a screenshot, and returns with ``submitted: false`` so a human (or a
caller that explicitly opts in via ``pause_before_submit: false``) makes
the actual send decision.

Secret handling: none today — Greenhouse apply flows are anonymous. If a
future board requires login, add a ``value_ref`` field to the plan and
wire it through ``secret_resolver`` exactly like the HTTP walker does.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from wayfinder.base import (
    EmitFn,
    SecretResolver,
    Wayfinder,
    WayfinderEvent,
    WayfinderReport,
    WayfinderSpec,
)


_SPEC = WayfinderSpec(
    type_name="greenhouse_submitter",
    description=(
        "Submit a Greenhouse application from a weaver JobPlan via the "
        "AI-facing browser. Fills every mapped field, flags AI-detection "
        "and unhandled questions, pauses before pressing Submit."
    ),
    required_inputs=frozenset({"plan", "resume_pdf_path"}),
    allowed_inputs=frozenset({
        "plan", "resume_pdf_path", "cover_letter_pdf_path",
        "identity", "allowed_domains", "headless", "pause_before_submit",
    }),
    secret_refs_allowed=True,   # future-proof: boards that need auth
)


@dataclass(slots=True)
class _FillOutcome:
    filled: int = 0
    flagged: list[str] = None       # type: ignore[assignment]
    unhandled: list[str] = None     # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.flagged is None:
            self.flagged = []
        if self.unhandled is None:
            self.unhandled = []


class GreenhouseApplicantWayfinder:
    """Concrete wayfinder that applies to a single Greenhouse posting."""

    spec = _SPEC

    def run(
        self,
        inputs: dict[str, Any],
        *,
        secret_resolver: SecretResolver | None = None,
        emit: EmitFn,
    ) -> WayfinderReport:
        spawn_id = inputs.get("__spawn_id", "")

        try:
            from wayfinder.browser import LocalExecutor, Session
            from wayfinder.browser.errors import ErrCode
        except ImportError as e:
            return WayfinderReport(
                spawn_id=spawn_id,
                type_name=_SPEC.type_name,
                status="failed",
                error=f"wayfinder[browser] not installed: {e}",
            )

        plan = inputs["plan"]
        if not isinstance(plan, dict):
            return WayfinderReport(
                spawn_id=spawn_id, type_name=_SPEC.type_name,
                status="failed", error="`plan` must be a dict",
            )
        url = str(plan.get("url") or "")
        if not url:
            return WayfinderReport(
                spawn_id=spawn_id, type_name=_SPEC.type_name,
                status="failed", error="`plan.url` missing",
            )

        resume_path = str(inputs["resume_pdf_path"])
        cover_path = inputs.get("cover_letter_pdf_path") or None

        identity = str(inputs.get("identity") or _default_identity(plan))
        allowed = list(inputs.get("allowed_domains") or _default_domains(url))
        headless = bool(inputs.get("headless", False))   # default headful
        pause_before_submit = bool(inputs.get("pause_before_submit", True))

        questions: list[dict[str, Any]] = list(plan.get("questions") or [])

        emit(WayfinderEvent.now(
            "status", phase="open",
            identity=identity, allowed_domains=allowed, headless=headless,
        ))

        s = Session(LocalExecutor())
        outcome = _FillOutcome()
        final_url = url
        shot_b64: str | None = None

        try:
            opened = s.open(identity=identity, allowed_domains=allowed, headless=headless)
            if not opened.ok:
                return _fail(spawn_id, f"open: {opened.error_detail or opened.error}")

            nav = s.goto(url, timeout_s=30)
            if not nav.ok:
                return _fail(spawn_id, f"goto: {nav.error_detail or nav.error}")

            obs = s.observe(viewport_only=False)
            emit(WayfinderEvent.now("progress", phase="form_opened",
                                     url=obs.url, handles=len(obs.handles)))

            # Many Greenhouse boards (Anthropic among them) now embed the
            # application form inline on the job posting — no modal, no
            # redirect. If we already see form inputs, don't click "Apply";
            # that button is the final Submit at the foot of the form, and
            # firing it on a blank form either destroys the page or sends
            # an empty application. Only click when no inputs are visible.
            has_form_inputs = any(
                h.role in _INTERACTABLE_ROLES
                for h in (obs.handles or [])
            )
            if not has_form_inputs:
                apply_btn = _find_handle(obs.handles, role="button",
                                          name_like=r"^apply\b")
                if apply_btn:
                    r = s.click(apply_btn)
                    if r.ok:
                        obs = s.observe(viewport_only=False)
                        emit(WayfinderEvent.now("progress", phase="apply_clicked"))
            else:
                emit(WayfinderEvent.now("progress", phase="form_inline",
                                         interactable=sum(
                                             1 for h in obs.handles
                                             if h.role in _INTERACTABLE_ROLES
                                         )))

            # Upload files BEFORE question fills. The observer-DOM on
            # Greenhouse degrades after a handful of React-driven input
            # events (each fill triggers a re-render that wipes observer
            # globals and thins the accessible tree). File inputs, being
            # ``display:none`` and not tracked by the observer, are
            # particularly vulnerable — if we wait, they vanish from the
            # locator too. Do them first while the DOM is fresh.
            _try_file_upload(s, obs, field_name="resume",
                              label_hint="resume", path=resume_path, emit=emit)
            if cover_path:
                _try_file_upload(s, obs, field_name="cover_letter",
                                  label_hint="cover letter",
                                  path=cover_path, emit=emit)

            # Fill every question the plan knows about.
            for q in questions:
                name = str(q.get("fieldName") or "").strip()
                label = str(q.get("label") or "")
                strategy = str(q.get("strategy") or "")
                if not name:
                    continue

                if strategy == "skipped-file-upload":
                    # Resume/CV — handled by the file-upload logic below.
                    continue

                if strategy == "unhandled":
                    outcome.unhandled.append(label)
                    emit(WayfinderEvent.now("finding", phase="unhandled", label=label))
                    continue

                if strategy == "ai-disclosure":
                    outcome.flagged.append(label)
                    emit(WayfinderEvent.now("finding", phase="ai_disclosure_flagged",
                                             label=label))
                    # Still try to answer if it's a select-yes; textareas we leave
                    # for the human to eyeball before submit.

                handle = _find_handle_by_field_name(obs.handles, name, label=label)
                if handle is None:
                    # Re-observe once in case the form lazy-rendered.
                    obs = s.observe(viewport_only=False)
                    handle = _find_handle_by_field_name(obs.handles, name, label=label)
                if handle is None:
                    outcome.unhandled.append(label)
                    observed = [
                        {"role": h.role, "label": h.label, "name": h.name[:40]}
                        for h in (obs.handles or [])[:25]
                        if h.role in _INTERACTABLE_ROLES
                    ]
                    emit(WayfinderEvent.now("error", phase="handle_not_found",
                                             field=name, label=label,
                                             observed=observed))
                    continue

                answer = str(q.get("proposedAnswer") or "")
                field_type = str(q.get("fieldType") or "")
                option_value = q.get("optionValue")

                ok = False
                if field_type == "multi_value_single_select":
                    option_str = str(option_value) if option_value is not None else answer
                    r = s.select(handle, option=option_str)
                    ok = r.ok
                elif answer:
                    r = s.fill(handle, value_literal=answer)
                    if not r.ok and r.error == ErrCode.secret_denied:
                        # We accidentally pointed a literal at a credential-
                        # shaped field. Log and flag.
                        outcome.flagged.append(f"{label} (credential-shape refused)")
                        emit(WayfinderEvent.now("error", phase="secret_denied",
                                                 field=name, label=label))
                        continue
                    ok = r.ok
                if ok:
                    outcome.filled += 1
                    emit(WayfinderEvent.now("progress", phase="field_filled",
                                             field=name, strategy=strategy))
                    obs = s.observe(viewport_only=False)

            # Screenshot for review.
            shot = s.screenshot(full_page=True)
            if shot.ok:
                shot_b64 = shot.b64

            final_url = s.observe().url

            if not pause_before_submit:
                obs = s.observe(viewport_only=False)
                submit = _find_handle(obs.handles, role="button",
                                       name_like=r"^submit( application)?$")
                if submit:
                    r = s.submit(submit)
                    emit(WayfinderEvent.now("progress", phase="submitted", ok=r.ok,
                                             error=r.error.value if r.error else None))
                    if r.ok:
                        return WayfinderReport(
                            spawn_id=spawn_id, type_name=_SPEC.type_name,
                            status="completed",
                            output={
                                "submitted": True,
                                "filled": outcome.filled,
                                "flagged": outcome.flagged,
                                "unhandled": outcome.unhandled,
                                "final_url": s.observe().url,
                                "screenshot_b64": shot_b64,
                            },
                        )

            # Paused for review — filled but not submitted.
            return WayfinderReport(
                spawn_id=spawn_id, type_name=_SPEC.type_name, status="completed",
                output={
                    "submitted": False,
                    "filled": outcome.filled,
                    "flagged": outcome.flagged,
                    "unhandled": outcome.unhandled,
                    "final_url": final_url,
                    "screenshot_b64": shot_b64,
                },
            )
        finally:
            try:
                s.close()
            except Exception:   # noqa: BLE001
                pass


# ---------- helpers ----------

def _default_identity(plan: dict[str, Any]) -> str:
    company = str(plan.get("company") or "greenhouse").lower()
    company = re.sub(r"[^a-z0-9]+", "-", company).strip("-") or "greenhouse"
    return f"greenhouse-{company}"


def _default_domains(url: str) -> list[str]:
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    root = ".".join(parts[-2:]) if len(parts) >= 2 else host
    return sorted({root, "greenhouse.io"})


def _find_handle(handles: Any, *, role: str, name_like: str) -> str | None:
    pat = re.compile(name_like, re.I)
    for h in handles or []:
        if h.role == role and pat.search(h.name or ""):
            return h.handle
    return None


_INTERACTABLE_ROLES = frozenset({
    "textbox", "searchbox", "combobox", "listbox",
    "radio", "checkbox", "spinbutton",
})


def _norm_label(s: str) -> str:
    """Normalise a form label for matching.

    Strips:
    - trailing required markers: ``*``, ``✱``, ``⁎``, ``(required)``, `` required``
    - ``(optional)`` anywhere
    - surrounding whitespace and collapses internal whitespace
    """
    if not s:
        return ""
    out = s.strip().lower()
    out = re.sub(r"\s*[*✱⁎]+\s*$", "", out)
    out = re.sub(r"\s*\(required\)\s*$", "", out)
    out = re.sub(r"\s+required\s*$", "", out)
    out = re.sub(r"\s*\(optional\)\s*", " ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _find_handle_by_field_name(handles: Any, field_name: str,
                                label: str | None = None) -> str | None:
    """Match a plan question to an observed handle.

    Greenhouse forms don't expose their machine field names in the
    accessible tree; Playwright's accessible-name resolver picks up the
    ``<label>`` text plus any trailing required markers. Match order:

    1. Exact match against the normalised plan ``label`` (if provided)
       or the humanised ``field_name``, restricted to interactable roles.
    2. Containment match (normalised target in normalised label) on
       interactable roles.
    3. Name-attribute containment for opaque ``question_NNNN`` fieldnames
       or file inputs that lack a proper label.
    """
    targets: list[str] = []
    if label:
        t = _norm_label(label)
        if t:
            targets.append(t)
    humanised = field_name.replace("_", " ")
    t = _norm_label(humanised)
    if t and t not in targets:
        targets.append(t)

    # Pass 1: exact normalised-label match on interactable roles.
    for h in handles or []:
        if h.role not in _INTERACTABLE_ROLES:
            continue
        h_lab = _norm_label(h.label or "")
        if h_lab and h_lab in targets:
            return h.handle
    # Pass 2: containment (target ⊆ label) on interactable roles.
    for h in handles or []:
        if h.role not in _INTERACTABLE_ROLES:
            continue
        h_lab = _norm_label(h.label or "")
        if not h_lab:
            continue
        for t in targets:
            if t and t in h_lab:
                return h.handle
    # Pass 3: name-attribute containment — catches opaque fieldnames and
    # file inputs that lack a proper accessible label.
    fn_lc = field_name.lower()
    for h in handles or []:
        if fn_lc and fn_lc in (h.name or "").lower():
            return h.handle
        # Also allow hinted label to match name when label is just a hint.
        for t in targets:
            if t and t in (h.name or "").lower():
                return h.handle
    return None


def _try_file_upload(session: Any, obs: Any, *, field_name: str,
                     path: str, emit: EmitFn,
                     label_hint: str | None = None) -> None:
    """Best-effort file upload.

    File inputs in Greenhouse (and most modern application forms) are
    ``display: none`` and driven by a styled button sibling. The
    accessible-name observer doesn't surface them, so the handle lookup
    used for every other field would always miss. Go direct through
    Playwright's CSS locator instead, matched on the ``name``/``id``
    attribute of the hidden ``<input type="file">``.
    """
    import os
    if not path or not os.path.exists(path):
        emit(WayfinderEvent.now("error", phase="file_missing",
                                 field=field_name, path=path))
        return

    page = session._state.page if session._state else None    # noqa: SLF001
    if page is None:
        emit(WayfinderEvent.now("error", phase="file_upload_failed",
                                 field=field_name, detail="no active page"))
        return

    def _upload_direct(pg: Any, p: str, fn: str,
                       hint: str | None) -> tuple[str, str]:
        # Greenhouse's hidden file input usually has either
        # ``name="resume"``/``"cover_letter"`` directly, or the nested
        # Rails form style ``name="job_application[resume]"``.
        # Partial-match on name first, then id, for both the fieldname
        # and the label hint.
        tried: list[str] = []
        candidates = [fn.lower()]
        if hint:
            h = hint.lower().replace(" ", "_")
            if h not in candidates:
                candidates.append(h)
            # Also try hyphenated form.
            h2 = hint.lower().replace(" ", "-")
            if h2 not in candidates:
                candidates.append(h2)
        for c in candidates:
            for sel in (f'input[type="file"][name*="{c}" i]',
                        f'input[type="file"][id*="{c}" i]'):
                tried.append(sel)
                loc = pg.locator(sel)
                try:
                    n = loc.count()
                except Exception:
                    n = 0
                if n > 0:
                    loc.first.set_input_files(p, timeout=5000)
                    return ("matched", sel)
        # Single-file-input fallback: only for the mandatory "resume"
        # slot. Don't fall through for optional fields like cover_letter
        # — if there's no matching input, the form doesn't support that
        # upload and we should skip rather than overwrite the resume.
        if fn == "resume":
            all_files = pg.locator('input[type="file"]')
            try:
                n = all_files.count()
            except Exception:
                n = 0
            if n == 1:
                all_files.first.set_input_files(p, timeout=5000)
                return ("single", "input[type=file]")
        return ("none", f"tried={tried}")

    try:
        outcome, detail = session._executor.run(   # noqa: SLF001
            _upload_direct, page, path, field_name, label_hint,
        )
    except Exception as e:   # noqa: BLE001
        emit(WayfinderEvent.now("error", phase="file_upload_failed",
                                 field=field_name,
                                 detail=f"{type(e).__name__}: {e}"))
        return

    if outcome in ("matched", "single"):
        emit(WayfinderEvent.now("progress", phase="file_uploaded",
                                 field=field_name, path=path, via=detail))
    else:
        emit(WayfinderEvent.now("error", phase="file_handle_not_found",
                                 field=field_name, detail=detail))


def _fail(spawn_id: str, error: str) -> WayfinderReport:
    return WayfinderReport(
        spawn_id=spawn_id, type_name=_SPEC.type_name,
        status="failed", error=error,
    )


__all__ = ["GreenhouseApplicantWayfinder"]
