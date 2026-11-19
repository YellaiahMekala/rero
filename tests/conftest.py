# Copyright (c) 2026 the owner of this repository. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary. View only; see LICENSE and AI_USE_POLICY.md.
# Canary: rero-canary-64f662dc07dd453c9360
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio
import respx
from asgi_lifespan import LifespanManager
from cryptography.hazmat.primitives.asymmetric import rsa
from jsonschema import Draft202012Validator, FormatChecker
from jwt.algorithms import RSAAlgorithm

from rero.app import create_app
from rero.config import Settings
from rero.models import Limits
from rero.runctx import RunContext
from rero.store import RunStore

ISSUER = "https://idp.test/realms/platform"
RECONCILE = "http://reconcile.test"
GATEWAY = "http://gateway.test"
JOB_ID = "11111111-1111-4111-8111-111111111111"
CHUNK_1 = "33333333-3333-4333-8333-333333333333"
CHUNK_2 = "44444444-4444-4444-8444-444444444444"
RUN_TOKEN = "eyJhbGciOiJFUzI1NiJ9.eyJydW4iOiJ0b2tlbi1tYXJrZXIifQ.cnVuLXRva2VuLXNpZw"
NOTE_MARKER = "NOTE-MARKER-settlement-late"
EVIDENCE_MARKER = "EVIDENCE-MARKER-value-date-moved"
PROCEDURE_MARKER = "PROCEDURE-MARKER-timing-steps"

# Vendored copies of platform/contracts/rero (Contract C) and contracts/gateway (Contract B).
# Keep them in sync with the platform hub when a contract changes.
_DEFAULT_CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "rero"
CONTRACTS_DIR = Path(os.environ.get("RERO_CONTRACTS_DIR", _DEFAULT_CONTRACTS)).resolve()


# ---------------- contracts ----------------
def _schema(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS_DIR / f"{name}.v1.json").read_text(encoding="utf-8"))


SCHEMA_NAMES = [
    "start-request",
    "run",
    "error",
    "internal-break",
    "internal-evidence",
    "internal-similar",
    "internal-knowledge",
]


def validator(name: str) -> Draft202012Validator:
    schema = _schema(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def assert_valid(name: str, instance: Any) -> None:
    errors = sorted(validator(name).iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, f"{name}: " + "; ".join(e.message for e in errors[:3])


def example(name: str) -> Any:
    return json.loads((CONTRACTS_DIR / "examples" / f"{name}.json").read_text(encoding="utf-8"))


# ---------------- auth ----------------
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "test-key-1"


def jwks_document() -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(_KEY.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


class FakeJwks:
    """Key resolver backed by a locally generated JWKS (no network)."""

    def __init__(self) -> None:
        self._set = jwt.PyJWKSet.from_dict(jwks_document())

    def signing_key(self, token: str) -> Any:
        kid = jwt.get_unverified_header(token).get("kid")
        for key in self._set.keys:
            if key.key_id == kid:
                return key.key
        raise jwt.PyJWKClientError("unknown kid")


def make_token(
    *,
    tenant: str | None = "tenant-a",
    audiences: list[str] | None = None,
    roles: list[str] | None = None,
    realm_roles: bool = True,
    issuer: str = ISSUER,
    expires_in: int = 300,
    signing_key: Any = None,
    algorithm: str = "RS256",
    omit: tuple[str, ...] = (),
) -> str:
    now = int(time.time())
    roles = ["RERO_CALLER", "GATEWAY_CONSUMER"] if roles is None else roles
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": "svc-reconcile-ai",
        "aud": ["rero-api", "ai-gateway"] if audiences is None else audiences,
        "iat": now,
        "exp": now + expires_in,
    }
    if tenant is not None:
        claims["tenant_id"] = tenant
    if realm_roles:
        claims["realm_access"] = {"roles": roles}
    else:
        claims["roles"] = roles
    for name in omit:
        claims.pop(name, None)
    return jwt.encode(claims, signing_key or _KEY, algorithm=algorithm, headers={"kid": KID})


def foreign_key() -> Any:
    return _OTHER_KEY


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------- settings and app ----------------
def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "RERO_ISSUER": ISSUER,
        "RERO_JWKS_URI": "https://idp.test/realms/platform/certs",
        "RERO_REQUIRED_AUDIENCES": "rero-api,ai-gateway",
        "RECONCILE_INTERNAL_BASE_URL": RECONCILE,
        "MODEL_ADAPTER": "fake",
        "RERO_RESULT_TTL": "24h",
        "RERO_DB_PATH": str(tmp_path / "rero.db"),
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


# ---------------- fake upstreams ----------------
@dataclass
class Upstreams:
    router: respx.MockRouter
    brk: respx.Route
    evidence: respx.Route
    similar: respx.Route
    knowledge: respx.Route
    chat: respx.Route

    def tool_requests(self) -> list[httpx.Request]:
        return [c.request for r in (self.brk, self.evidence, self.similar, self.knowledge) for c in r.calls]


def break_body() -> dict[str, Any]:
    return example("internal-break.valid")


def evidence_body() -> dict[str, Any]:
    return {
        "chunks": [
            {"chunkId": CHUNK_1, "source": "settlement-feed", "text": EVIDENCE_MARKER},
            {"chunkId": CHUNK_2, "source": "ops-log", "text": "Masked ops entry."},
        ]
    }


def similar_body() -> dict[str, Any]:
    return example("internal-similar.valid")


def knowledge_body() -> dict[str, Any]:
    return {
        "outcome": "OK",
        "procedures": [{"ref": "kb:proc-timing", "title": "Timing", "text": PROCEDURE_MARKER}],
    }


def chat_ok(output: str, invocation_id: str | None = None) -> dict[str, Any]:
    return {
        "invocationId": invocation_id or str(uuid.uuid4()),
        "correlationId": str(uuid.uuid4()),
        "status": "OK",
        "output": output,
        "usage": {"inputTokens": 10, "outputTokens": 5, "source": "PROVIDER_REPORTED"},
    }


@pytest.fixture
def upstreams() -> Any:
    """Fake reconcile-ai internal tools and fake ai-gateway. Any other host fails the test."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        base = re.escape(RECONCILE) + r"/internal/rero/v1/jobs/[^/]+/"
        brk = router.get(url__regex=base + r"break$").respond(200, json=break_body())
        evidence = router.get(url__regex=base + r"evidence").respond(200, json=evidence_body())
        similar = router.get(url__regex=base + r"similar-breaks$").respond(200, json=similar_body())
        knowledge = router.get(url__regex=base + r"knowledge").respond(200, json=knowledge_body())
        chat = router.post(f"{GATEWAY}/api/v1/chat")
        yield Upstreams(router, brk, evidence, similar, knowledge, chat)


# ---------------- API client ----------------
def start_body(job_id: str = JOB_ID, note: str = NOTE_MARKER, **limits: int) -> dict[str, Any]:
    lim = {"maxToolCalls": 8, "maxModelCalls": 2, "deadlineSeconds": 60}
    lim.update(limits)
    return {"jobId": job_id, "analystNote": note, "limits": lim, "runToken": RUN_TOKEN}


@dataclass
class Api:
    client: httpx.AsyncClient
    app: Any

    async def start(
        self, body: dict[str, Any] | None = None, token: str | None = None, key: str | None = None
    ):
        body = body or start_body()
        headers = bearer(token or make_token())
        headers["Idempotency-Key"] = key or body["jobId"]
        return self.checked(await self.client.post("/v1/recommendations", json=body, headers=headers))

    async def get(self, run_id: str, token: str | None = None):
        response = await self.client.get(
            f"/v1/recommendations/{run_id}", headers=bearer(token or make_token())
        )
        return self.checked(response)

    @staticmethod
    def checked(response: httpx.Response) -> httpx.Response:
        """Provider contract check: every body must match run.v1 (2xx) or error.v1 (non-2xx)."""
        assert_valid("run" if response.status_code < 300 else "error", response.json())
        return response

    async def finish(self, run_id: str, token: str | None = None) -> dict[str, Any]:
        for _ in range(200):
            body = (await self.get(run_id, token)).json()
            if body["status"] != "RUNNING":
                return body
            await asyncio.sleep(0.02)
        raise AssertionError("run did not finish")


@pytest_asyncio.fixture
async def api_factory(tmp_path: Path, upstreams: Upstreams):
    managers: list[LifespanManager] = []
    clients: list[httpx.AsyncClient] = []

    async def build(model: Any = None, **env: Any) -> Api:
        cfg = make_settings(tmp_path, **env)
        app = create_app(cfg, key_resolver=FakeJwks(), model=model)
        manager = LifespanManager(app)
        await manager.__aenter__()
        managers.append(manager)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rero.test")
        clients.append(client)
        return Api(client, app)

    yield build
    for client in clients:
        await client.aclose()
    for manager in managers:
        await manager.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def api(api_factory):
    return await api_factory()


# ---------------- direct runner helpers ----------------
def new_run(store: RunStore, *, max_tools: int = 8, deadline: int = 60, job_id: str = JOB_ID) -> RunContext:
    store.init()
    row, _ = store.create_or_get("tenant-a", job_id, "hash-" + job_id, job_id, 3600)
    return RunContext(
        run_id=row["run_id"],
        tenant_id="tenant-a",
        job_id=job_id,
        limits=Limits(maxToolCalls=max_tools, maxModelCalls=2, deadlineSeconds=deadline),
        note=NOTE_MARKER,
        run_token=RUN_TOKEN,
        service_token="svc-token-marker.payload.sig",
    )
