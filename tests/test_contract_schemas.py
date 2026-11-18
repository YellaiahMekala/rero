"""Provider contract tests: schemas are valid draft 2020-12, examples and fixtures conform."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from .conftest import (
    CONTRACTS_DIR,
    SCHEMA_NAMES,
    _schema,
    assert_valid,
    break_body,
    chat_ok,
    evidence_body,
    knowledge_body,
    similar_body,
    validator,
)

EXAMPLES = sorted((CONTRACTS_DIR / "examples").glob("*.json"))


def test_contract_folder_is_present():
    assert CONTRACTS_DIR.is_dir(), f"Contract C folder not found: {CONTRACTS_DIR} (set RERO_CONTRACTS_DIR)"
    assert len(EXAMPLES) >= 20


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_is_draft_2020_12_with_explicit_id(name):
    schema = _schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == f"https://contracts.platform.local/rero/{name}.v1.json"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_examples_match_their_verdict(path):
    from .conftest import example

    schema_name, _, tag = path.stem.partition(".")
    assert schema_name in SCHEMA_NAMES
    instance = example(path.stem)
    errors = list(validator(schema_name).iter_errors(instance))
    if tag.startswith("valid"):
        assert not errors, [e.message for e in errors]
    else:
        assert tag.startswith("invalid"), "example names must be <schema>.valid[-x] or <schema>.invalid-x"
        assert errors, "invalid example unexpectedly passed"


def test_every_schema_has_valid_and_invalid_examples():
    stems = [p.stem for p in EXAMPLES]
    for name in SCHEMA_NAMES:
        assert any(s.startswith(f"{name}.valid") for s in stems), name
        assert any(s.startswith(f"{name}.invalid") for s in stems), name


def test_tool_fixtures_used_by_tests_match_internal_schemas():
    assert_valid("internal-break", break_body())
    assert_valid("internal-evidence", evidence_body())
    assert_valid("internal-similar", similar_body())
    assert_valid("internal-knowledge", knowledge_body())


def test_fake_gateway_fixture_matches_gateway_response_schema():
    import json

    from jsonschema import FormatChecker

    schema = json.loads(
        (CONTRACTS_DIR.parent / "gateway" / "chat-response.v1.json").read_text(encoding="utf-8")
    )
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(chat_ok("x")))
    assert not errors
