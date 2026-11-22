"""Local Buddy observability UI. Run with --help for file and CloudWatch sources."""

import argparse
import hashlib
import importlib
import json
import math
import os
import re
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

FIELDS = re.compile(r"([a-zA-Z_]+)=([^\s]+)")
CODE = re.compile(r"^[a-zA-Z0-9_.:-]{1,100}$")
IDS = {"decision_id", "request_id", "diagnostic_id", "job_id", "deck_id", "analysis_id"}
CODES = {
    "tool",
    "reason",
    "outcome",
    "stage",
    "phase",
    "decision_source",
    "source_format",
    "failure_detail",
    "exception_type",
    "Service",
    "Stage",
    "Outcome",
    "Environment",
}
NUMBERS = {
    "elapsed_ms",
    "duration_ms",
    "capture_to_submit_ms",
    "submit_to_proposal_ms",
    "capture_to_proposal_ms",
    "server_elapsed_ms",
    "capture_to_action_ms",
    "proposal_to_action_ms",
    "preflight_ms",
    "executor_ms",
    "request_age_ms",
    "capture_age_ms",
    "attempt",
    "stable_chars",
    "provider_status",
    "provider_close_code",
    "invalid_result_count",
    "DurationMs",
    "InputTokens",
    "OutputTokens",
}
PHASES = {
    "session",
    "admission",
    "quota_reservation",
    "retrieval",
    "selection",
    "navigation",
    "topic_navigation",
    "validation",
}
PREFIXES = ("presentation_latency", "presentation_speech", "presentation_policy", "speech_stream")
MAX_BYTES = 8 * 1024 * 1024


def parse_line(line: str, source: str, timestamp: str | int = "") -> dict[str, Any] | None:
    """Expose only known diagnostic fields, never raw log lines or document content."""
    values: dict[str, Any]
    start = line.find("{")
    try:
        values = json.loads(line[start:]) if start >= 0 else {}
    except (ValueError, RecursionError):
        values = {}
    if not isinstance(values, dict):
        return None
    if values.get("event") in {"presentation_agent_decision", "presentation_agent_rejected"}:
        event = values["event"]
    elif "_aws" in values and values.get("Service") in {"api", "worker"}:
        event = "operational_metric"
        aws = values.get("_aws")
        timestamp = aws.get("Timestamp", timestamp) if isinstance(aws, dict) else timestamp
    else:
        prefix = next((p for p in PREFIXES if p + " " in line), None)
        if prefix is None:
            return None
        values = dict(FIELDS.findall(line.split(prefix + " ", 1)[1]))
        event = values.get("event", prefix)
    if not isinstance(event, str) or not CODE.fullmatch(event):
        return None
    safe: dict[str, Any] = {}
    for key in IDS | CODES:
        value = values.get(key)
        if isinstance(value, str) and CODE.fullmatch(value):
            safe[key] = value
    for key in NUMBERS:
        try:
            value = int(values[key])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if 0 <= value <= 10**12:
            safe[key] = value
    phases = values.get("phase_timings_ms")
    if isinstance(phases, dict):
        safe["phase_timings_ms"] = {
            key: value
            for key, value in phases.items()
            if key in PHASES and type(value) is int and 0 <= value <= 3_600_000
        }
    if not timestamp:
        match = re.match(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[,.]\d+)?", line)
        timestamp = match.group() if match else ""
    if not isinstance(timestamp, (str, int)):
        timestamp = ""
    fingerprint = hashlib.sha256(
        json.dumps([source, timestamp, event, safe], sort_keys=True).encode()
    ).hexdigest()[:20]
    return {
        "id": fingerprint,
        "source": source,
        "timestamp": timestamp,
        "event": event,
        "fields": safe,
    }


def parse_text(text: str, source: str) -> list[dict[str, Any]]:
    # CloudWatch exports from filter-log-events are accepted as well as JSONL/plain logs.
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        document = None
    if isinstance(document, dict) and isinstance(document.get("events"), list):
        return [
            event
            for item in document["events"]
            if isinstance(item, dict)
            and isinstance(item.get("message"), str)
            and (event := parse_line(item["message"], source, item.get("timestamp", "")))
        ]
    return [event for line in text.splitlines() if (event := parse_line(line, source))]


def percentile(values: list[int], fraction: float) -> int | None:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def build_snapshot(events: list[dict[str, Any]], sources: list[dict[str, Any]]) -> dict[str, Any]:
    unique = {event["id"]: event for event in events}
    groups: dict[str, dict[str, Any]] = {}
    for event in unique.values():
        fields = event["fields"]
        correlation = next(
            (
                key
                for key in ("decision_id", "request_id", "job_id", "diagnostic_id")
                if fields.get(key)
            ),
            None,
        )
        key = f"{correlation}:{fields[correlation]}" if correlation else "unlinked:" + event["id"]
        group = groups.setdefault(key, {"id": key, "events": [], "fields": {}})
        group["events"].append(event)
        group["fields"].update(fields)
        if (
            event["event"] == "backend_phase"
            and fields.get("phase") in PHASES
            and type(fields.get("duration_ms")) is int
        ):
            group["fields"].setdefault("phase_timings_ms", {})[fields["phase"]] = fields[
                "duration_ms"
            ]
    # A rejection/other record sharing an API request ID joins its decision if available.
    requests = {
        g["fields"]["request_id"]: g
        for g in groups.values()
        if "decision_id" in g["fields"] and "request_id" in g["fields"]
    }
    for key, group in list(groups.items()):
        if key.startswith("request_id:") and group["fields"].get("request_id") in requests:
            requests[group["fields"]["request_id"]]["events"].extend(group["events"])
            del groups[key]
    actions = [
        e["fields"]["capture_to_action_ms"]
        for e in unique.values()
        if e["event"] == "action_finished"
        and e["fields"].get("outcome") == "completed"
        and "capture_to_action_ms" in e["fields"]
    ]
    return {
        "groups": list(reversed(list(groups.values()))),
        "sources": sources,
        "summary": {
            "events": len(unique),
            "decisions": sum("decision_id" in g["fields"] for g in groups.values()),
            "completed": len(actions),
            "p50_ms": percentile(actions, 0.5),
            "p95_ms": percentile(actions, 0.95),
        },
        "updated_at": int(time.time() * 1000),
    }


class LogStore:
    def __init__(self, paths: list[Path]) -> None:
        self.paths = paths
        self.imports: dict[str, list[dict[str, Any]]] = {}
        self.cloud: deque[dict[str, Any]] = deque(maxlen=10000)
        self.cloud_status: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        events = []
        sources = []
        for path in self.paths:
            name = str(path)
            try:
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 2 * 1024 * 1024))
                    text = stream.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
                parsed = parse_text(text, name)
                events.extend(parsed)
                sources.append({"name": name, "status": "Watching", "events": len(parsed)})
            except OSError:
                sources.append({"name": name, "status": "Not available", "events": 0})
        with self.lock:
            for name, parsed in self.imports.items():
                events.extend(parsed)
                sources.append({"name": name, "status": "Imported snapshot", "events": len(parsed)})
            events.extend(self.cloud)
            sources.extend(self.cloud_status.values())
        return build_snapshot(events, sources)

    def poll_cloud(self, groups: list[str], profile: str | None, region: str) -> None:
        try:
            boto3 = importlib.import_module("boto3")
            config = importlib.import_module("botocore.config").Config(
                connect_timeout=3, read_timeout=5, retries={"max_attempts": 0}
            )
            client = boto3.Session(profile_name=profile, region_name=region).client(
                "logs", config=config
            )
        except Exception:
            with self.lock:
                for group in groups:
                    self.cloud_status[group] = {
                        "name": group,
                        "status": "AWS configuration unavailable",
                    }
            return
        cursors = {group: int((time.time() - 3600) * 1000) for group in groups}
        tokens: dict[str, str | None] = dict.fromkeys(groups)
        high_water = dict(cursors)
        seen: set[str] = set()
        while True:
            for group in groups:
                try:
                    token = tokens[group]
                    newest = high_water[group]
                    for _ in range(10):
                        reply = client.filter_log_events(
                            logGroupName=group,
                            startTime=cursors[group],
                            limit=1000,
                            **({"nextToken": token} if token else {}),
                        )
                        parsed = []
                        for item in reply.get("events", []):
                            newest = max(newest, item["timestamp"])
                            identifier = group + item["eventId"]
                            if identifier not in seen:
                                seen.add(identifier)
                                event = parse_line(item["message"], group, item["timestamp"])
                                if event:
                                    parsed.append(event)
                        with self.lock:
                            self.cloud.extend(parsed)
                        next_token = reply.get("nextToken")
                        if not next_token or next_token == token:
                            cursors[group] = max(cursors[group], newest - 1000)
                            tokens[group] = None
                            break
                        token = next_token
                        tokens[group] = token
                    else:
                        # Continue the next bounded batch from its pagination token.
                        pass
                    high_water[group] = newest
                    with self.lock:
                        self.cloud_status[group] = {"name": group, "status": "Live · last hour"}
                except Exception:
                    with self.lock:
                        self.cloud_status[group] = {
                            "name": group,
                            "status": "AWS read failed · check login and permissions",
                        }
            if len(seen) > 50000:
                seen.clear()
            time.sleep(10)


def make_handler(store: LogStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def send_payload(self, payload: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'",
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def local_request(self) -> bool:
            expected = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            origin = self.headers.get("Origin")
            return self.headers.get("Host") in expected and (
                not origin or origin in {"http://" + host for host in expected}
            )

        def do_GET(self) -> None:
            if not self.local_request():
                self.send_error(403)
                return
            route = urlparse(self.path).path
            if route == "/api/logs":
                self.send_payload(json.dumps(store.snapshot()).encode(), "application/json")
            elif route == "/":
                self.send_payload(
                    Path(__file__).with_name("buddy_log_viewer.html").read_bytes(),
                    "text/html; charset=utf-8",
                )
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            if not self.local_request() or self.path != "/api/import":
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BYTES:
                    self.send_error(413)
                    return
                payload = json.loads(self.rfile.read(size))
                name, text = payload["name"], payload["text"]
                if not isinstance(name, str) or not isinstance(text, str):
                    raise ValueError
                name = "Import: " + Path(name).name[:100]
                events = parse_text(text, name)
                with store.lock:
                    if name not in store.imports and len(store.imports) >= 20:
                        self.send_error(429)
                        return
                    store.imports[name] = events[-10000:]
                self.send_payload(json.dumps({"events": len(events)}).encode(), "application/json")
            except (ValueError, KeyError, TypeError, RecursionError):
                self.send_error(400)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "logs", nargs="*", type=Path, help="Additional backend/worker log files to watch"
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--cloudwatch", action="append", default=[], metavar="LOG_GROUP")
    parser.add_argument("--aws-profile")
    parser.add_argument("--aws-region", default="eu-central-1")
    args = parser.parse_args()
    root = Path(
        os.environ.get(
            "LOCALAPPDATA", os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
        )
    )
    desktop = root / "Buddy/logs/desktop.log"
    store = LogStore(
        [
            desktop.with_name(desktop.name + ".2"),
            desktop.with_name(desktop.name + ".1"),
            desktop,
            *args.logs,
        ]
    )
    if args.cloudwatch:
        threading.Thread(
            target=store.poll_cloud,
            args=(args.cloudwatch, args.aws_profile, args.aws_region),
            daemon=True,
        ).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store))
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Buddy observability: {url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
