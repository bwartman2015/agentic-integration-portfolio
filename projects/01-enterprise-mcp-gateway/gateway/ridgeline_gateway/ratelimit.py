"""Rate limits (Milestone 5): a sliding one-minute window per verified client.

The key is the `client_id` from the verified token, so one agent cannot use up another's budget and a
caller cannot choose its own key. State is in memory (one gateway process); a multi-replica deployment
would move it to a shared store. A call over the limit is denied before policy or any backend is asked,
and is audited like every other denial.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Mapping

DEFAULT_PER_MINUTE = 120
WINDOW_S = 60.0
_CLIENT = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class RateLimitConfigError(RuntimeError):
    """Raised at startup. Messages name variables, never values."""


@dataclass(frozen=True)
class RateLimitConfig:
    per_minute: int = DEFAULT_PER_MINUTE
    overrides: tuple[tuple[str, int], ...] = ()        # (client_id, per_minute)

    @staticmethod
    def from_env(env: Mapping[str, str]) -> "RateLimitConfig":
        errors: list[str] = []
        raw = (env.get("RATE_LIMIT_PER_MINUTE") or str(DEFAULT_PER_MINUTE)).strip()
        per_minute = int(raw) if raw.isdigit() else 0
        if not 1 <= per_minute <= 100_000:
            errors.append("RATE_LIMIT_PER_MINUTE must be a whole number between 1 and 100000")
        overrides: list[tuple[str, int]] = []
        for item in filter(None, (p.strip() for p in (env.get("RATE_LIMIT_OVERRIDES") or "").split(","))):
            client, sep, limit = item.partition("=")
            if not sep or not _CLIENT.match(client.strip()) or not limit.strip().isdigit() or int(limit) < 1:
                errors.append("RATE_LIMIT_OVERRIDES must look like client-a=30,client-b=5")
                break
            overrides.append((client.strip(), int(limit)))
        if errors:
            raise RateLimitConfigError("invalid rate-limit configuration: " + "; ".join(errors))
        return RateLimitConfig(per_minute, tuple(overrides))


@dataclass
class RateLimiter:
    config: RateLimitConfig = field(default_factory=RateLimitConfig)
    clock: Callable[[], float] = time.monotonic
    _calls: dict[str, deque[float]] = field(default_factory=dict, repr=False)

    def limit_for(self, client_id: str) -> int:
        return dict(self.config.overrides).get(client_id, self.config.per_minute)

    def allow(self, client_id: str) -> bool:
        """Count one call for this client; False when it is over its limit (the call is not counted)."""
        now = self.clock()
        window = self._calls.setdefault(client_id, deque())
        while window and now - window[0] >= WINDOW_S:
            window.popleft()
        if len(window) >= self.limit_for(client_id):
            return False
        window.append(now)
        return True
