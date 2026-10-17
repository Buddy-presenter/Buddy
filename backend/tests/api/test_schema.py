import copy
import json
from pathlib import Path

import pytest

from buddy_api.schema import breaking_changes, generate_schema


def test_request_only_widening_is_compatible_but_narrowing_and_response_changes_are_not():
    before = generate_schema()
    after = copy.deepcopy(before)
    old = before["components"]["schemas"]["UploadMetadata"]["properties"]
    old["content_type"] = {"type": "string", "const": "application/pdf"}
    old["filename"]["minLength"] = 6
    assert breaking_changes(before, after) == []
    assert breaking_changes(after, before)
    after = copy.deepcopy(before)
    after["components"]["schemas"]["HealthData"]["properties"]["status"] = {
        "type": "string",
        "enum": ["ok", "new-value"],
    }
    assert breaking_changes(before, after)


def test_openapi_snapshot_matches_real_routes_and_contracts():
    snapshot = Path(__file__).resolve().parents[3] / "docs" / "api" / "openapi.json"
    assert json.loads(snapshot.read_text(encoding="utf-8")) == generate_schema()


def test_removed_endpoint_is_a_breaking_change():
    before = generate_schema()
    after = copy.deepcopy(before)
    del after["paths"]["/api/v1/health"]
    assert breaking_changes(before, after)


def test_changed_response_and_required_fields_are_breaking():
    before = generate_schema()
    after = copy.deepcopy(before)
    after["components"]["schemas"]["HealthResponse"]["required"] = ["data"]
    assert breaking_changes(before, after)
    after = copy.deepcopy(before)
    after["components"]["schemas"]["HealthResponse"]["properties"]["request_id"]["type"] = "integer"
    assert breaking_changes(before, after)


def test_new_endpoint_optional_property_and_documentation_are_compatible():
    before = generate_schema()
    after = copy.deepcopy(before)
    after["paths"]["/api/v1/additional"] = copy.deepcopy(after["paths"]["/api/v1/health"])
    after["components"]["schemas"]["HealthData"]["properties"]["extra"] = {"type": "string"}
    after["info"]["description"] = "Updated documentation"
    assert breaking_changes(before, after) == []


@pytest.mark.parametrize("name", ["title", "description", "tags", "x-private", "properties"])
@pytest.mark.parametrize("change", ["type", "remove"])
def test_property_identifiers_are_not_documentation(name, change):
    before = generate_schema()
    before["components"]["schemas"]["Deck"] = {
        "type": "object",
        "properties": {name: {"type": "string"}},
    }
    after = copy.deepcopy(before)
    properties = after["components"]["schemas"]["Deck"]["properties"]
    if change == "remove":
        del properties[name]
    else:
        properties[name]["type"] = "integer"
    assert breaking_changes(before, after)


@pytest.mark.parametrize("name", ["title", "description", "x-contract"])
def test_schema_identifiers_are_not_documentation(name):
    before = generate_schema()
    before["components"]["schemas"][name] = {"type": "string"}
    after = copy.deepcopy(before)
    after["components"]["schemas"][name]["type"] = "integer"
    assert breaking_changes(before, after)
    del after["components"]["schemas"][name]
    assert breaking_changes(before, after)


def test_real_schema_and_operation_annotations_can_change():
    before = generate_schema()
    after = copy.deepcopy(before)
    schema = after["components"]["schemas"]["HealthResponse"]
    schema["description"] = "Updated schema documentation"
    schema["properties"]["request_id"]["title"] = "Updated field title"
    schema["x-internal-note"] = "Updated extension"
    operation = after["paths"]["/api/v1/health"]["get"]
    operation["summary"] = "Updated summary"
    operation["responses"]["200"]["description"] = "Updated response documentation"
    assert breaking_changes(before, after) == []


def test_default_object_keys_are_contract_values():
    before = generate_schema()
    before["components"]["schemas"]["HealthData"]["default"] = {"title": "original"}
    after = copy.deepcopy(before)
    after["components"]["schemas"]["HealthData"]["default"]["title"] = "changed"
    assert breaking_changes(before, after)


def test_nested_schema_annotations_are_distinct_from_contract_changes():
    before = generate_schema()
    before["components"]["schemas"]["Deck"] = {
        "allOf": [{"type": "object", "properties": {"title": {"type": "string"}}}]
    }
    after = copy.deepcopy(before)
    nested = after["components"]["schemas"]["Deck"]["allOf"][0]
    nested["description"] = "Updated documentation"
    assert breaking_changes(before, after) == []
    nested["properties"]["title"]["type"] = "integer"
    assert breaking_changes(before, after)


def test_parameter_annotations_can_change_but_parameter_types_cannot():
    before = generate_schema()
    after = copy.deepcopy(before)
    parameter = after["paths"]["/api/v1/version"]["get"]["parameters"][0]
    parameter["description"] = "Updated documentation"
    parameter["schema"]["title"] = "Updated title"
    assert breaking_changes(before, after) == []
    parameter["schema"]["type"] = "integer"
    assert breaking_changes(before, after)
