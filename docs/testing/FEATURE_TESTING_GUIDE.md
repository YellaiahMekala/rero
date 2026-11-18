# rero: feature testing guide

Scope: platform Q3.1 to Q3.6, tested on 2026-11-18. RERO is an optional Python service (FastAPI, LangGraph) that
drafts one bounded root-cause recommendation for a reconciliation break and runs a one-way self-check. All tests here
use fakes: a fake reconcile-ai internal API and a fake gateway. They show the code matches Contract C and Contract B.
They do **not** measure recommendation quality; that is Q3.7 (R8).

Evidence: register row E-31 in `platform/evidence/EVIDENCE_REGISTER.md`; CI green, `ci` #2 on `b2ba078`; 143 pytest
tests plus `ruff`. Contract: `platform/contracts/rero/CONTRACT_C.md` (vendored in `contracts/rero`).

How to run everything (free, no network, no model, no secrets):

```
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m pytest -q
```

`RERO_CONTRACTS_DIR` points the tests at another copy of the contracts. Re-copy the folder when the platform contract
changes.

## Index

| ID | Feature | Test file |
|---|---|---|
| [R1](#r1-contract-schemas-and-examples) | Contract C schemas and examples | `test_contract_schemas.py` |
| [R2](#r2-service-jwt-auth-and-config) | Service JWT auth and fail-fast config | `test_config_and_auth.py` |
| [R3](#r3-idempotent-runs) | Idempotent runs | `test_idempotency_and_retention.py` |
| [R4](#r4-retention-and-no-sensitive-storage) | Retention and no sensitive storage | `test_idempotency_and_retention.py` |
| [R5](#r5-fixed-graph-tool-budget-and-deadline) | Fixed graph, tool budget, deadline | `test_graph.py` |
| [R6](#r6-gateway-adapter-and-unknown-outcome) | Gateway adapter, unknown outcome | `test_gateway_model.py` |
| [R7](#r7-structural-no-write-paths) | Structural no-write paths | `test_no_write_paths.py` |
| [R8](#r8-q37-keepcut-evaluation) | Q3.7 keep/cut evaluation | E-32, done, CUT |

## R1 Contract schemas and examples

**What and why.** reconcile-ai and RERO are separate repos. If either drifts from the shared JSON Schemas, the
integration breaks silently. The schemas are the contract.

**Design decision.** Draft 2020-12 schemas with explicit ids, valid and invalid examples per schema, loaded by both
RERO's provider tests and reconcile-ai's consumer tests.

**Tests** (`test_contract_schemas.py`). The contract folder exists; every schema is draft 2020-12 with an explicit id;
each example validates or fails as its filename says; every schema has at least one valid and one invalid example; the
tool fixtures used in tests match the internal schemas; the fake gateway fixture matches the Contract B response schema.

**Result.** Green in CI #2 (E-31): Contract C plus 7 schemas and 25 examples.

**Failure looks like.** A schema without an invalid example fails the suite, so a schema that accepts everything is caught.

**Honest limits.** Schemas prove shape, not meaning.

**Interview question.** *How do you keep two services in sync?* Both test against the same schema files and examples,
so a change breaks one side's build before it reaches the other.

## R2 Service JWT auth and config

**What and why.** RERO must trust only the platform realm and must take the tenant from the token, never the body.

**Design decision.** RS256 service JWT with audience `rero-api` and role `RERO_CALLER`; tenant from the `tenant_id`
claim only; invalid settings stop startup.

**Tests** (`test_config_and_auth.py`). Settings from environment and defaults; invalid settings fail fast; missing
required env stops startup; health probes need no auth; missing or malformed bearer is 401; invalid tokens are 401;
wrong audience or role is 403; role and audience claim variants are accepted; a non-RS256 algorithm is rejected; a
body that carries a tenant is rejected.

**Result.** Green, CI #2.

**Edge cases.** The `alg` confusion case is covered by the non-RS256 test.

**Honest limits.** Tokens are minted by the tests; a live check against the platform realm is not recorded for RERO.

**Interview question.** *Why reject a tenant in the body?* A caller could ask for another tenant's data; the token is the
only trusted source.

## R3 Idempotent runs

**What and why.** reconcile-ai may retry a start request after a timeout. A second run would mean a second model call.

**Design decision.** `Idempotency-Key` equals `jobId`. Canonical input hash covers tenant, jobId, analyst note and
limits, and **excludes the run token** (it changes per attempt). Same key and input: 200 with the existing run. Same key
and different input: 409.

**Tests** (`test_idempotency_and_retention.py`). New run is 202 then the same input is 200 with one run; the run token
is excluded from the hash; same key with different input is 409; same key from another tenant is a separate run; key
must equal job id; invalid bodies are 400 without echo; empty analyst note is accepted; another tenant's run is 404;
unknown and malformed run ids.

**Result.** Green, CI #2.

**Honest limits.** In-process store (see R4 on restart); no multi-replica test.

**Interview question.** *Why exclude the token from the hash?* Otherwise a retry with a fresh token looks like a new
request and the idempotency guarantee is lost.

## R4 Retention and no sensitive storage

**What and why.** A recommendation service must not become a store of tokens, prompts or payloads.

**Design decision.** Store only run metadata, the validated result, counts, timing and gateway invocation ids, for
`RERO_RESULT_TTL`. Purge at startup and on each start request. A restart marks any RUNNING run `FAILED/RESTARTED`.

**Tests.** The database file holds no tokens, prompts, payloads or raw output (success and failed runs); the schema has
no column for sensitive data; logs contain no payload; purge removes only expired rows; an expired run is invisible and
its key reusable; startup marks running runs failed with `RESTARTED`.

**Result.** Green, CI #2.

**Edge cases.** The test scans the database file bytes and captured logs, so a new log line leaking a token fails it.

**Honest limits.** The scan looks for known sentinel values; it cannot prove the absence of every possible leak.

**Interview question.** *How do you prove you do not log secrets?* I put recognisable tokens and prompts through a run,
then scan the database file and captured logs for them.

## R5 Fixed graph, tool budget and deadline

**What and why.** An agent that chooses its own steps can loop or overspend. A recommendation should be bounded and
predictable.

**Design decision.** A fixed linear LangGraph (LOAD_CONTEXT, RETRIEVE, DRAFT, SELF_CHECK, DONE), a fake model in tests, a
local atomic tool budget (`maxToolCalls`) and a deadline. Citations must be a subset of chunk ids served by the evidence
tool in the same run; `kb:` refs are never citable.

**Tests** (`test_graph.py`). Nodes run in fixed order; the result cites a served chunk; tools send the run token as
bearer and no tenant; challenge passes through; model output failures; budget exceeded locally; budget of one stops after
the first call; budget is atomic under concurrency; internal error mapping; transport error and malformed body are
`TOOL_UNAVAILABLE`; unavailable knowledge does not fail the run; no evidence yields `UNKNOWN`; deadline exceeded and
enforced on the API path; an unexpected crash fails closed without leaking.

**Result.** Green, CI #2. (reconcile-ai counts the same budget on its side: 30 parallel calls vs budget 5 gave 5
accepted and 25 x 429, see the reconcile-ai guide F13.)

**Honest limits.** A fake model returns scripted output, so nothing is said about real model behaviour.

**Interview question.** *Why a fixed graph?* The decision is the recommendation, not the route. A fixed order is easy to
test, bound and audit.

## R6 Gateway adapter and unknown outcome

**What and why.** When RERO calls the model through ai-gateway, a timeout after sending must not become a second
request.

**Design decision.** The adapter forwards the caller's token and uses idempotency keys scoped to the job
(`{jobId}:draft`, `{jobId}:selfcheck`). A timeout or 5xx after send is `UNKNOWN_OUTCOME` with **exactly one request**,
never a retry. A connect error means nothing was sent.

**Tests** (`test_gateway_model.py`). Token and keys forwarded; every fake gateway reply matches Contract B; replayed
idempotent status accepted; timeout or 5xx after send is unknown outcome with one request; the invocation id from the
error is recorded; error mapping never retries; connect error means nothing sent; draft and self-check are one request
each; a 200 without output is `INVALID_MODEL_OUTPUT`; a bad citation is `INVALID_CITATION`; challenge passes through; the
gateway path logs no payload; the request is the only place the prompt appears; a deadline with a request in flight is
unknown outcome.

**Result.** Green, CI #2, against a fake gateway.

**Honest limits.** No real RERO-to-gateway run exists. That belongs to Q3.7.

**Interview question.** *What do you do on a timeout after send?* I count it as one request with an unknown outcome,
record any invocation id, and let a person check the ledger.

## R7 Structural no-write paths

**What and why.** The safety claim is that RERO can only read. A code-structure test makes that checkable.

**Tests** (`test_no_write_paths.py`). Exactly two HTTP clients exist (reconcile and gateway); no knowledge-base service
client exists; the tool client exposes GET only; the tool client only ever issues GET; no write route is even mocked.

**Result.** Green, CI #2.

**Honest limits.** A structural test checks the code, not a future change made around it.

**Interview question.** *How do you enforce read-only?* Three layers: no write route in the contract, a GET-only client,
and a test that fails if another HTTP client appears.

## R8 Q3.7 keep/cut evaluation

Status: **done 2026-11-18, decision CUT (E-32).** Paid run (approved), both engines on gpt-4o-mini through the real ai-gateway, knowledge off for both, same day. C1 fail (HRD-002: RERO sent a wrong cause to L1 where the baseline chose manual review), C2 fail (root cause 26/28 vs baseline 28/28; McNemar p = 0.50, not significant), C3 fail (evidence recall 23/24 vs 24/24); C4 pass (cost 1.34x), C5 pass (p95 6522 vs 5460 ms), C6 pass (no budget overrun, no duplicate runs, no unknown outcomes). Spend 0.0145 USD from the gateway ledger. RERO stays disabled by default; the Q1/Q2 three-service platform is the final shape. Interview point: the rule was frozen before the code and the negative result is published as it stands. Full report: `reconcile-ai/docs/proof/q3-7-rero-keep-cut-2026-11-18.md`. The evaluation rule was frozen before any Q3 code (reconcile-ai `docs/adr/RERO_EVALUATION_RULE.md`).
