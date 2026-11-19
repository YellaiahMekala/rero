# Copyright (c) 2026 the owner of this repository. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary. View only; see LICENSE and AI_USE_POLICY.md.
# Canary: rero-canary-64f662dc07dd453c9360
from __future__ import annotations

from typing import Any

import httpx

from .errors import FailureCode, RunFailure
from .model import DraftInput, ModelReply, SelfCheckInput
from .prompts import draft_messages, self_check_messages
from .runctx import RunContext

CHAT_PATH = "/api/v1/chat"

# Gateway codes that say nothing reached a provider (consumer contract section 3).
_NOT_EXECUTED = {
    "NOT_SENT",
    "RETRYABLE_FAILURE",
    "OVERLOADED",
    "MODEL_UNAVAILABLE",
    "GOVERNANCE_UNAVAILABLE",
    "NO_ELIGIBLE_MODEL",
    "PROVIDER_CONTRACT_FAILURE",
}
_BLOCKED = {
    "RISK_BLOCKED",
    "SENSITIVITY_BLOCKED",
    "BUDGET_BLOCKED",
    "IDEMPOTENCY_KEY_REUSED",
    "IDEMPOTENCY_IN_PROGRESS",
}
_UNKNOWN = {"UNKNOWN_OUTCOME", "IDEMPOTENCY_UNKNOWN_OUTCOME"}
# Failures where the request provably never left this process.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def map_gateway_error(status: int, code: str | None) -> FailureCode:
    """Contract C section 4 plus gateway consumer contract section 3. No retry on any outcome."""
    if code in _UNKNOWN or status == 504:
        return FailureCode.UNKNOWN_OUTCOME
    if code in _BLOCKED or status == 409:
        return FailureCode.MODEL_BLOCKED
    if code in _NOT_EXECUTED or status in (400, 401, 403, 404, 413, 429):
        return FailureCode.MODEL_UNAVAILABLE
    if status >= 500:
        return FailureCode.UNKNOWN_OUTCOME  # 5xx after send with no proof it did not execute
    return FailureCode.MODEL_UNAVAILABLE


class GatewayModel:
    """Model port over the ai-gateway chat endpoint (Contract B).

    Forwards the caller's own service token, sends Idempotency-Key {jobId}:draft / {jobId}:selfcheck,
    and never retries: one logical call is exactly one HTTP request."""

    def __init__(self, base_url: str, timeout: float) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def draft(self, ctx: RunContext, inp: DraftInput) -> ModelReply:
        return await self._call(ctx, "draft", draft_messages(inp, ctx.note))

    async def self_check(self, ctx: RunContext, inp: SelfCheckInput) -> ModelReply:
        return await self._call(ctx, "selfcheck", self_check_messages(inp))

    def _record(self, ctx: RunContext, body: Any) -> None:
        if isinstance(body, dict):
            inv = body.get("invocationId")
            if isinstance(inv, str) and inv and inv not in ctx.gateway_ids:
                ctx.gateway_ids.append(inv)

    async def _call(self, ctx: RunContext, suffix: str, messages: list[dict[str, str]]) -> ModelReply:
        payload = {
            "taskClass": "REASONING",
            "messages": messages,
            "settings": {"temperature": 0.0, "maxOutputTokens": 1024},
        }
        headers = {
            "Authorization": f"Bearer {ctx.service_token}",
            "Idempotency-Key": f"{ctx.job_id}:{suffix}",
        }
        ctx.model_in_flight = True
        try:
            response = await self._client.post(CHAT_PATH, json=payload, headers=headers)
        except _NEVER_SENT as exc:
            ctx.model_in_flight = False
            raise RunFailure(FailureCode.MODEL_UNAVAILABLE) from exc
        except httpx.HTTPError as exc:  # timeout or transport error after send: outcome unknown
            ctx.model_in_flight = False
            raise RunFailure(FailureCode.UNKNOWN_OUTCOME) from exc
        # A cancellation (run deadline) deliberately leaves model_in_flight set: the runner reads it.
        ctx.model_in_flight = False

        try:
            body: Any = response.json()
        except ValueError:
            body = None
        self._record(ctx, body)

        if response.status_code == 200:
            output = body.get("output") if isinstance(body, dict) else None
            if not isinstance(output, str):
                raise RunFailure(FailureCode.INVALID_MODEL_OUTPUT)
            return ModelReply(output)
        code = body.get("code") if isinstance(body, dict) and isinstance(body.get("code"), str) else None
        raise RunFailure(map_gateway_error(response.status_code, code))
