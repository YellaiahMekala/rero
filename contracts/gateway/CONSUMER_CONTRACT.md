# Consumer contract: `POST /api/v1/chat` (v1)

What a service calling the gateway (reconcile-ai, EKA) sends, what it gets back, and when it may retry. Every
statement cites the code it comes from; `ConsumerContractIT` validates real responses against the schemas in
`docs/integration/schemas/` on every build. Read from code on 2026-11-15.

Schemas (JSON Schema draft 2020-12):

| File | Validates |
|---|---|
| `schemas/chat-request.v1.json` | the request body (`ChatRequest.java`) |
| `schemas/chat-response.v1.json` | every 2xx body (`ChatResponse.java`) |
| `schemas/error.v1.json` | every non-2xx body (`ApiError.java`) |

## 1. Request

`POST /api/v1/chat`, `Content-Type: application/json`, body at most 262,144 bytes (`application.yml:119`; 413 otherwise).

Headers:

| Header | Required | Rule | Source |
|---|---|---|---|
| `Authorization: Bearer <jwt>` | yes | RS256; `iss`, `aud` contains `ai-gateway`, `tenant_id`, `sub`, top-level `roles` containing `GATEWAY_CONSUMER`. See `SERVICE_TOKEN_READINESS.md` | `SecurityConfig.java` |
| `Idempotency-Key` | yes | 8 to 128 chars of `[A-Za-z0-9._:-]`; unique per logical call per tenant | `ChatController.java:36` |
| `X-Tenant-Id` or any tenant header or query parameter | must not be sent | 400 `TENANT_OVERRIDE_REJECTED`; tenant comes from the token only | `TenantOverrideRejectionFilter.java` |

Body (`ChatRequest.java`): `taskClass` (SUMMARY, EXTRACTION, REASONING, CLASSIFICATION, EMBEDDING) and exactly one of
`messages` (1 to 50 of `{role: SYSTEM|USER|ASSISTANT, content ≤ 32,000 chars}`) or `prompt` (`{promptId, variables}`).
`settings` (`temperature` 0 to 2, `topP` 0 to 1, `maxOutputTokens` 1 to 32,000, `stopSequences` ≤ 4, `seed`) is allowed
with `messages` only. Unknown fields are rejected (400 `INVALID_REQUEST`; `fail-on-unknown-properties: true`,
`application.yml:26`). A `tenantId` body field is therefore an `INVALID_REQUEST`, not a `TENANT_OVERRIDE_REJECTED`.

```json
{ "taskClass": "SUMMARY",
  "messages": [ { "role": "USER", "content": "..." } ],
  "settings": { "maxOutputTokens": 256, "temperature": 0.2 } }
```

## 2. Success response (200)

Header `X-Correlation-Id` equals the body's `correlationId` (`ChatController.java:43`). Null fields are omitted.

| Field | Type | Meaning | Source |
|---|---|---|---|
| `invocationId` | uuid | the ledger row for this call; use it in tickets and for `GET /api/v1/invocations/{id}` | `ChatResponse.java` |
| `correlationId` | uuid | trace id for this request | |
| `status` | enum | `OK`, `FALLBACK_OK`, `CACHE_HIT_EXACT`, `CACHE_HIT_SEMANTIC`, `IDEMPOTENT_REPLAY`, `CACHE_DEGRADED` | `InvocationStatus.java` |
| `output` | string | the model's text | |
| `usage.inputTokens`, `usage.outputTokens` | integer | tokens | |
| `usage.source` | enum | `PROVIDER_REPORTED` or `ESTIMATED` | `UsageSource.java` |
| `replayOf` | uuid | only with `IDEMPOTENT_REPLAY`: the original `invocationId` | `ConsumerContractIT.successAndReplay` |
| `cache` | enum | `L1`, `L2`, `SEMANTIC`, `DEGRADED` when the cache took part | `CacheTier.java` |
| `risk` | string | risk action applied when something was flagged (for example `ANNOTATE`) | `RiskAction.java` |

Returned on success (additive, optional, v1): `dataSensitivity` on `/chat` and on stream `completed`/`error` events (absent on an idempotent replay); `firstTokenMs` on stream final events only (absent if no token was produced). `SENSITIVITY_BLOCKED` errors carry `details.dataSensitivity`. Cost and model used stay in the ledger only.

A same-key, same-body request after completion returns the stored result with `status: IDEMPOTENT_REPLAY` and
`replayOf`. There is no 202 and no polling.

## 3. Error response (every non-2xx)

Plain `application/json` (not problem+json), shape `error.v1.json`: `code`, `message`, `timestamp` always;
`correlationId` and `invocationId` when known; `details` is not emitted on `/chat` today. `invocationId` is present
when a ledger row exists for the call, and on `IDEMPOTENCY_UNKNOWN_OUTCOME` it is the **earlier** call's row.

| HTTP | `code` | Ledger row | `Retry-After` | Caller action | Source |
|---|---|---|---|---|---|
| 400 | `MISSING_HEADER` | no | | fix the request | `GatewayExceptionHandler.java:115` |
| 400 | `INVALID_REQUEST` | no | | fix the request (body, key format, unknown field) | `GatewayExceptionHandler.java:78-92` |
| 400 | `TENANT_OVERRIDE_REJECTED` | no | | remove the tenant header or parameter | `TenantOverrideRejectionFilter.java:46` |
| 401 | `UNAUTHENTICATED` | no | | fix the token | `SecurityConfig.java` |
| 403 | `FORBIDDEN` | no | | token lacks `GATEWAY_CONSUMER` | `SecurityConfig.java:69-71` |
| 404 | `PROMPT_NOT_FOUND` | no | | wrong `promptId` for this tenant | `InvocationService.java:723` |
| 409 | `IDEMPOTENCY_IN_PROGRESS` | no | none | the first request is still running: wait for its answer; do not resend | `InvocationService.java:744-755` |
| 409 | `IDEMPOTENCY_UNKNOWN_OUTCOME` | no (earlier row stands) | none | **never retry**; the earlier call may have run. Reconcile via `GET /api/v1/invocations/{invocationId}` | `InvocationService.java:758` |
| 413 | `REQUEST_TOO_LARGE` | no | | body over 262,144 bytes | `RequestSizeLimitFilter.java:68` |
| 422 | `IDEMPOTENCY_KEY_REUSED` | no | | same key, different body: a caller bug | `InvocationService.java:762` |
| 422 | `RISK_BLOCKED` | yes | | content refused; do not retry unchanged | `InvocationService.java:690` |
| 422 | `SENSITIVITY_BLOCKED` | yes | | no model cleared for the data level; do not retry | `InvocationService.java:680` |
| 429 | `RATE_LIMITED` | no | | slow down | `InvocationService.java:243` |
| 429 | `TENANT_CONCURRENCY_LIMIT` | yes (`NOT_SENT`) | 1 | one retry allowed after the delay | `InvocationService.java:361` |
| 429 | `BUDGET_BLOCKED` | yes | seconds to window reset (≥ 1) | **never retry within the deadline**; the budget will not change | `InvocationService.java:700` |
| 500 | `PROMPT_INTEGRITY_FAILURE` | | | report; do not retry | `InvocationService.java:728` |
| 502 | `PROVIDER_CONTRACT_FAILURE` | yes | | provider answered badly; one retry allowed | `InvocationService.java:886-903` |
| 503 | `NOT_SENT`, `RETRYABLE_FAILURE` | yes | 5 | nothing reached the provider; one retry allowed | same |
| 503 | `OVERLOADED` | yes | 1 | one retry allowed | same |
| 503 | `MODEL_UNAVAILABLE` | yes | 5 | one retry allowed | same |
| 503 | `GOVERNANCE_UNAVAILABLE` | yes (`NOT_EXECUTED`) | 5 | budget store unreachable; one retry allowed | `InvocationService.java:712` |
| 503 | `NO_ELIGIBLE_MODEL` | yes | | not reachable over HTTP with the shipped catalogue | `InvocationService.java:290` |
| 504 | `UNKNOWN_OUTCOME` | yes | | **never retry**: the provider may have executed. A same-key resend returns 409 `IDEMPOTENCY_UNKNOWN_OUTCOME` | same |

## 4. Retry rule for callers

1. One logical call, one `Idempotency-Key`, generated before the first attempt and kept for any retry.
2. On 503 `NOT_SENT` / `RETRYABLE_FAILURE` / `OVERLOADED` / `MODEL_UNAVAILABLE` / `GOVERNANCE_UNAVAILABLE`, 502, or
   429 `TENANT_CONCURRENCY_LIMIT`: wait `Retry-After` (default 5 s if absent), then retry **at most once**, with the
   **same key and same body**, only if the caller's own deadline still allows the full call. Then give up and record
   the `invocationId` from the error.
3. Never retry on 504 `UNKNOWN_OUTCOME`, 409 `IDEMPOTENCY_UNKNOWN_OUTCOME`, 429 `BUDGET_BLOCKED`, 422, 400, 401, 403,
   404, 413. For the first two, the call may have executed and been charged: reconcile with
   `GET /api/v1/invocations/{invocationId}` instead.
4. On 409 `IDEMPOTENCY_IN_PROGRESS`: the caller's own earlier request is still running (a duplicate from a retry
   storm or two workers). Wait for that request; resending the same key just yields 409 again until it finishes.
5. A client-side timeout on the caller is **not** a reason to send a new key. Resend the **same key**: a completed call
   replays (`IDEMPOTENT_REPLAY`), a running one says 409 `IDEMPOTENCY_IN_PROGRESS`, an unknown one says 409
   `IDEMPOTENCY_UNKNOWN_OUTCOME`. A new key would make a second, billable call.

Why "at most once": the gateway already retries pre-send failures and fails over inside one call
(`FailureClassification`); a caller's retry is a second full pass through rate limit, budget and provider.

## 5. Provider-side verification

`gateway-api/src/test/java/com/explore/aigateway/api/contract/ConsumerContractIT.java` (6 cases) reads the three
schemas from `docs/integration/schemas/` and validates real responses: 200 and replay; 422 `RISK_BLOCKED` and 429
`BUDGET_BLOCKED` with `Retry-After`; 409 `IDEMPOTENCY_IN_PROGRESS` (no `Retry-After`), 409
`IDEMPOTENCY_UNKNOWN_OUTCOME` (same `invocationId` as the 504), 422 `IDEMPOTENCY_KEY_REUSED`; 503/503/502 provider
failures; 401, 403, 400 ×3 and `TENANT_OVERRIDE_REJECTED`; plus the request schema rejecting the shapes the gateway
rejects. Validator: `com.networknt:json-schema-validator:1.5.1`, test scope only.

Run: `./mvnw -B -pl gateway-api -am verify -Dtest=none -Dsurefire.failIfNoSpecifiedTests=false -Dit.test=ConsumerContractIT -Dfailsafe.failIfNoSpecifiedTests=false`
Result 2026-11-15, local, stub provider: 6 tests, 0 failures.

Not covered here: `POST /api/v1/chat/stream` (SSE; `StreamController.java`) and 503 `STREAM_CAPACITY`. UNVERIFIED by
this test: the exact `message` texts; callers must branch on `code` and HTTP status only.
