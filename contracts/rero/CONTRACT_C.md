# Contract C v1: reconcile-ai ↔ RERO (conditional, Q3)

Source of truth for Q3.1 to Q3.6. Design: `docs/TDD.md` §2.3 and §6; judged by reconcile-ai
`docs/adr/RERO_EVALUATION_RULE.md` (frozen). JSON Schemas in this folder (`*.v1.json`, draft 2020-12) must validate
the examples in `examples/`; RERO's provider tests and reconcile-ai's consumer tests both load them.

Conventions: ISO-8601 UTC timestamps, UUID strings, camelCase JSON. Error body for every non-2xx:
`{"code": "<UPPER_SNAKE>", "message": "<safe text, no payload>"}`.

## 1. reconcile-ai → RERO

Auth: `Authorization: Bearer <service JWT>` from the platform realm, audiences `rero-api` **and** `ai-gateway`,
claim `tenant_id`, role `RERO_CALLER`. RERO takes the tenant from this token only; the body never carries a tenant.

### POST /v1/recommendations
Header `Idempotency-Key: <jobId>` (required, equals `jobId`).
```json
{ "jobId": "uuid",
  "analystNote": "string, already PII-masked by reconcile-ai, max 2000 chars, may be empty",
  "limits": { "maxToolCalls": 8, "maxModelCalls": 2, "deadlineSeconds": 60 },
  "runToken": "compact JWS (see §3)" }
```
Limits: maxToolCalls 1..20, maxModelCalls exactly 2 (draft + self-check), deadlineSeconds 5..120.
- `202` new run started, body = Run (below).
- `200` same key, same canonical input: the existing run (no second run, no second model call).
- `409 IDEMPOTENCY_KEY_REUSED` same key, different canonical input. Canonical input = SHA-256 over
  `tenant_id`, `jobId`, `analystNote`, `limits` (sorted keys); **`runToken` is excluded**.
- `400 INVALID_REQUEST`, `401`, `403 WRONG_AUDIENCE_OR_ROLE`.

### GET /v1/recommendations/{runId}
Same service token; a run of another tenant is `404` (not 403).
Run:
```json
{ "runId": "uuid", "jobId": "uuid",
  "status": "RUNNING | SUCCEEDED | FAILED",
  "stage": "LOAD_CONTEXT | RETRIEVE | DRAFT | SELF_CHECK | DONE",
  "failureCode": null,
  "toolCallsUsed": 3, "modelCallsUsed": 2,
  "gatewayInvocationIds": ["string"],
  "startedAt": "...", "finishedAt": "... or null",
  "result": null }
```
`failureCode` (FAILED only): `DEADLINE_EXCEEDED`, `TOOL_BUDGET_EXCEEDED`, `TOOL_UNAVAILABLE`, `TOOL_REJECTED`,
`MODEL_UNAVAILABLE`, `MODEL_BLOCKED`, `UNKNOWN_OUTCOME`, `INVALID_MODEL_OUTPUT`, `INVALID_CITATION`, `RESTARTED`.

`result` (SUCCEEDED only):
```json
{ "rootCauseCategory": "TIMING_DIFFERENCE | FX_RATE_DIFFERENCE | FEE_OR_CHARGE | DUPLICATE_ENTRY | MISSING_ENTRY | DATA_ENTRY_ERROR | PARTIAL_SETTLEMENT | UNKNOWN",
  "explanation": "string, max 2000",
  "citedChunkIds": ["uuid"],
  "confidence": 0.82,
  "selfCheck": "PASS | CHALLENGE",
  "selfCheckReason": "string, max 500" }
```
RERO returns **no route, no approval, no status change**. Citations must be a subset of chunk ids returned by the
evidence tool in this run (RERO checks; reconcile-ai checks again against what it served). `kb:` refs are never
citable. A self-check `PASS` cannot lower review; `CHALLENGE` maps to reconcile-ai's `reviewerChallenged` gate input.

Retention: RERO stores run metadata, the validated result, counts, timing and gateway invocation ids for
`RERO_RESULT_TTL` (default 24 h), then purges. It never stores tokens, prompts, tool payloads or raw model output.
v0 runs in the API process: a restart marks any RUNNING run `FAILED/RESTARTED` at startup.

## 2. RERO → reconcile-ai internal tools (read-only)

Base URL configured in RERO (`RECONCILE_INTERNAL_BASE_URL`). Auth: `Authorization: Bearer <runToken>`.
All `GET`. No endpoint takes a tenant or break id; the job row supplies scope.

| Path | Returns |
|---|---|
| `/internal/rero/v1/jobs/{jobId}/break` | `{ "breakRef", "variance", "currency", "sourceA", "sourceB", "status" }` (masked) |
| `/internal/rero/v1/jobs/{jobId}/evidence?query=` | `{ "chunks": [ { "chunkId": "uuid", "source": "string", "text": "masked string" } ] }` |
| `/internal/rero/v1/jobs/{jobId}/similar-breaks` | `{ "items": [ { "breakRef", "rootCauseCategory", "resolvedAt" } ] }` |
| `/internal/rero/v1/jobs/{jobId}/knowledge?query=` | `{ "outcome": "OK|EMPTY|UNAVAILABLE|REJECTED", "procedures": [ { "ref": "kb:...", "title", "text" } ] }` |

Every call is counted atomically in reconcile-ai against `maxToolCalls` for the job, and audited without arguments.
Errors: `401 RUN_TOKEN_INVALID` (signature, issuer, audience, expiry, scope), `403 RUN_TOKEN_JOB_MISMATCH`,
`409 JOB_NOT_RUNNING` (terminal or cancelled job, or job mode is not RERO), `429 TOOL_BUDGET_EXCEEDED`.
There is no write route under `/internal/**`; any non-GET is `405`.

## 3. Run token

Minted by reconcile-ai per job, ES256, key pair from configuration (public key given to nobody but the internal
chain; RERO does not verify it, it only forwards it).
```
header: alg=ES256, kid
claims: iss=reconcile-ai, aud=reconcile-internal, sub=rero, tenant_id, job_id, scope="tools:read", jti, iat, exp
```
TTL = deadlineSeconds + 30 s. Never logged, never persisted by either side.

## 4. Model calls (inside RERO)

Exactly two per run: DRAFT and SELF_CHECK. Port with two adapters: `FakeModel` (default, deterministic, tests) and
`GatewayModel` (Contract B, `platform/contracts/gateway/`), forwarding the incoming service token. Idempotency key
per call: `{jobId}:draft` and `{jobId}:selfcheck`. A gateway timeout or 5xx after send is `UNKNOWN_OUTCOME`:
**no retry**, the run fails. A gateway 409/policy block is `MODEL_BLOCKED`.
