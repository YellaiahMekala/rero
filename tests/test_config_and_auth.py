# Copyright (c) 2026 the owner of this repository. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary. View only; see LICENSE and AI_USE_POLICY.md.
# Canary: rero-canary-64f662dc07dd453c9360
from __future__ import annotations

import pytest
from pydantic import ValidationError

from rero.app import create_app
from rero.config import Settings

from .conftest import (
    JOB_ID,
    RUN_TOKEN,
    assert_valid,
    bearer,
    foreign_key,
    make_settings,
    make_token,
    start_body,
)

# ---------------- config: fail fast ----------------


def test_settings_from_environment(monkeypatch, tmp_path):
    for k, v in {
        "RERO_ISSUER": "https://idp.test/realms/platform",
        "RERO_JWKS_URI": "https://idp.test/certs",
        "RECONCILE_INTERNAL_BASE_URL": "http://reconcile.test/",
        "MODEL_ADAPTER": "gateway",
        "GATEWAY_BASE_URL": "http://gateway.test",
        "RERO_RESULT_TTL": "2h",
        "RERO_DB_PATH": str(tmp_path / "x.db"),
        "RERO_REQUIRED_AUDIENCES": "rero-api, ai-gateway",
    }.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.result_ttl_seconds == 7200
    assert s.required_audiences == ["rero-api", "ai-gateway"]
    assert s.reconcile_internal_base_url == "http://reconcile.test"
    assert s.model_adapter == "gateway"


def test_defaults(tmp_path):
    s = make_settings(tmp_path, RERO_RESULT_TTL="86400")
    assert s.model_adapter == "fake"
    assert s.result_ttl_seconds == 86400


@pytest.mark.parametrize(
    "overrides",
    [
        {"RERO_ISSUER": ""},
        {"RERO_JWKS_URI": "not-a-url"},
        {"RECONCILE_INTERNAL_BASE_URL": "ftp://x"},
        {"MODEL_ADAPTER": "gateway"},  # no GATEWAY_BASE_URL
        {"MODEL_ADAPTER": "openai"},
        {"RERO_RESULT_TTL": "soon"},
        {"RERO_RESULT_TTL": "0"},
        {"RERO_REQUIRED_AUDIENCES": " , "},
    ],
)
def test_invalid_settings_fail_fast(tmp_path, overrides):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, **overrides)


def test_missing_required_env_stops_startup(monkeypatch):
    for name in ("RERO_ISSUER", "RERO_JWKS_URI", "RECONCILE_INTERNAL_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError):
        create_app()


# ---------------- health ----------------


async def test_health_probes_need_no_auth(api):
    assert (await api.client.get("/health/live")).json() == {"status": "UP"}
    assert (await api.client.get("/health/ready")).status_code == 200


# ---------------- auth ----------------


async def _post(api, token=None, headers=None):
    h = headers if headers is not None else bearer(token) if token else {}
    h = {**h, "Idempotency-Key": JOB_ID}
    return await api.client.post("/v1/recommendations", json=start_body(), headers=h)


async def test_missing_and_malformed_bearer_is_401(api):
    for headers in (
        {},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer junk"},
    ):
        r = await _post(api, headers=headers)
        assert r.status_code == 401
        assert r.json()["code"] == "UNAUTHENTICATED"
        assert_valid("error", r.json())


@pytest.mark.parametrize(
    "token",
    [
        lambda: make_token(signing_key=foreign_key()),  # wrong signature
        lambda: make_token(expires_in=-60),  # expired
        lambda: make_token(issuer="https://evil.test/realms/x"),  # wrong issuer
        lambda: make_token(tenant=None),  # tenant_id claim required
        lambda: make_token(tenant="  "),
    ],
)
async def test_invalid_tokens_are_401(api, token):
    r = await _post(api, token())
    assert r.status_code == 401
    assert_valid("error", r.json())


@pytest.mark.parametrize(
    "token",
    [
        lambda: make_token(audiences=["rero-api"]),  # missing ai-gateway
        lambda: make_token(audiences=["ai-gateway"]),  # missing rero-api
        lambda: make_token(audiences=["other"]),
        lambda: make_token(roles=["GATEWAY_CONSUMER"]),  # missing RERO_CALLER
        lambda: make_token(roles=[]),
    ],
)
async def test_wrong_audience_or_role_is_403(api, token):
    r = await _post(api, token())
    assert r.status_code == 403
    assert r.json()["code"] == "WRONG_AUDIENCE_OR_ROLE"
    assert_valid("error", r.json())


async def test_roles_claim_and_audience_string_variants_accepted(api, upstreams):
    token = make_token(realm_roles=False)  # top-level roles claim
    r = await _post(api, token)
    assert r.status_code == 202


async def test_non_rs256_algorithm_rejected(api):
    import jwt

    forged = jwt.encode(
        {
            "iss": "https://idp.test/realms/platform",
            "aud": ["rero-api", "ai-gateway"],
            "tenant_id": "t",
            "roles": ["RERO_CALLER"],
            "exp": 4102444800,
        },
        "secret" * 8,
        algorithm="HS256",
        headers={"kid": "test-key-1"},
    )
    r = await _post(api, forged)
    assert r.status_code == 401


async def test_body_tenant_is_rejected(api):
    body = start_body()
    body["tenantId"] = "tenant-b"
    r = await api.client.post(
        "/v1/recommendations", json=body, headers={**bearer(make_token()), "Idempotency-Key": JOB_ID}
    )
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_REQUEST"
    assert "tenant-b" not in r.text and RUN_TOKEN not in r.text
