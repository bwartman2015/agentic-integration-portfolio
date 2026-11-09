"""OBSERVE: traces, logs, metrics, cost tracking, latency.

Target: Agent traces kept separate from the monitored system's telemetry; MTTR and auto-resolved % on Grafana; cost per incident in Langfuse.
"""
import os

from agentcore import JSONLSink, Span, Tracer


class OTelSink:
    """TODO(observability milestone): export each Span as an OpenTelemetry span (OTLP)."""
    def export(self, span: Span) -> None:
        raise NotImplementedError("OTelSink.export")


class LangfuseSink:
    """TODO(observability milestone): send think.* spans (model, tokens, cost) to self-hosted Langfuse."""
    def export(self, span: Span) -> None:
        raise NotImplementedError("LangfuseSink.export")


def get_tracer() -> Tracer:
    sinks = [JSONLSink(os.environ.get("TRACE_FILE", "traces.jsonl"))]
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        sinks.append(OTelSink())
    if os.environ.get("LANGFUSE_HOST"):
        sinks.append(LangfuseSink())
    return Tracer(*sinks)
