"""Wayfinder — a family of agent-workers Warden spawns to do scoped work.

Core: the Wayfinder protocol. First shipped type: the resilient HTTP walker.
"""
from wayfinder.base import (
    EmitFn,
    SecretResolver,
    SecretScope,
    Wayfinder,
    WayfinderEvent,
    WayfinderReport,
    WayfinderSpec,
    validate_inputs,
)
from wayfinder.events import WalkEvent, WalkReport
from wayfinder.http_client import HttpClient, HttpxAdapter, HttpResponse
from wayfinder.policy import FetchPolicy
from wayfinder.walker import WalkTarget, walk
from wayfinder.walkers import HttpWalkerWayfinder

__version__ = "0.1.0"

__all__ = [
    # legacy direct-walk API
    "FetchPolicy",
    "HttpClient",
    "HttpResponse",
    "HttpxAdapter",
    "WalkEvent",
    "WalkReport",
    "WalkTarget",
    "walk",
    # wayfinder protocol + first implementation
    "EmitFn",
    "HttpWalkerWayfinder",
    "SecretResolver",
    "SecretScope",
    "Wayfinder",
    "WayfinderEvent",
    "WayfinderReport",
    "WayfinderSpec",
    "validate_inputs",
]
