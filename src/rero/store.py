# Copyright (c) 2026 the owner of this repository. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary. View only; see LICENSE and AI_USE_POLICY.md.
# Canary: rero-canary-64f662dc07dd453c9360
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from .errors import FailureCode

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  job_id TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  failure_code TEXT,
  tool_calls_used INTEGER NOT NULL DEFAULT 0,
  model_calls_used INTEGER NOT NULL DEFAULT 0,
  gateway_invocation_ids TEXT NOT NULL DEFAULT '[]',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  result_json TEXT,
  expires_at TEXT NOT NULL,
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_runs_expires ON runs (expires_at);
"""


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class RunStore:
    """SQLite persistence. Stores only the fields the contract Retention clause allows:
    run metadata, validated result, counts, timing and gateway invocation ids.
    There is no column for tokens, prompts, tool payloads, the analyst note or raw model output."""

    def __init__(self, path: str) -> None:
        self._path = path

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def init(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def ping(self) -> bool:
        try:
            with self._conn() as c:
                c.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- idempotent create ----
    def create_or_get(
        self, tenant_id: str, key: str, input_hash: str, job_id: str, ttl_seconds: int
    ) -> tuple[dict[str, Any], str]:
        """Returns (row, outcome) with outcome in CREATED | EXISTING | CONFLICT."""
        now = now_utc()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("DELETE FROM runs WHERE expires_at <= ?", (iso(now),))
                row = c.execute(
                    "SELECT * FROM runs WHERE tenant_id = ? AND idempotency_key = ?", (tenant_id, key)
                ).fetchone()
                if row is not None:
                    c.execute("COMMIT")
                    return dict(row), ("EXISTING" if row["input_hash"] == input_hash else "CONFLICT")
                run_id = str(uuid.uuid4())
                expires = iso(now + timedelta(seconds=ttl_seconds))
                c.execute(
                    "INSERT INTO runs (run_id, tenant_id, idempotency_key, input_hash, job_id, status, stage,"
                    " started_at, expires_at) VALUES (?,?,?,?,?,'RUNNING','LOAD_CONTEXT',?,?)",
                    (run_id, tenant_id, key, input_hash, job_id, iso(now), expires),
                )
                row = c.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                c.execute("COMMIT")
                return dict(row), "CREATED"
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise

    def get(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM runs WHERE tenant_id = ? AND run_id = ? AND expires_at > ?",
                (tenant_id, run_id, iso(now_utc())),
            ).fetchone()
        return dict(row) if row else None

    # ---- progress ----
    def update_progress(
        self, run_id: str, stage: str, tool_calls: int, model_calls: int, gateway_ids: list[str]
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET stage=?, tool_calls_used=?, model_calls_used=?, gateway_invocation_ids=?"
                " WHERE run_id=? AND status='RUNNING'",
                (stage, tool_calls, model_calls, json.dumps(gateway_ids), run_id),
            )

    def finish_success(
        self, run_id: str, result: dict[str, Any], tool_calls: int, model_calls: int, gateway_ids: list[str]
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET status='SUCCEEDED', stage='DONE', failure_code=NULL, result_json=?,"
                " tool_calls_used=?, model_calls_used=?, gateway_invocation_ids=?, finished_at=?"
                " WHERE run_id=? AND status='RUNNING'",
                (
                    json.dumps(result),
                    tool_calls,
                    model_calls,
                    json.dumps(gateway_ids),
                    iso(now_utc()),
                    run_id,
                ),
            )

    def finish_failed(
        self,
        run_id: str,
        code: FailureCode,
        stage: str,
        tool_calls: int,
        model_calls: int,
        gateway_ids: list[str],
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET status='FAILED', stage=?, failure_code=?, result_json=NULL,"
                " tool_calls_used=?, model_calls_used=?, gateway_invocation_ids=?, finished_at=?"
                " WHERE run_id=? AND status='RUNNING'",
                (stage, code.value, tool_calls, model_calls, json.dumps(gateway_ids), iso(now_utc()), run_id),
            )

    # ---- lifecycle ----
    def mark_running_failed(self) -> int:
        """Startup recovery: v0 runs in the API process, so a RUNNING row at startup is dead."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE runs SET status='FAILED', failure_code=?, finished_at=? WHERE status='RUNNING'",
                (FailureCode.RESTARTED.value, iso(now_utc())),
            )
            return cur.rowcount

    def purge_expired(self, now: datetime | None = None) -> int:
        with self._conn() as c:
            cur = c.execute("DELETE FROM runs WHERE expires_at <= ?", (iso(now or now_utc()),))
            return cur.rowcount


def to_run_view(row: dict[str, Any]) -> dict[str, Any]:
    """DB row to the Contract C Run body (camelCase)."""
    return {
        "runId": row["run_id"],
        "jobId": row["job_id"],
        "status": row["status"],
        "stage": row["stage"],
        "failureCode": row["failure_code"],
        "toolCallsUsed": row["tool_calls_used"],
        "modelCallsUsed": row["model_calls_used"],
        "gatewayInvocationIds": json.loads(row["gateway_invocation_ids"]),
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
    }
