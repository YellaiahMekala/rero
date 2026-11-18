from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

RootCause = Literal[
    "TIMING_DIFFERENCE",
    "FX_RATE_DIFFERENCE",
    "FEE_OR_CHARGE",
    "DUPLICATE_ENTRY",
    "MISSING_ENTRY",
    "DATA_ENTRY_ERROR",
    "PARTIAL_SETTLEMENT",
    "UNKNOWN",
]


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    maxToolCalls: int = Field(ge=1, le=20)  # noqa: N815 - wire name
    maxModelCalls: Literal[2]  # noqa: N815
    deadlineSeconds: int = Field(ge=5, le=120)  # noqa: N815


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jobId: UUID  # noqa: N815
    analystNote: Annotated[str, StringConstraints(max_length=2000)]  # noqa: N815
    limits: Limits
    runToken: Annotated[  # noqa: N815
        str, StringConstraints(max_length=4096, pattern=r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
    ]


def canonical_hash(tenant_id: str, request: StartRequest) -> str:
    """SHA-256 over tenant_id, jobId, analystNote, limits (sorted keys). runToken is excluded."""
    material = {
        "tenant_id": tenant_id,
        "jobId": str(request.jobId),
        "analystNote": request.analystNote,
        "limits": request.limits.model_dump(),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---- model outputs (validated; extra keys from a model are ignored and never stored) ----


class DraftOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    rootCauseCategory: RootCause  # noqa: N815
    explanation: Annotated[str, StringConstraints(max_length=2000)]
    citedChunkIds: Annotated[list[str], Field(max_length=20)]  # noqa: N815
    confidence: Annotated[float, Field(ge=0, le=1)]


class SelfCheckOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    selfCheck: Literal["PASS", "CHALLENGE"]  # noqa: N815
    selfCheckReason: Annotated[str, StringConstraints(max_length=500)]  # noqa: N815


# ---- tool responses from reconcile-ai (unknown keys tolerated) ----


class _Tolerant(BaseModel):
    model_config = ConfigDict(extra="ignore")


class BreakSummary(_Tolerant):
    breakRef: str  # noqa: N815
    variance: str | float | int
    currency: str
    sourceA: str  # noqa: N815
    sourceB: str  # noqa: N815
    status: str


class EvidenceChunk(_Tolerant):
    chunkId: UUID  # noqa: N815
    source: str
    text: str


class EvidenceResponse(_Tolerant):
    chunks: list[EvidenceChunk]


class SimilarItem(_Tolerant):
    breakRef: str  # noqa: N815
    rootCauseCategory: str  # noqa: N815
    resolvedAt: str  # noqa: N815


class SimilarResponse(_Tolerant):
    items: list[SimilarItem]


class Procedure(_Tolerant):
    ref: str
    title: str
    text: str


class KnowledgeResponse(_Tolerant):
    outcome: Literal["OK", "EMPTY", "UNAVAILABLE", "REJECTED"]
    procedures: list[Procedure]
