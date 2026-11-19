# Copyright (c) 2026 the owner of this repository. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary. View only; see LICENSE and AI_USE_POLICY.md.
# Canary: rero-canary-64f662dc07dd453c9360
from __future__ import annotations

import httpx
from pydantic import BaseModel, ValidationError

from .errors import FailureCode, RunFailure
from .models import BreakSummary, EvidenceResponse, KnowledgeResponse, SimilarResponse
from .runctx import RunContext

_PREFIX = "/internal/rero/v1/jobs"


def _map_status(status: int) -> FailureCode:
    """Contract C section 2 errors mapped to run failure codes."""
    if status == 429:
        return FailureCode.TOOL_BUDGET_EXCEEDED
    if status in (401, 403, 409):
        return FailureCode.TOOL_REJECTED
    if status >= 500:
        return FailureCode.TOOL_UNAVAILABLE
    return FailureCode.TOOL_REJECTED


class ReconcileToolClient:
    """Read-only client for reconcile-ai internal tools. Exposes GET only: there is no
    method that issues any other verb. The run token is the bearer on every call."""

    def __init__(self, base_url: str, timeout: float) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get[T: BaseModel](
        self, ctx: RunContext, path: str, model: type[T], params: dict[str, str] | None = None
    ) -> T:
        ctx.budget.take()  # reserved atomically before the call; local limit is authoritative
        try:
            response = await self._client.get(
                f"{_PREFIX}/{ctx.job_id}/{path}",
                params=params,
                headers={"Authorization": f"Bearer {ctx.run_token}"},
            )
        except httpx.HTTPError as exc:
            raise RunFailure(FailureCode.TOOL_UNAVAILABLE) from exc
        if response.status_code != 200:
            raise RunFailure(_map_status(response.status_code))
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise RunFailure(FailureCode.TOOL_UNAVAILABLE) from exc

    async def get_break(self, ctx: RunContext) -> BreakSummary:
        return await self._get(ctx, "break", BreakSummary)

    async def get_evidence(self, ctx: RunContext, query: str) -> EvidenceResponse:
        return await self._get(ctx, "evidence", EvidenceResponse, {"query": query})

    async def get_similar_breaks(self, ctx: RunContext) -> SimilarResponse:
        return await self._get(ctx, "similar-breaks", SimilarResponse)

    async def get_knowledge(self, ctx: RunContext, query: str) -> KnowledgeResponse:
        return await self._get(ctx, "knowledge", KnowledgeResponse, {"query": query})
