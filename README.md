# RERO: bounded recommendation run

RERO is a small Python service that, for one reconciliation break, runs a fixed, bounded workflow and returns a
**recommendation only**: a root-cause category, an explanation with citations, a confidence and a one-way
self-check verdict. It returns no route, no approval and no status change. The Java service stays the authority.

Source of truth: `platform/contracts/rero/CONTRACT_C.md` (schemas and examples in the same folder).

## Honest scope

- This is an **experiment**. It is judged by the frozen rule in reconcile-ai `docs/adr/RERO_EVALUATION_RULE.md`;
  the default outcome is **CUT**. Nothing here is evidence that it earns its place.
- Everything runs on fakes. The tests use a fake model, a fake reconcile-ai (respx) and a fake gateway (respx). No
  paid API and no real provider network is used, and none has been exercised end to end.
- v0 runs the graph **in the API process**. There is no worker, queue, claim, resume or recovery. A restart marks
  any RUNNING run `FAILED / RESTARTED`.
- The self-check is one-way: `PASS` never lowers review; `CHALLENGE` is passed through unchanged.
- Gateway calls forward the caller's own service token. If that token expires mid-run the gateway call fails;
  there is no token exchange.
- Pattern checks and masking are done upstream; RERO adds none of its own.

## Flow

`LOAD_CONTEXT -> RETRIEVE -> DRAFT -> (deterministic reference check) -> SELF_CHECK -> DONE`, built with LangGraph
as a fixed linear `StateGraph`. The reference check is a graph node but not a reported stage: the run stays at
`DRAFT` while it runs.

- Tools (reconcile-ai internal, GET only, run token as bearer): break, similar-breaks, evidence, knowledge.
- Exactly two model calls: draft and self-check.
- A local atomic tool budget (`maxToolCalls`) and a deadline (`deadlineSeconds`) are enforced by RERO.
- Citations must be a subset of chunk ids served by the evidence tool in the same run. `kb:` references are
  never citable.

## Run

```
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m pytest -q
```

The provider tests read the Contract C schemas. By default they look for
the vendored copy in `contracts/rero` (and `contracts/gateway` for Contract B), copied from the platform hub;
re-copy when a contract changes. Set `RERO_CONTRACTS_DIR` to point at another copy.

Serve (needs real issuer settings; see the table):

```
uvicorn rero.app:create_app --factory --host 0.0.0.0 --port 8080
```

## Configuration

Read from the process environment only (no `.env` file is read). Invalid or missing values stop startup.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `RERO_ISSUER` | yes | | Expected `iss` of service JWTs |
| `RERO_JWKS_URI` | yes | | JWKS URL used to verify RS256 signatures |
| `RERO_REQUIRED_AUDIENCES` | no | `rero-api,ai-gateway` | Audiences that must all be present |
| `RECONCILE_INTERNAL_BASE_URL` | yes | | Base URL of reconcile-ai internal tools |
| `MODEL_ADAPTER` | no | `fake` | `fake` or `gateway` |
| `GATEWAY_BASE_URL` | if `gateway` | | Base URL of ai-gateway |
| `RERO_RESULT_TTL` | no | `86400` | Retention; seconds or `30m`, `24h`, `1d` |
| `RERO_DB_PATH` | no | `rero.db` | SQLite file |
| `RERO_TOOL_TIMEOUT_SECONDS` | no | `10` | Per tool call timeout |
| `RERO_GATEWAY_TIMEOUT_SECONDS` | no | `30` | Per gateway call timeout |

## API

- `POST /v1/recommendations` with `Idempotency-Key` equal to `jobId`: 202 new, 200 same input, 409
  `IDEMPOTENCY_KEY_REUSED`, 400, 401, 403 `WRONG_AUDIENCE_OR_ROLE`.
- `GET /v1/recommendations/{runId}`: another tenant's run is 404.
- `GET /health/live`, `GET /health/ready` (readiness checks the database only).

Auth: RS256 service JWT, audiences `rero-api` and `ai-gateway`, role `RERO_CALLER` (in `realm_access.roles` or
`roles`), claim `tenant_id`.

## What is stored and logged

Stored for `RERO_RESULT_TTL`: run metadata, validated result, counts, timing, gateway invocation ids. Never stored
or logged: bearer or run tokens, prompts, tool payloads, the analyst note, raw model output. Tests scan the
database file and captured logs for this. Expired rows are purged at startup and on each start request.

## Test evidence (latest)

Unit and provider contract tests only, all on fakes (a fake reconcile-ai internal API and a fake gateway). They show the
code matches the contract; they do not measure recommendation quality. Run: `pytest -q` and `ruff check .` (see Run).
**143 tests, ruff clean**, CI green. Register ids are in the platform repo (`evidence/EVIDENCE_REGISTER.md`).

| Feature | How it was tested | Result | Evidence |
|---|---|---|---|
| Contract C schemas | Schemas and valid/invalid examples validated; fixtures checked | 7 schemas, 25 examples green | E-31, [R1](docs/testing/FEATURE_TESTING_GUIDE.md#r1-contract-schemas-and-examples) |
| Service JWT auth, fail-fast config | Missing, malformed, wrong-aud/role, non-RS256 tokens; bad settings | All refused as designed; fakes | E-31, [R2](docs/testing/FEATURE_TESTING_GUIDE.md#r2-service-jwt-auth-and-config) |
| Idempotent runs | Same key twice, changed input, other tenant | 202 then 200 with one run; 409 on changed input | E-31, [R3](docs/testing/FEATURE_TESTING_GUIDE.md#r3-idempotent-runs) |
| No sensitive storage, TTL purge | Scan database file and logs; purge and restart tests | No tokens, prompts or payloads found | E-31, [R4](docs/testing/FEATURE_TESTING_GUIDE.md#r4-retention-and-no-sensitive-storage) |
| Fixed graph, tool budget, deadline | Fake model, concurrent budget test | Atomic budget, deadline, fail-closed on crash | E-31, [R5](docs/testing/FEATURE_TESTING_GUIDE.md#r5-fixed-graph-tool-budget-and-deadline) |
| Gateway adapter | Fake gateway, replies checked against Contract B | Unknown outcome = exactly one request, no retry | E-31, [R6](docs/testing/FEATURE_TESTING_GUIDE.md#r6-gateway-adapter-and-unknown-outcome) |
| No write paths | Structural tests on HTTP clients | Two clients, GET only, no write route mocked | E-31, [R7](docs/testing/FEATURE_TESTING_GUIDE.md#r7-structural-no-write-paths) |
| Q3.7 RERO keep/cut evaluation | Paid, approved, real gateway, 28 cases, same day | **CUT**: C1-C3 fail (root cause 26/28 vs 28/28), C4-C6 pass; $0.0145 | [R8](docs/testing/FEATURE_TESTING_GUIDE.md#r8-q37-keepcut-evaluation) |

| Repo | CI run | Commit |
|---|---|---|
| rero | `ci` #2 (fakes) | `b2ba078` |

Detailed guide (tests per feature, commands, failure modes, interview questions): [docs/testing/FEATURE_TESTING_GUIDE.md](docs/testing/FEATURE_TESTING_GUIDE.md).
