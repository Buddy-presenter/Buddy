"""Tracing is private, isolated per task, and unable to change agent behavior."""

import asyncio
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest

from buddy_api.agent_tracing import AgentTracer, current_trace, trace_decision
from buddy_api.trace_execution import execution_records


class Observation:
    def __init__(self, records, **values):
        self.records = records
        self.values = values
        records.append(values)

    def start_observation(self, **values):
        return Observation(self.records, **values)

    def update(self, **values):
        self.values.update(values)

    def end(self):
        self.values["ended"] = True


class Client:
    def __init__(self):
        self.records = []

    def start_observation(self, **values):
        return Observation(self.records, **values)


def test_disabled_does_not_import_sdk(monkeypatch):
    monkeypatch.delenv("BUDDY_LANGFUSE_ENABLED", raising=False)
    monkeypatch.setitem(sys.modules, "langfuse", None)
    assert AgentTracer.from_environment().client is None


def test_task_isolation_and_no_private_content():
    client = Client()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent_tracer=AgentTracer(client)))
    )

    @trace_decision
    async def decide(*, request, text):
        trace = current_trace.get()
        trace.finish_phase()
        trace.begin_phase("selection")
        await asyncio.sleep(0)
        assert current_trace.get() is trace
        return SimpleNamespace(
            decision_id=trace.decision_id,
            target=SimpleNamespace(tool="next_slide"),
            reason="stay",
            decision_source="model",
        )

    async def run():
        return await asyncio.gather(
            decide(request=request, text="PRIVATE SPEECH"),
            decide(request=request, text="PRIVATE DECK"),
        )

    first, second = asyncio.run(run())
    assert first.decision_id != second.decision_id
    roots = [r for r in client.records if r["name"] == "presentation.decision"]
    assert {r["trace_context"]["trace_id"] for r in roots} == {
        first.decision_id.hex,
        second.decision_id.hex,
    }
    assert all(r["ended"] for r in client.records)
    assert "PRIVATE" not in repr(client.records)
    assert current_trace.get() is None


def test_exporter_failure_and_application_exception():
    class BrokenClient:
        def start_observation(self, **values):
            raise RuntimeError("PRIVATE EXPORTER ERROR")

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent_tracer=AgentTracer(BrokenClient())))
    )

    @trace_decision
    async def decide(*, request):
        return 42

    assert asyncio.run(decide(request=request)) == 42

    client = Client()
    request.app.state.agent_tracer = AgentTracer(client)

    @trace_decision
    async def fail(*, request):
        raise ValueError("PRIVATE INPUT")

    with pytest.raises(ValueError, match="PRIVATE INPUT"):
        asyncio.run(fail(request=request))
    assert "PRIVATE" not in repr(client.records)
    assert all(r["ended"] for r in client.records)
    assert current_trace.get() is None


def test_execution_import_allowlists_and_deduplicates(tmp_path):
    identifier = UUID("12345678-1234-4234-8234-123456789abc")
    record = (
        f"presentation_latency event=action_finished decision_id={identifier} "
        "source_format=pdf tool=next_slide outcome=completed capture_to_action_ms=200 "
        "proposal_to_action_ms=20 preflight_ms=0 executor_ms=10 transcript=PRIVATE\n"
    )
    path = tmp_path / "desktop.log"
    path.write_text(record * 2 + record.replace("executor_ms=10", "executor_ms=-1"))
    records = list(execution_records([path]))
    assert len(records) == 1
    assert records[0][0] == identifier
    assert records[0][1]["executor_ms"] == 10
    assert "PRIVATE" not in repr(records)


def test_sdk_exports_real_parented_spans_without_network():
    langfuse = pytest.importorskip("langfuse")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    client = langfuse.Langfuse(
        public_key="pk-lf-offline-test",
        secret_key="sk-lf-offline-test",
        span_exporter=exporter,
    )
    tracer = AgentTracer(client)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent_tracer=tracer)))

    @trace_decision
    async def decide(*, request):
        trace = current_trace.get()
        trace.finish_phase()
        trace.begin_phase("selection")
        return SimpleNamespace(
            decision_id=trace.decision_id,
            target=SimpleNamespace(tool="highlight_region"),
            reason="topic",
            decision_source="model",
        )

    try:
        result = asyncio.run(decide(request=request))
        client.flush()
        spans = exporter.get_finished_spans()
        assert {span.name for span in spans} == {"presentation.decision", "session", "selection"}
        root = next(span for span in spans if span.name == "presentation.decision")
        assert root.context.trace_id == int(result.decision_id.hex, 16)
        assert all(
            span.parent.span_id == root.context.span_id for span in spans if span is not root
        )
        assert all(span.context.trace_id == root.context.trace_id for span in spans)
    finally:
        tracer.shutdown()
