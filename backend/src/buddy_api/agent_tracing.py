"""Opt-in, content-free agent observations; exporter failures never affect decisions."""

import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from contextvars import ContextVar
from functools import wraps
from importlib import import_module
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)
current_trace: ContextVar["DecisionTrace | None"] = ContextVar("agent_trace", default=None)


class AgentTracer:
    def __init__(self, client: Any = None) -> None:
        self.client = client

    @classmethod
    def from_environment(cls) -> "AgentTracer":
        if os.environ.get("BUDDY_LANGFUSE_ENABLED", "").lower() != "true":
            return cls()
        if not all(os.environ.get(key) for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")):
            logger.warning("Agent tracing unavailable: missing Langfuse configuration")
            return cls()
        try:
            return cls(import_module("langfuse").Langfuse())
        except Exception:
            logger.warning("Agent tracing unavailable: SDK initialization failed")
            return cls()

    def start(self, name: str, decision_id: UUID, **kwargs: Any) -> Any:
        if self.client is None:
            return None
        try:
            return self.client.start_observation(
                name=name, trace_context={"trace_id": decision_id.hex}, **kwargs
            )
        except Exception:
            return None

    def shutdown(self) -> None:
        if self.client is not None:
            with suppress(Exception):
                self.client.shutdown()


def finish(observation: Any, **kwargs: Any) -> None:
    if observation is not None:
        with suppress(Exception):
            observation.update(**kwargs)
        with suppress(Exception):
            observation.end()


class DecisionTrace:
    def __init__(self, tracer: AgentTracer) -> None:
        self.tracer = tracer
        self.decision_id = uuid4()
        self.root = tracer.start("presentation.decision", self.decision_id, as_type="agent")
        self.phase: Any = None

    def begin_phase(self, name: str) -> None:
        self.finish_phase()
        if self.root is not None:
            try:
                self.phase = self.root.start_observation(
                    name=name,
                    as_type="generation" if name == "selection" else "span",
                    **({"model": "command-a-03-2025"} if name == "selection" else {}),
                )
            except Exception:
                self.phase = None

    def finish_phase(self) -> None:
        finish(self.phase)
        self.phase = None


def trace_decision[**P, R](function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @wraps(function)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        request = kwargs.get("request")
        tracer = getattr(getattr(request, "app", None), "state", None)
        trace = DecisionTrace(getattr(tracer, "agent_tracer", AgentTracer()))
        token = current_trace.set(trace)
        trace.begin_phase("session")
        try:
            result = await function(*args, **kwargs)
            target = getattr(result, "target", None)
            trace.finish_phase()
            finish(
                trace.root,
                output={
                    "tool": getattr(target, "tool", "no_action"),
                    "reason": getattr(result, "reason", "unknown"),
                    "decision_source": getattr(result, "decision_source", "fallback"),
                },
                metadata={"decision_id": str(trace.decision_id)},
            )
            return result
        except BaseException:
            # Never hand exceptions to the SDK: messages may contain private input.
            trace.finish_phase()
            finish(trace.root, level="ERROR", status_message="decision_failed")
            raise
        finally:
            trace.finish_phase()
            current_trace.reset(token)

    return wrapped
