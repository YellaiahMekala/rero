"""Structural guarantees: two HTTP clients, no EKA client, read-only tool client."""

from __future__ import annotations

import re
from pathlib import Path

import rero
from rero.tools import ReconcileToolClient

SRC = Path(rero.__file__).parent
SOURCES = {p.name: p.read_text(encoding="utf-8") for p in SRC.glob("*.py")}


def test_exactly_two_http_clients_reconcile_and_gateway():
    holders = {name: len(re.findall(r"httpx\.(?:Async)?Client\(", text)) for name, text in SOURCES.items()}
    assert {n: c for n, c in holders.items() if c} == {"tools.py": 1, "gateway_model.py": 1}
    for text in SOURCES.values():
        assert not re.search(r"\b(requests|aiohttp|urllib3|http\.client)\b", text)


def test_no_knowledge_base_service_client_exists():
    for name, text in SOURCES.items():
        assert not re.search(r"\beka\b|eka-api|docchat", text, re.IGNORECASE), name
    assert "EKA_" not in " ".join(SOURCES.values())


def test_tool_client_exposes_get_only():
    public = {n for n in dir(ReconcileToolClient) if not n.startswith("_")}
    assert public == {"aclose", "get_break", "get_evidence", "get_similar_breaks", "get_knowledge"}
    tools_src = SOURCES["tools.py"]
    assert re.findall(r"self\._client\.(\w+)\(", tools_src) == ["aclose", "get"]
    assert not re.search(r"\.(post|put|patch|delete|request|send|stream)\(", tools_src)


async def test_tool_client_only_ever_issues_get(api, upstreams):
    run_id = (await api.start()).json()["runId"]
    await api.finish(run_id)
    calls = [
        c
        for r in (upstreams.brk, upstreams.evidence, upstreams.similar, upstreams.knowledge)
        for c in r.calls
    ]
    assert len(calls) == 4
    assert {c.request.method for c in calls} == {"GET"}
    # every request the mocked network saw towards reconcile-ai was a GET
    assert {c.request.method for c in upstreams.router.calls if "reconcile.test" in str(c.request.url)} == {
        "GET"
    }
    assert all("/internal/rero/v1/jobs/" in c.request.url.path for c in calls)


async def test_no_write_route_is_even_mocked(upstreams):
    # the fake only knows GET routes; a POST to reconcile-ai would raise AllMockedAssertionError
    import httpx
    import pytest
    import respx

    with pytest.raises(respx.models.AllMockedAssertionError):
        async with httpx.AsyncClient() as client:
            await client.post("http://reconcile.test/internal/rero/v1/jobs/x/break")
