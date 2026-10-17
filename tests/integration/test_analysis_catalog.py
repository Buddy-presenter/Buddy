"""Backend storage/generation schemas stay aligned with the actual executor inputs."""

import json
from pathlib import Path

from buddy_api.analysis.catalog import CATALOG, generation_schema, schema
from buddy_api.analysis.contracts import AnalysisInput
from buddy_desktop.slide_contract import parse_action, parse_manifest

ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = ROOT / "frontend/src/buddy_desktop/slide_contracts"


def test_backend_vendored_catalog_is_byte_identical_and_generator_subset_is_exact():
    for name in ["action-v1.1.schema.json", "manifest-v1.schema.json"]:
        assert schema(name) == json.loads((CATALOG_ROOT / name).read_text())
        from importlib.resources import files

        assert (
            files("buddy_api.analysis").joinpath("schemas", name).read_bytes()
            == (CATALOG_ROOT / name).read_bytes()
        )
    assert (
        {entry["properties"]["type"]["const"] for entry in generation_schema()["oneOf"]}
        == {key for key, category in CATALOG.items() if category == "slide_content"}
        == {"zoom_region", "highlight_region"}
    )


def test_persisted_actions_parse_through_the_completed_production_executor_contract():
    value = AnalysisInput.model_validate_json((ROOT / "docs/fixtures/analysis-v1.json").read_text())
    manifest = parse_manifest(value.manifest.model_dump(mode="json"), ROOT)
    for index, slide in enumerate(value.slides):
        for action in slide.actions:
            parsed = parse_action(action.model_dump(mode="json"), manifest, index)
            assert parsed.type == action.type
            assert parsed.purpose == action.purpose
            assert parsed.instance_id == action.instance_id
