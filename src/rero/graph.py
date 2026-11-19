# Copyright (c) 2026 the owner of this repository. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary. View only; see LICENSE and AI_USE_POLICY.md.
# Canary: rero-canary-64f662dc07dd453c9360
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError

from .errors import FailureCode, RunFailure
from .model import DraftInput, ModelPort, SelfCheckInput
from .models import (
    BreakSummary,
    DraftOutput,
    EvidenceChunk,
    Procedure,
    SelfCheckOutput,
    SimilarItem,
)
from .runctx import RunContext
from .tools import ReconcileToolClient

NODE_ORDER = ["LOAD_CONTEXT", "RETRIEVE", "DRAFT", "REFERENCE_CHECK", "SELF_CHECK", "DONE"]
MAX_QUERY_CHARS = 500


class RunState(TypedDict, total=False):
    break_summary: BreakSummary
    similar: list[SimilarItem]
    chunks: list[EvidenceChunk]
    procedures: list[Procedure]
    draft: DraftOutput
    self_check: SelfCheckOutput
    result: dict[str, Any]


@dataclass
class Deps:
    tools: ReconcileToolClient
    model: ModelPort
    persist: Any  # callable(ctx) -> None, writes stage and counters


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_model_json[T: BaseModel](raw: str, model: type[T]) -> T:
    """Strictly parse model text into a validated object. Raw text is never kept or logged."""
    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        return model.model_validate(json.loads(text))
    except (ValueError, ValidationError) as exc:
        raise RunFailure(FailureCode.INVALID_MODEL_OUTPUT) from exc


def check_citations(draft: DraftOutput, served_ids: set[str]) -> None:
    """Deterministic reference check: citations must be a subset of chunk ids served by the evidence tool.
    A kb: reference, or any id not served in this run, is never citable."""
    for cited in draft.citedChunkIds:
        if cited not in served_ids:
            raise RunFailure(FailureCode.INVALID_CITATION)
    if not draft.citedChunkIds and draft.rootCauseCategory != "UNKNOWN":
        raise RunFailure(FailureCode.INVALID_CITATION)


def _enter(ctx: RunContext, node: str, stage: str | None, deps: Deps) -> None:
    ctx.visited.append(node)
    if stage:
        ctx.stage = stage
        deps.persist(ctx)


def build_graph():
    async def load_context(state: RunState, config: RunnableConfig) -> RunState:
        ctx: RunContext = config["configurable"]["ctx"]
        deps: Deps = config["configurable"]["deps"]
        _enter(ctx, "LOAD_CONTEXT", "LOAD_CONTEXT", deps)
        brk = await deps.tools.get_break(ctx)
        similar = await deps.tools.get_similar_breaks(ctx)
        return {"break_summary": brk, "similar": similar.items}

    async def retrieve(state: RunState, config: RunnableConfig) -> RunState:
        ctx: RunContext = config["configurable"]["ctx"]
        deps: Deps = config["configurable"]["deps"]
        _enter(ctx, "RETRIEVE", "RETRIEVE", deps)
        query = (ctx.note.strip() or state["break_summary"].breakRef)[:MAX_QUERY_CHARS]
        evidence = await deps.tools.get_evidence(ctx, query)
        ctx.served_chunk_ids = {str(c.chunkId) for c in evidence.chunks}
        knowledge = await deps.tools.get_knowledge(ctx, query)
        procedures = knowledge.procedures if knowledge.outcome == "OK" else []
        return {"chunks": evidence.chunks, "procedures": procedures}

    async def draft(state: RunState, config: RunnableConfig) -> RunState:
        ctx: RunContext = config["configurable"]["ctx"]
        deps: Deps = config["configurable"]["deps"]
        _enter(ctx, "DRAFT", "DRAFT", deps)
        ctx.take_model_call()
        reply = await deps.model.draft(
            ctx,
            DraftInput(state["break_summary"], state["similar"], state["chunks"], state["procedures"]),
        )
        deps.persist(ctx)
        return {"draft": parse_model_json(reply.text, DraftOutput)}

    async def reference_check(state: RunState, config: RunnableConfig) -> RunState:
        ctx: RunContext = config["configurable"]["ctx"]
        deps: Deps = config["configurable"]["deps"]
        _enter(ctx, "REFERENCE_CHECK", None, deps)  # reported stage stays DRAFT
        check_citations(state["draft"], ctx.served_chunk_ids)
        return {}

    async def self_check(state: RunState, config: RunnableConfig) -> RunState:
        ctx: RunContext = config["configurable"]["ctx"]
        deps: Deps = config["configurable"]["deps"]
        _enter(ctx, "SELF_CHECK", "SELF_CHECK", deps)
        ctx.take_model_call()
        reply = await deps.model.self_check(ctx, SelfCheckInput(state["draft"], state["chunks"]))
        deps.persist(ctx)
        return {"self_check": parse_model_json(reply.text, SelfCheckOutput)}

    async def done(state: RunState, config: RunnableConfig) -> RunState:
        ctx: RunContext = config["configurable"]["ctx"]
        _enter(ctx, "DONE", None, config["configurable"]["deps"])
        d, s = state["draft"], state["self_check"]
        result = {
            "rootCauseCategory": d.rootCauseCategory,
            "explanation": d.explanation,
            "citedChunkIds": list(dict.fromkeys(d.citedChunkIds)),
            "confidence": d.confidence,
            "selfCheck": s.selfCheck,  # PASS never lowers review; CHALLENGE passes through unchanged
            "selfCheckReason": s.selfCheckReason,
        }
        return {"result": result}

    graph = StateGraph(RunState)
    nodes = [
        ("load_context", load_context),
        ("retrieve", retrieve),
        ("draft", draft),
        ("reference_check", reference_check),
        ("self_check", self_check),
        ("done", done),
    ]
    for name, fn in nodes:
        graph.add_node(name, fn)
    graph.add_edge(START, nodes[0][0])
    for (a, _), (b, _) in zip(nodes, nodes[1:], strict=False):
        graph.add_edge(a, b)
    graph.add_edge(nodes[-1][0], END)
    return graph.compile()
