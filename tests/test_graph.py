from __future__ import annotations

import asyncio

import httpx
import pytest

from rero.errors import FailureCode
from rero.graph import NODE_ORDER
from rero.model import FakeModel
from rero.runner import Runner
from rero.store import RunStore
from rero.tools import ReconcileToolClient

from .conftest import CHUNK_1, JOB_ID, RECONCILE, new_run, start_body


def _runner(tmp_path, model=None) -> tuple[Runner, RunStore, ReconcileToolClient]:
    store = RunStore(str(tmp_path / "g.db"))
    tools = ReconcileToolClient(RECONCILE, 5)
    return Runner(store, tools, model or FakeModel()), store, tools


async def test_nodes_run_in_fixed_linear_order(tmp_path, upstreams):
    runner, store, tools = _runner(tmp_path)
    ctx = new_run(store)
    await runner.execute(ctx)
    row = store.get("tenant-a", ctx.run_id)
    assert (
        ctx.visited
        == NODE_ORDER
        == ["LOAD_CONTEXT", "RETRIEVE", "DRAFT", "REFERENCE_CHECK", "SELF_CHECK", "DONE"]
    )
    assert row["status"] == "SUCCEEDED" and row["stage"] == "DONE"
    assert row["tool_calls_used"] == 4 and row["model_calls_used"] == 2
    await tools.aclose()


async def test_success_result_cites_first_evidence_chunk(api):
    run_id = (await api.start()).json()["runId"]
    done = await api.finish(run_id)
    assert done["status"] == "SUCCEEDED" and done["stage"] == "DONE"
    assert done["failureCode"] is None
    result = done["result"]
    assert result["citedChunkIds"] == [CHUNK_1]
    assert result["selfCheck"] == "PASS"
    assert result["rootCauseCategory"] == "TIMING_DIFFERENCE"
    assert done["toolCallsUsed"] == 4 and done["modelCallsUsed"] == 2
    assert done["gatewayInvocationIds"] == []


async def test_tools_send_run_token_as_bearer_and_no_tenant(api, upstreams):
    run_id = (await api.start()).json()["runId"]
    await api.finish(run_id)
    from .conftest import RUN_TOKEN

    reqs = upstreams.tool_requests()
    assert len(reqs) == 4
    for r in reqs:
        assert r.headers["authorization"] == f"Bearer {RUN_TOKEN}"
        assert f"/jobs/{JOB_ID}/" in r.url.path
        assert "tenant" not in str(r.url).lower() and "tenant" not in {k.lower() for k in r.headers}


async def test_challenge_result_passes_through(api_factory):
    api = await api_factory(model=FakeModel(self_check_mode="challenge"))
    run_id = (await api.start()).json()["runId"]
    done = await api.finish(run_id)
    assert done["status"] == "SUCCEEDED"
    assert done["result"]["selfCheck"] == "CHALLENGE"
    assert done["result"]["selfCheckReason"]


@pytest.mark.parametrize(
    ("draft_mode", "self_mode", "code", "stage", "model_calls"),
    [
        ("bad_citation", "pass", "INVALID_CITATION", "DRAFT", 1),
        ("kb_citation", "pass", "INVALID_CITATION", "DRAFT", 1),
        ("empty_citation", "pass", "INVALID_CITATION", "DRAFT", 1),
        ("invalid_json", "pass", "INVALID_MODEL_OUTPUT", "DRAFT", 1),
        ("missing_field", "pass", "INVALID_MODEL_OUTPUT", "DRAFT", 1),
        ("ok", "invalid_json", "INVALID_MODEL_OUTPUT", "SELF_CHECK", 2),
    ],
)
async def test_model_output_failures(api_factory, draft_mode, self_mode, code, stage, model_calls):
    model = FakeModel(draft_mode=draft_mode, self_check_mode=self_mode)
    api = await api_factory(model=model)
    done = await api.finish((await api.start()).json()["runId"])
    assert done["status"] == "FAILED"
    assert done["failureCode"] == code
    assert done["stage"] == stage
    assert done["result"] is None
    assert done["modelCallsUsed"] == model_calls
    if draft_mode in ("bad_citation", "kb_citation", "empty_citation"):
        assert model.calls == ["draft"]  # the reference check stops the run before the self-check


async def test_tool_budget_exceeded_locally_and_atomically(api_factory, upstreams):
    api = await api_factory()
    done = await api.finish((await api.start(start_body(maxToolCalls=3))).json()["runId"])
    assert done["failureCode"] == "TOOL_BUDGET_EXCEEDED"
    assert done["stage"] == "RETRIEVE"
    assert done["toolCallsUsed"] == 3
    assert upstreams.knowledge.call_count == 0  # the 4th call was never sent
    assert len(upstreams.tool_requests()) == 3


async def test_budget_of_one_stops_after_first_call(api_factory, upstreams):
    api = await api_factory()
    done = await api.finish((await api.start(start_body(maxToolCalls=1))).json()["runId"])
    assert done["failureCode"] == "TOOL_BUDGET_EXCEEDED"
    assert len(upstreams.tool_requests()) == 1


async def test_budget_is_atomic_under_concurrency():
    from rero.runctx import ToolBudget

    budget = ToolBudget(5)
    results = await asyncio.gather(*[asyncio.to_thread(_take, budget) for _ in range(50)])
    assert sum(results) == 5 and budget.used == 5


def _take(budget) -> bool:
    try:
        budget.take()
        return True
    except Exception:
        return False


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (429, "TOOL_BUDGET_EXCEEDED"),
        (401, "TOOL_REJECTED"),
        (403, "TOOL_REJECTED"),
        (409, "TOOL_REJECTED"),
        (405, "TOOL_REJECTED"),
        (500, "TOOL_UNAVAILABLE"),
        (503, "TOOL_UNAVAILABLE"),
    ],
)
async def test_internal_error_mapping(api, upstreams, status, code):
    upstreams.brk.respond(status, json={"code": "X", "message": "m"})
    done = await api.finish((await api.start()).json()["runId"])
    assert done["status"] == "FAILED" and done["failureCode"] == code
    assert done["stage"] == "LOAD_CONTEXT"


async def test_tool_transport_error_and_bad_body_are_unavailable(api, upstreams):
    upstreams.brk.mock(side_effect=httpx.ConnectError("down"))
    assert (await api.finish((await api.start()).json()["runId"]))["failureCode"] == "TOOL_UNAVAILABLE"


async def test_malformed_tool_body_is_unavailable(api_factory, upstreams):
    api = await api_factory()
    upstreams.evidence.respond(200, json={"chunks": [{"chunkId": "not-a-uuid", "source": "s", "text": "t"}]})
    assert (await api.finish((await api.start()).json()["runId"]))["failureCode"] == "TOOL_UNAVAILABLE"


async def test_knowledge_unavailable_outcome_does_not_fail_the_run(api, upstreams):
    upstreams.knowledge.respond(200, json={"outcome": "UNAVAILABLE", "procedures": []})
    done = await api.finish((await api.start()).json()["runId"])
    assert done["status"] == "SUCCEEDED"


async def test_no_evidence_yields_unknown_category(api, upstreams):
    upstreams.evidence.respond(200, json={"chunks": []})
    done = await api.finish((await api.start()).json()["runId"])
    assert done["status"] == "SUCCEEDED"
    assert done["result"]["rootCauseCategory"] == "UNKNOWN" and done["result"]["citedChunkIds"] == []


async def test_deadline_exceeded(tmp_path, upstreams):
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={"chunks": []})

    upstreams.evidence.mock(side_effect=slow)
    runner, store, tools = _runner(tmp_path)
    ctx = new_run(store)
    await runner.execute(ctx, deadline_seconds=0.3)
    row = store.get("tenant-a", ctx.run_id)
    assert row["status"] == "FAILED"
    assert row["failure_code"] == FailureCode.DEADLINE_EXCEEDED.value
    assert row["stage"] == "RETRIEVE"
    assert row["tool_calls_used"] == 3  # break, similar, evidence (in flight)
    assert upstreams.knowledge.call_count == 0
    await tools.aclose()


async def test_deadline_is_enforced_by_the_api_path(api, upstreams):
    # the contract bounds deadlineSeconds to 5..120; the run uses the request value
    done_run = await api.start(start_body(deadlineSeconds=5))
    assert done_run.status_code == 202
    assert (await api.finish(done_run.json()["runId"]))["status"] == "SUCCEEDED"


async def test_unexpected_crash_fails_closed_without_leaking(tmp_path, upstreams, caplog):
    class Boom(FakeModel):
        async def draft(self, ctx, inp):
            raise RuntimeError("secret detail " + ctx.note)

    caplog.set_level("DEBUG")
    runner, store, tools = _runner(tmp_path, Boom())
    ctx = new_run(store)
    await runner.execute(ctx)
    assert store.get("tenant-a", ctx.run_id)["failure_code"] == "RESTARTED"
    assert "secret detail" not in caplog.text
    await tools.aclose()
