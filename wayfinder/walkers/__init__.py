"""Concrete wayfinder implementations.

Today:
  * :class:`HttpWalkerWayfinder` — resilient HTTP walker.
  * :class:`GreenhouseApplicantWayfinder` — browser-driven job application
    submitter. The first wayfinder type that writes, not just reads.
"""
from wayfinder.walkers.greenhouse_submitter import GreenhouseApplicantWayfinder
from wayfinder.walkers.http import HttpWalkerWayfinder

__all__ = ["GreenhouseApplicantWayfinder", "HttpWalkerWayfinder"]
