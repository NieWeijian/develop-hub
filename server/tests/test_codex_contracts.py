from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from develop_hub.codex_runner import MAX_NORMALIZED_STRING_CHARS, _error_message_from_event, _normalize_event_value


SCHEMA_ROOT = Path(__file__).parents[1] / "schemas"


def _assert_strict_objects(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        if value.get("type") == "object" and isinstance(value.get("properties"), dict):
            assert value.get("additionalProperties") is False, path
            assert set(value.get("required", [])) == set(value["properties"]), path
        for key, nested in value.items():
            _assert_strict_objects(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_strict_objects(nested, f"{path}[{index}]")


@pytest.mark.parametrize(
    "schema_name",
    [
        "plan-result.schema.json",
        "implementation-result.schema.json",
        "review-result.schema.json",
        "qa-report.schema.json",
    ],
)
def test_codex_output_schema_uses_strict_object_contract(schema_name: str) -> None:
    schema = json.loads((SCHEMA_ROOT / schema_name).read_text(encoding="utf-8"))
    _assert_strict_objects(schema)


def test_nested_turn_failure_message_is_extracted() -> None:
    api_error = {
        "type": "error",
        "error": {
            "code": "invalid_json_schema",
            "message": "Missing 'blockers'.",
        },
        "status": 400,
    }
    event = {
        "type": "turn.failed",
        "error": {"message": json.dumps(api_error)},
    }
    assert _error_message_from_event(event) == "Missing 'blockers'."


def test_large_event_is_bounded_for_database_and_frontend() -> None:
    event = {
        "type": "item.completed",
        "item": {"aggregated_output": "x" * (MAX_NORMALIZED_STRING_CHARS + 10_000)},
    }
    normalized = _normalize_event_value(event)
    assert isinstance(normalized, dict)
    output = normalized["item"]["aggregated_output"]
    assert len(output) < MAX_NORMALIZED_STRING_CHARS + 200
    assert "raw JSONL artifact" in output
