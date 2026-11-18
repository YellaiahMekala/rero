from __future__ import annotations

import asyncio
import logging

from .errors import FailureCode, RunFailure
from .graph import Deps, build_graph
from .model import ModelPort
from .runctx import RunContext
from .store import RunStore
from .tools import ReconcileToolClient

log = logging.getLogger("rero.runner")


class Runner:
    """Runs the fixed graph in the API process. No queue, no worker claims, no resume."""

    def __init__(self, store: RunStore, tools: ReconcileToolClient, model: ModelPort) -> None:
        self._store = store
        self._graph = build_graph()
        self._deps = Deps(tools=tools, model=model, persist=self._persist)
        self._tasks: set[asyncio.Task] = set()

    def _persist(self, ctx: RunContext) -> None:
        self._store.update_progress(ctx.run_id, ctx.stage, ctx.budget.used, ctx.model_calls, ctx.gateway_ids)

    def start(self, ctx: RunContext, deadline_seconds: float | None = None) -> asyncio.Task:
        task = asyncio.create_task(self.execute(ctx, deadline_seconds))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_all(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self.wait_all()

    async def execute(self, ctx: RunContext, deadline_seconds: float | None = None) -> None:
        deadline = float(ctx.limits.deadlineSeconds if deadline_seconds is None else deadline_seconds)
        try:
            async with asyncio.timeout(deadline):
                final = await self._graph.ainvoke(
                    {}, config={"configurable": {"ctx": ctx, "deps": self._deps}}
                )
            self._store.finish_success(
                ctx.run_id, final["result"], ctx.budget.used, ctx.model_calls, ctx.gateway_ids
            )
            log.info("run succeeded run_id=%s", ctx.run_id)
        except TimeoutError:
            # A deadline that fires while a gateway request is in flight leaves its outcome unknown.
            code = FailureCode.UNKNOWN_OUTCOME if ctx.model_in_flight else FailureCode.DEADLINE_EXCEEDED
            self._fail(ctx, code)
        except RunFailure as failure:
            self._fail(ctx, failure.code)
        except asyncio.CancelledError:
            raise  # process shutdown: the row stays RUNNING and startup marks it RESTARTED
        except Exception as exc:
            # Only the exception type is logged: messages can embed URLs that carry the analyst note.
            log.error("run crashed run_id=%s type=%s", ctx.run_id, type(exc).__name__)
            self._fail(ctx, FailureCode.RESTARTED)

    def _fail(self, ctx: RunContext, code: FailureCode) -> None:
        self._store.finish_failed(
            ctx.run_id, code, ctx.stage, ctx.budget.used, ctx.model_calls, ctx.gateway_ids
        )
        log.info("run failed run_id=%s stage=%s code=%s", ctx.run_id, ctx.stage, code.value)
