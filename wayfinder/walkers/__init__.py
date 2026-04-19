"""Concrete wayfinder implementations.

Today:
  * :class:`HttpWalkerWayfinder` — resilient HTTP walker.
  * :class:`GreenhouseApplicantPlain` — bare-Playwright Greenhouse submitter.
    The primary path used by ``weaver submit apply``: ``get_by_label`` +
    ``set_input_files`` + a click-to-open helper for React dropdowns.
  * :class:`GreenhouseApplicantWayfinder` — the earlier observer/handle-
    based submitter. Kept around for the use cases that still benefit
    from the AX-tree model, but no longer the default for Greenhouse
    submissions; the wrapper's React-form interference made it unreliable.
"""
from wayfinder.walkers.greenhouse_plain import GreenhouseApplicantPlain
from wayfinder.walkers.greenhouse_submitter import GreenhouseApplicantWayfinder
from wayfinder.walkers.http import HttpWalkerWayfinder

__all__ = [
    "GreenhouseApplicantPlain",
    "GreenhouseApplicantWayfinder",
    "HttpWalkerWayfinder",
]
