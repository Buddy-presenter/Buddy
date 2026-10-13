import copy
import json
from pathlib import Path

from buddy_api.schema import breaking_changes, generate_schema


def test_openapi_snapshot_matches_real_routes_and_contracts():
    snapshot = Path(__file__).resolve().parents[2] / "docs" / "api" / "openapi.json"
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
