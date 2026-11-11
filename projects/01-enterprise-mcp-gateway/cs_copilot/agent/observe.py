"""OBSERVE: traces, logs, metrics, cost tracking, latency.

Target: OTel span per tool call at the gateway and in the agent; Langfuse for LLM tokens/cost; gateway audit rows carry the agent trace_id.
"""
import os

from agentcore import JSONLSink, Span, Tracer

from .guardrails import mask_pii


class OTelSink:
    """TODO(observability milestone): export each Span as an OpenTelemetry span (OTLP)."""
    def export(self, span: Span) -> None:
        raise NotImplementedError("OTelSink.export")


class LangfuseSink:
    """TODO(observability milestone): send think.* spans (model, tokens, cost) to self-hosted Langfuse."""
    def export(self, span: Span) -> None:
        raise NotImplementedError("LangfuseSink.export")


def get_tracer() -> Tracer:
    # Traces are masked at rest: the same PII mask as answers (Milestone 5).
    sinks = [JSONLSink(os.environ.get("TRACE_FILE", "traces.jsonl"), redact=mask_pii)]
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        sinks.append(OTelSink())
    if os.environ.get("LANGFUSE_HOST"):
        sinks.append(LangfuseSink())
    return Tracer(*sinks)
