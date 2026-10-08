"""OpenTelemetry setup. Spans follow the GenAI semantic conventions where they exist."""

from __future__ import annotations

import logging
import re
from typing import Any

from opentelemetry import metrics, trace

from orchestrator.config import Settings
from orchestrator.guardrails import redact_pii

_configured = False

# Telegram puts the bot token in the URL path (/bot<id>:<secret>/sendMessage), and HTTP
# client libraries log request URLs at INFO.
_BOT_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def _scrub(text: str) -> str:
    return redact_pii(_BOT_TOKEN.sub("bot<redacted>", text))[0]


def redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Neither credentials embedded in URLs nor personal data (e-mail, phone, cédula,
    card, SSN) may stay in a log record, including the traceback of a logged exception.
    Logs are technical, not the audit trail: they must hold no PHI."""
    message = record.getMessage()
    clean = _scrub(message)
    if clean != message:
        record.msg, record.args = clean, None
    if record.exc_info and not record.exc_text:
        record.exc_text = logging.Formatter().formatException(record.exc_info)
    if record.exc_text:
        record.exc_text = _scrub(record.exc_text)
    return record


class SecretRedactingFilter(logging.Filter):
    """The same redaction as a filter, for handlers configured outside this process's
    record factory (e.g. a library that builds records itself)."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_record(record)
        return True


def install_log_redaction() -> None:
    """Redact every record when it is CREATED, whatever logger or handler it goes to.
    (A logger-level filter would only see records of that exact logger, not its children.)"""
    current = logging.getLogRecordFactory()
    if getattr(current, "_redacting", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        return redact_record(current(*args, **kwargs))

    factory._redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


install_log_redaction()


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
