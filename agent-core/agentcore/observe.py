"""OBSERVE layer: traces, logs, metrics, cost tracking and latency for every layer.

Sinks are pluggable: InMemorySink (tests), JSONLSink (local files), and in later
milestones an OpenTelemetry exporter and a Langfuse exporter behind the same Sink protocol.
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator, Protocol


@dataclass
class Span:
    trace_id: str
    name: str                       # "<layer>.<operation>", e.g. "act.get_sales_order"
    layer: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    start: float = field(default_factory=time.time)
    end: float | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"

    @property
    def latency_ms(self) -> float:
        return ((self.end or time.time()) - self.start) * 1000


class Sink(Protocol):
    def export(self, span: Span) -> None: ...


class InMemorySink:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    def export(self, span: Span) -> None:
        self.spans.append(span)


class JSONLSink:
    def __init__(self, path: str):
        self.path = path

    def export(self, span: Span) -> None:
        with open(self.path, "a") as f:
            f.write(json.dumps({**asdict(span), "latency_ms": span.latency_ms}, default=str) + "\n")


LAYERS = {"perceive", "think", "remember", "plan", "act", "guardrails", "observe", "harness", "agent"}


class Tracer:
    def __init__(self, *sinks: Sink):
        self.sinks = sinks or (InMemorySink(),)
        self.cost_usd = 0.0
        self.tokens = 0

    @contextmanager
    def span(self, trace_id: str, name: str, **attrs: Any) -> Iterator[Span]:
        layer = name.split(".", 1)[0]
        if layer not in LAYERS:
            raise ValueError(f"span name must start with a layer name, got {name!r}")
        s = Span(trace_id=trace_id, name=name, layer=layer, attrs=dict(attrs))
        try:
            yield s
        except Exception as e:
            s.status = f"error: {type(e).__name__}"
            raise
        finally:
            s.end = time.time()
            for sink in self.sinks:
                sink.export(s)

    def record_llm(self, span: Span, resp: Any) -> None:
        if resp is None:
            return
        span.attrs.update(model=resp.model, input_tokens=resp.input_tokens,
                          output_tokens=resp.output_tokens, cost_usd=resp.cost_usd)
        self.cost_usd += resp.cost_usd
        self.tokens += resp.input_tokens + resp.output_tokens
