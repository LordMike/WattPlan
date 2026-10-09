"""Search evaluation budget; the worker separately enforces whole-call timeout."""

from dataclasses import dataclass, field
import math
import time


@dataclass
class SearchBudget:
    max_evaluations: int
    max_seconds: float | None = None
    evaluations: int = field(init=False, default=0)
    _start: float = field(init=False, default_factory=time.perf_counter)

    def __post_init__(self):
        if type(self.max_evaluations) is not int or self.max_evaluations < 1:
            raise ValueError("max_evaluations must be a positive integer")
        if self.max_seconds is not None and (
            type(self.max_seconds) not in {int, float}
            or not math.isfinite(self.max_seconds)
            or self.max_seconds <= 0
        ):
            raise ValueError("max_seconds must be finite and positive")

    @property
    def termination_reason(self) -> str | None:
        if self.evaluations >= self.max_evaluations:
            return "evaluation_limit"
        if (
            self.max_seconds is not None
            and time.perf_counter() - self._start >= self.max_seconds
        ):
            return "search_time_limit"
        return None

    def take(self) -> bool:
        """Reserve one evaluation before performing it; no hidden extra attempts."""
        if self.termination_reason is not None:
            return False
        self.evaluations += 1
        return True

    def diagnostics(self, reason: str = "search_complete") -> dict:
        return {
            "evaluations": self.evaluations,
            "termination_reason": self.termination_reason or reason,
            "search_seconds": time.perf_counter() - self._start,
        }
