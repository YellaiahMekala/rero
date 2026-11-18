from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .errors import FailureCode, RunFailure
from .models import Limits


class ToolBudget:
    """Local atomic tool-call budget. take() reserves one call before it is made."""

    def __init__(self, max_calls: int) -> None:
        self._max = max_calls
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        return self._used

    def take(self) -> int:
        with self._lock:
            if self._used >= self._max:
                raise RunFailure(FailureCode.TOOL_BUDGET_EXCEEDED)
            self._used += 1
            return self._used


@dataclass
class RunContext:
    """Per-run in-memory state. Holds the two bearer tokens for the life of the run only;
    they are excluded from repr and never written to the database or to logs."""

    run_id: str
    tenant_id: str
    job_id: str
    limits: Limits
    note: str = field(repr=False)
    run_token: str = field(repr=False)
    service_token: str = field(repr=False)
    budget: ToolBudget = field(init=False)
    model_calls: int = 0
    gateway_ids: list[str] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)
    stage: str = "LOAD_CONTEXT"
    served_chunk_ids: set[str] = field(default_factory=set)
    model_in_flight: bool = False

    def __post_init__(self) -> None:
        self.budget = ToolBudget(self.limits.maxToolCalls)

    def take_model_call(self) -> None:
        if self.model_calls >= self.limits.maxModelCalls:
            raise RunFailure(FailureCode.MODEL_BLOCKED)
        self.model_calls += 1
