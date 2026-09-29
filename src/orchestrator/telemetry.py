"""OpenTelemetry setup. Spans follow the GenAI semantic conventions where they exist."""

from __future__ import annotations

import logging

from opentelemetry import metrics, trace

from orchestrator.config import Settings

_configured = False


def setup_telemetry(settings: Settings) -> None:
    global _configured
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if _configured or not settings.otel_enabled:
        return
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {"service.name": settings.otel_service_name, "deployment.environment": settings.app_env}
    )
    endpoint = (settings.otel_exporter_otlp_endpoint or "http://localhost:4318").rstrip("/")
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(f"{endpoint}/v1/metrics"))
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
    _configured = True


def tracer() -> trace.Tracer:
    return trace.get_tracer("orchestrator")


_meter = metrics.get_meter("orchestrator")
ROUTED = _meter.create_counter("orchestrator.routed", description="Questions routed per agent")
BLOCKED = _meter.create_counter(
    "orchestrator.blocked", description="Requests blocked by guardrails"
)
LATENCY = _meter.create_histogram(
    "orchestrator.request.duration", unit="s", description="End-to-end orchestration latency"
)
TOKENS = _meter.create_counter("gen_ai.client.token.usage", description="LLM tokens consumed")
