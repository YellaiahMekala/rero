from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

from rero.model import FakeModel
from rero.store import RunStore, now_utc

from .conftest import (
    CHUNK_1,
    EVIDENCE_MARKER,
    JOB_ID,
    NOTE_MARKER,
    PROCEDURE_MARKER,
    RUN_TOKEN,
    assert_valid,
    make_token,
    start_body,
)

OTHER_JOB = "55555555-5555-4555-8555-555555555555"


# ---------------- idempotency ----------------


async def test_new_run_is_202_then_same_input_is_200_with_one_run(api, upstreams):
    first = await api.start()
    assert first.status_code == 202
    run_id = first.json()["runId"]
    done = await api.finish(run_id)
    assert done["status"] == "SUCCEEDED"
    calls_after_first = len(upstreams.tool_requests())

    again = await api.start()
    assert again.status_code == 200
    assert again.json()["runId"] == run_id
    await api.app.state.runner.wait_all()
    assert len(upstreams.tool_requests()) == calls_after_first  # no second run, no second tool call


async def test_run_token_is_excluded_from_the_hash(api):
    first = await api.start()
    other_token = RUN_TOKEN.replace("cnVuLXRva2VuLXNpZw", "b3RoZXItc2ln")
    body = {**start_body(), "runToken": other_token}
    again = await api.start(body)
    assert again.status_code == 200
    assert again.json()["runId"] == first.json()["runId"]


async def test_same_key_different_input_is_409(api):
    await api.start()
    for changed in (
        start_body(note="DIFFERENT-NOTE-X"),
        start_body(maxToolCalls=9),
        start_body(deadlineSeconds=61),
    ):
        r = await api.start(changed)
        assert r.status_code == 409
        assert r.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert "DIFFERENT-NOTE-X" not in r.text


async def test_same_key_other_tenant_is_a_separate_run(api):
    a = await api.start(token=make_token(tenant="tenant-a"))
    b = await api.start(token=make_token(tenant="tenant-b"))
    assert (a.status_code, b.status_code) == (202, 202)
    assert a.json()["runId"] != b.json()["runId"]


async def test_idempotency_key_must_equal_job_id(api):
    for key in ("not-the-job-id", None):
        headers = {"Authorization": f"Bearer {make_token()}"}
        if key:
            headers["Idempotency-Key"] = key
        r = await api.client.post("/v1/recommendations", json=start_body(), headers=headers)
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_REQUEST"
        assert_valid("error", r.json())


async def test_invalid_bodies_are_400_without_echo(api):
    bad_bodies = [
        {**start_body(), "limits": {"maxToolCalls": 0, "maxModelCalls": 2, "deadlineSeconds": 60}},
        {**start_body(), "limits": {"maxToolCalls": 8, "maxModelCalls": 3, "deadlineSeconds": 60}},
        {**start_body(), "limits": {"maxToolCalls": 8, "maxModelCalls": 2, "deadlineSeconds": 4}},
        {**start_body(), "limits": {"maxToolCalls": 8, "maxModelCalls": 2, "deadlineSeconds": 121}},
        {**start_body(), "analystNote": "x" * 2001},
        {**start_body(), "jobId": "not-a-uuid"},
        {**start_body(), "runToken": "bad token"},
    ]
    for body in bad_bodies:
        headers = {"Authorization": f"Bearer {make_token()}", "Idempotency-Key": JOB_ID}
        r = await api.client.post("/v1/recommendations", json=body, headers=headers)
        assert r.status_code == 400, body
        assert_valid("error", r.json())
    r = await api.client.post(
        "/v1/recommendations",
        content=b"{not json",
        headers={"Authorization": f"Bearer {make_token()}", "Idempotency-Key": JOB_ID},
    )
    assert r.status_code == 400


async def test_empty_analyst_note_is_accepted(api):
    r = await api.start(start_body(note=""))
    assert r.status_code == 202
    assert (await api.finish(r.json()["runId"]))["status"] == "SUCCEEDED"


# ---------------- tenant isolation on GET ----------------


async def test_get_other_tenants_run_is_404(api):
    run_id = (await api.start(token=make_token(tenant="tenant-a"))).json()["runId"]
    own = await api.get(run_id, make_token(tenant="tenant-a"))
    assert own.status_code == 200
    other = await api.get(run_id, make_token(tenant="tenant-b"))
    assert other.status_code == 404
    assert other.json()["code"] == "RUN_NOT_FOUND"


async def test_get_unknown_and_malformed_run_ids(api):
    assert (await api.get("66666666-6666-4666-8666-666666666666")).status_code == 404
    assert (await api.get("nope")).status_code == 400


# ---------------- retention: nothing sensitive is persisted or logged ----------------


def _db_bytes(path: Path) -> bytes:
    data = b""
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            data += p.read_bytes()
    return data


async def test_db_file_holds_no_tokens_prompts_payloads_or_raw_output(api, tmp_path, upstreams):
    token = make_token()
    run_id = (await api.start(token=token)).json()["runId"]
    done = await api.finish(run_id)
    assert done["status"] == "SUCCEEDED"
    await api.app.state.runner.wait_all()

    data = _db_bytes(tmp_path / "rero.db")
    assert CHUNK_1.encode() in data  # positive control: the validated result is stored
    forbidden = [
        token,
        RUN_TOKEN,
        "eyJ",  # any JWT header fragment
        NOTE_MARKER,
        EVIDENCE_MARKER,
        PROCEDURE_MARKER,
        "FAKE-RAW-OUTPUT-MARKER",  # appears only in the raw model output
        "untrusted data",  # prompt text
    ]
    for item in forbidden:
        assert item.encode() not in data, item


async def test_failed_runs_also_store_no_payload(api_factory, tmp_path):
    api = await api_factory(model=FakeModel(draft_mode="bad_citation"))
    run_id = (await api.start()).json()["runId"]
    assert (await api.finish(run_id))["failureCode"] == "INVALID_CITATION"
    data = _db_bytes(tmp_path / "rero.db")
    for item in (NOTE_MARKER, EVIDENCE_MARKER, RUN_TOKEN, "FAKE-RAW-OUTPUT-MARKER"):
        assert item.encode() not in data


def test_schema_has_no_column_for_sensitive_data(tmp_path):
    store = RunStore(str(tmp_path / "s.db"))
    store.init()
    with sqlite3.connect(tmp_path / "s.db") as c:
        columns = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
    assert not {
        c for c in columns if any(w in c for w in ("token", "prompt", "note", "payload", "raw", "evidence"))
    }


async def test_logs_contain_no_payload(api, caplog, upstreams):
    caplog.set_level(logging.DEBUG)
    token = make_token()
    run_id = (await api.start(token=token)).json()["runId"]
    await api.finish(run_id)
    await api.app.state.runner.wait_all()
    assert caplog.records, "expected some log output to scan"
    for item in (
        token,
        RUN_TOKEN,
        NOTE_MARKER,
        EVIDENCE_MARKER,
        PROCEDURE_MARKER,
        "FAKE-RAW-OUTPUT-MARKER",
        "Bearer",
    ):
        assert item not in caplog.text, item


# ---------------- TTL purge and restart ----------------


def test_purge_removes_only_expired_rows(tmp_path):
    store = RunStore(str(tmp_path / "p.db"))
    store.init()
    short, _ = store.create_or_get("tenant-a", JOB_ID, "h1", JOB_ID, ttl_seconds=60)
    long, _ = store.create_or_get("tenant-a", OTHER_JOB, "h2", OTHER_JOB, ttl_seconds=86400)
    assert store.purge_expired(now_utc()) == 0
    assert store.purge_expired(now_utc() + timedelta(seconds=120)) == 1
    assert store.get("tenant-a", short["run_id"]) is None
    assert store.get("tenant-a", long["run_id"]) is not None


def test_expired_run_is_invisible_and_key_is_reusable(tmp_path):
    store = RunStore(str(tmp_path / "e.db"))
    store.init()
    first, _ = store.create_or_get("tenant-a", JOB_ID, "h1", JOB_ID, ttl_seconds=1)
    import time

    time.sleep(1.1)
    assert store.get("tenant-a", first["run_id"]) is None
    second, outcome = store.create_or_get("tenant-a", JOB_ID, "h-new", JOB_ID, ttl_seconds=60)
    assert outcome == "CREATED"
    assert second["run_id"] != first["run_id"]


async def test_startup_marks_running_runs_failed_restarted(api_factory, tmp_path):
    store = RunStore(str(tmp_path / "rero.db"))
    store.init()
    running, _ = store.create_or_get("tenant-a", JOB_ID, "h", JOB_ID, ttl_seconds=3600)
    done, _ = store.create_or_get("tenant-a", OTHER_JOB, "h2", OTHER_JOB, ttl_seconds=3600)
    store.finish_failed(
        done["run_id"],
        __import__("rero.errors", fromlist=["x"]).FailureCode.TOOL_REJECTED,
        "RETRIEVE",
        1,
        0,
        [],
    )

    api = await api_factory()  # lifespan startup runs recovery
    body = (await api.get(running["run_id"])).json()
    assert body["status"] == "FAILED"
    assert body["failureCode"] == "RESTARTED"
    assert body["finishedAt"] is not None
    assert (await api.get(done["run_id"])).json()["failureCode"] == "TOOL_REJECTED"  # untouched
