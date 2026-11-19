# Copyright (c) 2026 the owner of this repository. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary. View only; see LICENSE and AI_USE_POLICY.md.
# Canary: rero-canary-64f662dc07dd453c9360
"""GatewayModel against a respx fake gateway only (Contract B consumer behaviour)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from rero.errors import FailureCode
from rero.gateway_model import GatewayModel
from rero.runner import Runner
from rero.store import RunStore
from rero.tools import ReconcileToolClient

from .conftest import (
    CHUNK_1,
    CONTRACTS_DIR,
    EVIDENCE_MARKER,
    GATEWAY,
    JOB_ID,
    NOTE_MARKER,
    RECONCILE,
    RUN_TOKEN,
    bearer,
    chat_ok,
    make_token,
    new_run,
    start_body,
)

INV_1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
INV_2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
GATEWAY_ENV = {"MODEL_ADAPTER": "gateway", "GATEWAY_BASE_URL": GATEWAY}


def _gw_schema(name: str) -> Draft202012Validator:
    schema = json.loads((CONTRACTS_DIR.parent / "gateway" / f"{name}.v1.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def draft_json(cited: list[str] | None = None) -> str:
    return json.dumps(
        {
            "rootCauseCategory": "TIMING_DIFFERENCE",
            "explanation": "RAW-GATEWAY-EXPLANATION-MARKER",
            "citedChunkIds": [CHUNK_1] if cited is None else cited,
            "confidence": 0.7,
        }
    )


SELF_PASS = json.dumps({"selfCheck": "PASS", "selfCheckReason": "ok"})


def gateway_error(code: str, invocation_id: str | None = None) -> dict:
    body = {"code": code, "message": "safe", "timestamp": "2026-11-18T10:00:00Z"}
    if invocation_id:
        body["invocationId"] = invocation_id
    return body


async def _run(api_factory, upstreams, token=None):
    api = await api_factory(**GATEWAY_ENV)
    resp = await api.start(token=token)
    assert resp.status_code == 202
    return await api.finish(resp.json()["runId"])


async def test_happy_path_forwards_token_and_idempotency_keys(api_factory, upstreams):
    upstreams.chat.mock(
        side_effect=[
            httpx.Response(200, json=chat_ok(draft_json(), INV_1)),
            httpx.Response(200, json=chat_ok(SELF_PASS, INV_2)),
        ]
    )
    token = make_token()
    done = await _run(api_factory, upstreams, token)
    assert done["status"] == "SUCCEEDED"
    assert done["modelCallsUsed"] == 2
    assert done["gatewayInvocationIds"] == [INV_1, INV_2]
    assert done["result"]["citedChunkIds"] == [CHUNK_1]

    calls = upstreams.chat.calls
    assert len(calls) == 2
    assert [c.request.headers["idempotency-key"] for c in calls] == [f"{JOB_ID}:draft", f"{JOB_ID}:selfcheck"]
    for c in calls:
        assert c.request.headers["authorization"] == f"Bearer {token}"  # the incoming service token
        assert not any("tenant" in k.lower() for k in c.request.headers)
        body = json.loads(c.request.content)
        assert "tenantId" not in body
        errors = list(_gw_schema("chat-request").iter_errors(body))  # request matches Contract B schema
        assert not errors, [e.message for e in errors]
        assert 8 <= len(c.request.headers["idempotency-key"]) <= 128


async def test_gateway_responses_used_in_tests_match_contract_b(api_factory, upstreams):
    assert not list(_gw_schema("chat-response").iter_errors(chat_ok("x", INV_1)))
    for code in ("RISK_BLOCKED", "IDEMPOTENCY_UNKNOWN_OUTCOME", "UNKNOWN_OUTCOME", "BUDGET_BLOCKED"):
        assert not list(_gw_schema("error").iter_errors(gateway_error(code, INV_1)))


async def test_idempotent_replay_status_is_accepted(api_factory, upstreams):
    replay = {**chat_ok(draft_json(), INV_2), "status": "IDEMPOTENT_REPLAY", "replayOf": INV_1}
    assert not list(_gw_schema("chat-response").iter_errors(replay))
    upstreams.chat.mock(
        side_effect=[httpx.Response(200, json=replay), httpx.Response(200, json=chat_ok(SELF_PASS, INV_1))]
    )
    assert (await _run(api_factory, upstreams))["status"] == "SUCCEEDED"


@pytest.mark.parametrize(
    "effect",
    [
        httpx.ReadTimeout("slow"),
        httpx.ReadError("reset"),
        httpx.RemoteProtocolError("cut"),
        httpx.Response(504, json=gateway_error("UNKNOWN_OUTCOME", INV_1)),
        httpx.Response(500, json={"oops": True}),
        httpx.Response(502, text="<html>bad gateway</html>"),
        httpx.Response(409, json=gateway_error("IDEMPOTENCY_UNKNOWN_OUTCOME", INV_1)),
    ],
    ids=["read-timeout", "read-error", "protocol", "504", "500", "502-html", "409-unknown"],
)
async def test_timeout_or_5xx_after_send_is_unknown_outcome_with_exactly_one_request(
    api_factory, upstreams, effect
):
    upstreams.chat.mock(side_effect=[effect])
    done = await _run(api_factory, upstreams)
    assert done["status"] == "FAILED"
    assert done["failureCode"] == "UNKNOWN_OUTCOME"
    assert upstreams.chat.call_count == 1  # no retry, and the self-check is never attempted
    assert done["modelCallsUsed"] == 1
    assert done["stage"] == "DRAFT"


async def test_unknown_outcome_records_invocation_id_from_error(api_factory, upstreams):
    upstreams.chat.mock(side_effect=[httpx.Response(504, json=gateway_error("UNKNOWN_OUTCOME", INV_1))])
    done = await _run(api_factory, upstreams)
    assert done["gatewayInvocationIds"] == [INV_1]


@pytest.mark.parametrize(
    ("status", "code", "failure"),
    [
        (422, "RISK_BLOCKED", "MODEL_BLOCKED"),
        (422, "SENSITIVITY_BLOCKED", "MODEL_BLOCKED"),
        (429, "BUDGET_BLOCKED", "MODEL_BLOCKED"),
        (409, "IDEMPOTENCY_IN_PROGRESS", "MODEL_BLOCKED"),
        (422, "IDEMPOTENCY_KEY_REUSED", "MODEL_BLOCKED"),
        (503, "MODEL_UNAVAILABLE", "MODEL_UNAVAILABLE"),
        (503, "NOT_SENT", "MODEL_UNAVAILABLE"),
        (503, "GOVERNANCE_UNAVAILABLE", "MODEL_UNAVAILABLE"),
        (429, "RATE_LIMITED", "MODEL_UNAVAILABLE"),
        (502, "PROVIDER_CONTRACT_FAILURE", "MODEL_UNAVAILABLE"),
        (401, "UNAUTHENTICATED", "MODEL_UNAVAILABLE"),
        (403, "FORBIDDEN", "MODEL_UNAVAILABLE"),
    ],
)
async def test_gateway_error_mapping_never_retries(api_factory, upstreams, status, code, failure):
    upstreams.chat.mock(side_effect=[httpx.Response(status, json=gateway_error(code, INV_1))])
    done = await _run(api_factory, upstreams)
    assert done["failureCode"] == failure
    assert upstreams.chat.call_count == 1
    assert done["gatewayInvocationIds"] == [INV_1]


async def test_connect_error_means_nothing_was_sent(api_factory, upstreams):
    upstreams.chat.mock(side_effect=[httpx.ConnectError("refused")])
    done = await _run(api_factory, upstreams)
    assert done["failureCode"] == "MODEL_UNAVAILABLE"
    assert upstreams.chat.call_count == 1


async def test_self_check_failure_after_draft_is_one_request_each(api_factory, upstreams):
    upstreams.chat.mock(
        side_effect=[
            httpx.Response(200, json=chat_ok(draft_json(), INV_1)),
            httpx.Response(504, json=gateway_error("UNKNOWN_OUTCOME", INV_2)),
        ]
    )
    done = await _run(api_factory, upstreams)
    assert done["failureCode"] == "UNKNOWN_OUTCOME" and done["stage"] == "SELF_CHECK"
    assert upstreams.chat.call_count == 2
    assert done["gatewayInvocationIds"] == [INV_1, INV_2]


async def test_gateway_output_validation_and_citations(api_factory, upstreams):
    upstreams.chat.mock(side_effect=[httpx.Response(200, json=chat_ok("not json", INV_1))])
    assert (await _run(api_factory, upstreams))["failureCode"] == "INVALID_MODEL_OUTPUT"


async def test_gateway_200_without_output_is_invalid_model_output(api_factory, upstreams):
    body = chat_ok("x", INV_1)
    del body["output"]
    upstreams.chat.mock(side_effect=[httpx.Response(200, json=body)])
    assert (await _run(api_factory, upstreams))["failureCode"] == "INVALID_MODEL_OUTPUT"


async def test_gateway_bad_citation_is_invalid_citation(api_factory, upstreams):
    bad = draft_json([str(uuid.uuid4())])
    upstreams.chat.mock(side_effect=[httpx.Response(200, json=chat_ok(bad, INV_1))])
    done = await _run(api_factory, upstreams)
    assert done["failureCode"] == "INVALID_CITATION"
    assert upstreams.chat.call_count == 1  # reference check blocks the self-check call


async def test_gateway_challenge_passes_through(api_factory, upstreams):
    challenge = json.dumps({"selfCheck": "CHALLENGE", "selfCheckReason": "weak"})
    upstreams.chat.mock(
        side_effect=[
            httpx.Response(200, json=chat_ok(draft_json(), INV_1)),
            httpx.Response(200, json=chat_ok(challenge, INV_2)),
        ]
    )
    done = await _run(api_factory, upstreams)
    assert done["status"] == "SUCCEEDED" and done["result"]["selfCheck"] == "CHALLENGE"


async def test_gateway_path_logs_no_payload(api_factory, upstreams, caplog):
    caplog.set_level(logging.DEBUG)
    token = make_token()
    upstreams.chat.mock(
        side_effect=[
            httpx.Response(200, json=chat_ok(draft_json(), INV_1)),
            httpx.Response(200, json=chat_ok(SELF_PASS, INV_2)),
        ]
    )
    await _run(api_factory, upstreams, token)
    for item in (token, RUN_TOKEN, NOTE_MARKER, EVIDENCE_MARKER, "RAW-GATEWAY-EXPLANATION-MARKER"):
        assert item not in caplog.text


async def test_prompt_contains_evidence_but_request_is_the_only_place(api_factory, upstreams):
    upstreams.chat.mock(
        side_effect=[
            httpx.Response(200, json=chat_ok(draft_json(), INV_1)),
            httpx.Response(200, json=chat_ok(SELF_PASS, INV_2)),
        ]
    )
    await _run(api_factory, upstreams)
    draft_body = json.loads(upstreams.chat.calls[0].request.content)
    text = " ".join(m["content"] for m in draft_body["messages"])
    assert EVIDENCE_MARKER in text and CHUNK_1 in text
    assert RUN_TOKEN not in text  # the run token never goes to the model or the gateway
    assert RUN_TOKEN not in upstreams.chat.calls[0].request.headers.get("authorization", "")


async def test_deadline_with_request_in_flight_is_unknown_outcome(tmp_path, upstreams):
    sent: list[int] = []

    async def hang(request):
        sent.append(1)
        await asyncio.sleep(5)
        return httpx.Response(200, json=chat_ok(draft_json(), INV_1))

    upstreams.chat.mock(side_effect=hang)
    store = RunStore(str(tmp_path / "d.db"))
    tools = ReconcileToolClient(RECONCILE, 5)
    gateway = GatewayModel(GATEWAY, 30)
    runner = Runner(store, tools, gateway)
    ctx = new_run(store)
    await runner.execute(ctx, deadline_seconds=0.4)
    row = store.get("tenant-a", ctx.run_id)
    assert row["failure_code"] == FailureCode.UNKNOWN_OUTCOME.value  # sent, outcome unknown
    assert len(sent) == 1
    await tools.aclose()
    await gateway.aclose()


def test_bearer_helper_sanity():
    assert bearer("x") == {"Authorization": "Bearer x"}
    assert start_body()["jobId"] == JOB_ID
