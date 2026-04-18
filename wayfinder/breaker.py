"""Per-host circuit breaker. Counts consecutive failures per host; trips after N."""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass(slots=True)
class HostBreaker:
    """Tracks failure streaks per host so we can stop hammering a source in trouble."""
    threshold: int
    consecutive: dict[str, int] = field(default_factory=dict)
    broken: set[str] = field(default_factory=set)

    def host_of(self, url: str) -> str:
        return (urlparse(url).hostname or "").lower()

    def is_broken(self, url: str) -> bool:
        return self.host_of(url) in self.broken

    def record_success(self, url: str) -> None:
        host = self.host_of(url)
        self.consecutive.pop(host, None)

    def record_failure(self, url: str) -> bool:
        """Returns True if recording this failure tripped the breaker."""
        host = self.host_of(url)
        self.consecutive[host] = self.consecutive.get(host, 0) + 1
        if self.consecutive[host] >= self.threshold and host not in self.broken:
            self.broken.add(host)
            return True
        return False
