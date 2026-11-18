from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from .models import BreakSummary, DraftOutput, EvidenceChunk, Procedure, SimilarItem
from .runctx import RunContext


@dataclass
class DraftInput:
    break_summary: BreakSummary
    similar: list[SimilarItem]
    chunks: list[EvidenceChunk]
    procedures: list[Procedure]


@dataclass
class SelfCheckInput:
    draft: DraftOutput
    chunks: list[EvidenceChunk]


@dataclass
class ModelReply:
    text: str  # raw model text: parsed by the graph, never stored or logged


class ModelPort(Protocol):
    """Exactly two calls per run: draft, then self-check."""

    async def draft(self, ctx: RunContext, inp: DraftInput) -> ModelReply: ...

    async def self_check(self, ctx: RunContext, inp: SelfCheckInput) -> ModelReply: ...


NOT_SERVED_ID = "99999999-9999-4999-8999-999999999999"


@dataclass
class FakeModel:
    """Deterministic model for tests and offline runs. No network.

    draft_mode: ok | invalid_json | missing_field | bad_citation | kb_citation | empty_citation
    self_check_mode: pass | challenge | invalid_json
    """

    draft_mode: str = "ok"
    self_check_mode: str = "pass"
    calls: list[str] = field(default_factory=list)

    async def draft(self, ctx: RunContext, inp: DraftInput) -> ModelReply:
        self.calls.append("draft")
        first = str(inp.chunks[0].chunkId) if inp.chunks else None
        cited = [first] if first else []
        category = "TIMING_DIFFERENCE" if first else "UNKNOWN"
        if self.draft_mode == "invalid_json":
            return ModelReply("this is not json {")
        if self.draft_mode == "missing_field":
            return ModelReply(json.dumps({"rootCauseCategory": category}))
        if self.draft_mode == "bad_citation":
            cited = [NOT_SERVED_ID]
        elif self.draft_mode == "kb_citation":
            cited = ["kb:proc-timing"]
        elif self.draft_mode == "empty_citation":
            cited = []
            category = "TIMING_DIFFERENCE"
        body = {
            "rootCauseCategory": category,
            "explanation": "Deterministic reference explanation based on the first served evidence chunk.",
            "citedChunkIds": cited,
            "confidence": 0.82 if first else 0.3,
            "rawOnlyMarker": "FAKE-RAW-OUTPUT-MARKER",  # present in raw output, dropped by validation
        }
        return ModelReply(json.dumps(body, indent=2))

    async def self_check(self, ctx: RunContext, inp: SelfCheckInput) -> ModelReply:
        self.calls.append("self_check")
        if self.self_check_mode == "invalid_json":
            return ModelReply("nope")
        if self.self_check_mode == "challenge":
            return ModelReply(
                json.dumps({"selfCheck": "CHALLENGE", "selfCheckReason": "Evidence only partly supports it."})
            )
        return ModelReply(
            json.dumps({"selfCheck": "PASS", "selfCheckReason": "Citations support the category."})
        )
