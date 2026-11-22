import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

spec = importlib.util.spec_from_file_location(
    "buddy_log_viewer", Path(__file__).resolve().parents[2] / "scripts/buddy_log_viewer.py"
)
viewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewer)


def test_join_timings_redaction_dedup_and_cloudwatch_export():
    identifier = "12345678-1234-4234-8234-123456789abc"
    line = (
        f"presentation_latency event=action_finished decision_id={identifier} "
        "tool=next_slide source_format=pdf outcome=completed capture_to_action_ms=250 "
        "executor_ms=12 transcript=PRIVATE token=SECRET"
    )
    backend = json.dumps(
        {
            "event": "presentation_agent_decision",
            "decision_id": identifier,
            "request_id": "req-1",
            "elapsed_ms": 100,
            "phase_timings_ms": {"selection": 80, "PRIVATE": 1},
            "prompt": "SECRET",
        }
    )
    cloud = json.dumps({"events": [{"timestamp": 1234, "message": backend}]})
    events = viewer.parse_text(line + "\n" + line, "desktop") + viewer.parse_text(cloud, "api")
    result = viewer.build_snapshot(events, [])
    assert result["summary"] == {
        "events": 2,
        "decisions": 1,
        "completed": 1,
        "p50_ms": 250,
        "p95_ms": 250,
    }
    assert len(result["groups"]) == 1
    assert result["groups"][0]["fields"]["phase_timings_ms"] == {"selection": 80}
    assert "PRIVATE" not in json.dumps(result)
    assert "SECRET" not in json.dumps(result)


def test_unlinked_and_malformed_records():
    assert viewer.parse_line("random exception with PRIVATE text", "api") is None
    assert viewer.parse_line('{"_aws":null,"Service":"api"}', "api")
    event = viewer.parse_line("presentation_speech event=interim_submitted attempt=1", "desktop")
    snapshot = viewer.build_snapshot([event], [])
    assert snapshot["groups"][0]["id"].startswith("unlinked:")
    assert snapshot["summary"]["p95_ms"] is None


def test_backend_phase_header_logs_join_desktop_decision():
    text = (
        "presentation_latency event=proposal_received decision_id=abc tool=next_slide\n"
        "presentation_latency event=backend_phase decision_id=abc phase=session duration_ms=45\n"
        "presentation_latency event=backend_phase decision_id=abc phase=selection duration_ms=120\n"
        "presentation_policy event=proposal_rejected decision_id=abc reason=duplicate\n"
    )
    snapshot = viewer.build_snapshot(viewer.parse_text(text, "desktop"), [])
    assert len(snapshot["groups"]) == 1
    assert snapshot["groups"][0]["fields"]["phase_timings_ms"] == {"session": 45, "selection": 120}
    assert snapshot["groups"][0]["events"][-1]["fields"]["reason"] == "duplicate"


def test_watch_file_refresh_and_missing_source(tmp_path):
    path = tmp_path / "backend.log"
    store = viewer.LogStore([path])
    assert store.snapshot()["sources"][0]["status"] == "Not available"
    path.write_text("presentation_latency event=local_navigation decision_id=abc tool=next_slide\n")
    assert store.snapshot()["summary"]["events"] == 1
    with path.open("a") as file:
        file.write("presentation_latency event=proposal_failed submit_to_failure_ms=5\n")
    assert store.snapshot()["summary"]["events"] == 2


def test_http_import_and_local_origin_guard(tmp_path):
    store = viewer.LogStore([])
    server = ThreadingHTTPServer(("127.0.0.1", 0), viewer.make_handler(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        payload = json.dumps(
            {
                "name": "api.log",
                "text": json.dumps(
                    {"event": "presentation_agent_decision", "decision_id": "abc", "elapsed_ms": 30}
                ),
            }
        ).encode()
        with urlopen(Request(base + "/api/import", data=payload, method="POST")) as response:
            assert json.load(response)["events"] == 1
        with urlopen(base + "/api/logs") as response:
            assert json.load(response)["summary"]["decisions"] == 1
        with pytest.raises(HTTPError) as rejected:
            urlopen(
                Request(
                    base + "/api/import",
                    data=payload,
                    headers={"Origin": "https://external.example"},
                    method="POST",
                )
            )
        assert rejected.value.code == 403
        with urlopen(base) as response:
            assert b"Observability" in response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
