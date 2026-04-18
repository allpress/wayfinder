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

            # If Greenhouse shows an "Apply" button first, click it.
            apply_btn = _find_handle(obs.handles, role="button", name_like=r"^apply\b")
            if apply_btn:
                r = s.click(apply_btn)
                if r.ok:
                    obs = s.observe(viewport_only=False)
                    emit(WayfinderEvent.now("progress", phase="apply_clicked"))

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

                handle = _find_handle_by_field_name(obs.handles, name)
                if handle is None:
                    # Re-observe once in case the form lazy-rendered.
                    obs = s.observe(viewport_only=False)
                    handle = _find_handle_by_field_name(obs.handles, name)
                if handle is None:
                    outcome.unhandled.append(label)
                    emit(WayfinderEvent.now("error", phase="handle_not_found",
                                             field=name, label=label))
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

            # Resume + cover letter uploads. Playwright locator.set_input_files
            # isn't exposed on our Session yet — fall through to the executor's
            # run() primitive. We look up the <input type=file> by fieldName.
            _try_file_upload(s, obs, field_name="resume", path=resume_path, emit=emit)
            if cover_path:
                obs = s.observe(viewport_only=False)
                _try_file_upload(s, obs, field_name="cover_letter",
                                  path=cover_path, emit=emit)

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


def _find_handle_by_field_name(handles: Any, field_name: str) -> str | None:
    """Greenhouse form fields aren't usually labelled with the field name
    directly; Playwright's accessible-name resolution picks up the <label>
    text. Match on label text first, then fall back to anything that mentions
    the field name verbatim.
    """
    # Try label that closely matches the field name (Greenhouse does this for
    # things like ``first_name`` → label "First Name").
    human = field_name.replace("_", " ").strip().lower()
    for h in handles or []:
        if h.role in ("textbox", "searchbox", "combobox", "listbox") and \
                (h.label or "").strip().lower() == human:
            return h.handle
    # Next: any handle whose label contains the field name.
    for h in handles or []:
        if field_name.lower() in (h.label or "").lower():
            return h.handle
    # Finally: name match.
    for h in handles or []:
        if field_name.lower() in (h.name or "").lower():
            return h.handle
    return None


def _try_file_upload(session: Any, obs: Any, *, field_name: str,
                     path: str, emit: EmitFn) -> None:
    """Best-effort file upload. Uses the Session's executor to drive the
    underlying Playwright locator's ``set_input_files`` — wayfinder doesn't
    expose a dedicated verb for this yet, so we reach through the executor.
    """
    import os
    if not path or not os.path.exists(path):
        emit(WayfinderEvent.now("error", phase="file_missing",
                                 field=field_name, path=path))
        return

    handle = _find_handle_by_field_name(obs.handles, field_name)
    if handle is None:
        emit(WayfinderEvent.now("error", phase="file_handle_not_found",
                                 field=field_name))
        return

    # Resolve the handle to a Playwright Locator, then set_input_files.
    from wayfinder.browser.observer import resolve_handle
    page = session._state.page if session._state else None    # noqa: SLF001
    if page is None:
        return

    loc, err, detail = session._executor.run(   # noqa: SLF001
        resolve_handle, page, obs, handle,
    )
    if err is not None or loc is None:
        emit(WayfinderEvent.now("error", phase="file_resolve_failed",
                                 field=field_name, detail=detail))
        return
    try:
        session._executor.run(lambda l, p: l.set_input_files(p), loc, path)   # noqa: SLF001
        emit(WayfinderEvent.now("progress", phase="file_uploaded",
                                 field=field_name, path=path))
    except Exception as e:   # noqa: BLE001
        emit(WayfinderEvent.now("error", phase="file_upload_failed",
                                 field=field_name, detail=f"{type(e).__name__}: {e}"))


def _fail(spawn_id: str, error: str) -> WayfinderReport:
    return WayfinderReport(
        spawn_id=spawn_id, type_name=_SPEC.type_name,
        status="failed", error=error,
    )


__all__ = ["GreenhouseApplicantWayfinder"]
