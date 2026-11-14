"""Worker IPC must not inherit a Windows console's text encoding."""

import io
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import BaseModel

from buddy_api.processing import stage


@pytest.mark.parametrize("encoding", ["cp1252", "ascii"])
def test_stage_serializes_non_ascii_json_as_utf8(monkeypatch, encoding):
    class Output(BaseModel):
        text: str

    text = "Résumé — العربية 中文 ✓"
    kms_key = "arn:aws:kms:eu-central-1:123456789012:key/11111111-1111-1111-1111-111111111111"
    request = {
        "operation": "inspect",
        "source": {
            "source_format": "pptx",
            "key": "source",
            "version": "1",
            "size": 1,
            "sha256": "a" * 64,
            "revision": "1",
            "deck_id": str(uuid4()),
        },
        "storage": {
            "bucket": "buddy-test-private",
            "region": "eu-central-1",
            "kms_key": kms_key,
        },
    }
    wire = io.BytesIO()
    console = io.TextIOWrapper(wire, encoding=encoding)
    monkeypatch.setattr(
        stage, "SourceStorage", lambda _settings: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(stage, "inspect_source", lambda *_args: Output(text=text))
    monkeypatch.setattr(stage, "_configure_child_resource_limits", lambda _resource: None)
    with monkeypatch.context() as streams:
        streams.setattr(
            stage.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))
        )
        streams.setattr(stage.sys, "stdout", console)
        assert stage.main() == 0
        console.flush()
        payload = wire.getvalue()
    assert json.loads(payload.decode("utf-8")) == {"text": text}
