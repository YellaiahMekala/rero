from __future__ import annotations

from enum import StrEnum


class FailureCode(StrEnum):
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    TOOL_BUDGET_EXCEEDED = "TOOL_BUDGET_EXCEEDED"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    TOOL_REJECTED = "TOOL_REJECTED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_BLOCKED = "MODEL_BLOCKED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    INVALID_MODEL_OUTPUT = "INVALID_MODEL_OUTPUT"
    INVALID_CITATION = "INVALID_CITATION"
    RESTARTED = "RESTARTED"


class RunFailure(Exception):  # noqa: N818 - domain name
    """A run ends FAILED with a contract failure code. Carries no payload."""

    def __init__(self, code: FailureCode) -> None:
        super().__init__(code.value)
        self.code = code


class ApiError(Exception):
    """Non-2xx response with the contract error body. The message is safe text."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
