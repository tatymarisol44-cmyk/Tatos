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
    if _configured or not (settings.otel_enabled or settings.metrics_port):
        return
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import MetricReader
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create(
        {"service.name": settings.otel_service_name, "deployment.environment": settings.app_env}
    )
    readers: list[MetricReader] = []
    if settings.otel_enabled:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = (settings.otel_exporter_otlp_endpoint or "http://localhost:4318").rstrip("/")
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(f"{endpoint}/v1/traces")))
        trace.set_tracer_provider(provider)
        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(f"{endpoint}/v1/metrics")))
    if settings.metrics_port:
        # Pull model for Prometheus / Google Managed Prometheus, on its own port: the public
        # Gateway only routes the API port, so tenant-labelled metrics never leave the cluster.
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from prometheus_client import start_http_server

        readers.append(PrometheusMetricReader())
        start_http_server(settings.metrics_port, addr="0.0.0.0")  # noqa: S104 (pod-internal)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))
    _configured = True


def tracer() -> trace.Tracer:
    return trace.get_tracer("orchestrator")


_meter = metrics.get_meter("orchestrator")
# Second-scale buckets. The SDK default (0, 5, 10, 25 ... 10000) is meant for
# milliseconds: in seconds every request would land in the first bucket and p95 would be
# meaningless. 0.3 s is the latency SLO threshold for the API (docs/slo.md).
SECONDS_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 5, 10, 30, 60, 120)
ROUTED = _meter.create_counter("orchestrator.routed", description="Questions routed per agent")
BLOCKED = _meter.create_counter(
    "orchestrator.blocked", description="Requests blocked by guardrails"
)
LATENCY = _meter.create_histogram(
    "orchestrator.request.duration",
    unit="s",
    description="End-to-end orchestration latency",
    explicit_bucket_boundaries_advisory=SECONDS_BUCKETS,
)
TOKENS = _meter.create_counter("gen_ai.client.token.usage", description="LLM tokens consumed")

# --- SLO signals (docs/slo.md). Names are ours, so alert rules do not depend on the
# HTTP semantic-convention version of an instrumentation library.
HTTP_DURATION = _meter.create_histogram(
    "agency.http.server.duration",
    unit="s",
    description="API request duration by route template, method and status class",
    explicit_bucket_boundaries_advisory=SECONDS_BUCKETS,
)
LLM_CALLS = _meter.create_counter(
    "agency.llm.calls", description="Model calls by outcome: ok, error, circuit_open"
)
LLM_DURATION = _meter.create_histogram(
    "agency.llm.duration",
    unit="s",
    description="Model call duration (successful calls)",
    explicit_bucket_boundaries_advisory=SECONDS_BUCKETS,
)
CIRCUIT_OPENED = _meter.create_counter(
    "agency.circuit.opened", description="Times a circuit breaker opened, by dependency"
)
MODEL_INFLIGHT = _meter.create_up_down_counter(
    "agency.model_requests.inflight", description="Requests on model routes being served"
)
BUSY_REJECTED = _meter.create_counter(
    "agency.model_requests.busy", description="Model-route requests refused: pod at capacity"
)
SSE_CONNECTIONS = _meter.create_up_down_counter(
    "agency.sse.connections", description="Open streaming (SSE) responses"
)

# Operational gauges, refreshed by the background worker from the database (a gauge
# callback cannot await a query). Keys: reviews_pending, alert_oldest_open_age_seconds,
# outbox_pending, privacy_steps_due_soon.
OPS: dict[str, float] = {}


def _ops_gauge(key: str) -> Any:
    def callback(options: Any) -> Any:
        from opentelemetry.metrics import Observation

        return [Observation(OPS[key])] if key in OPS else []

    return callback


_meter.create_observable_gauge(
    "agency.reviews.pending",
    callbacks=[_ops_gauge("reviews_pending")],
    description="Answers held for human review, all tenants",
)
_meter.create_observable_gauge(
    "agency.alerts.oldest_open_age",
    unit="s",
    callbacks=[_ops_gauge("alert_oldest_open_age_seconds")],
    description="Age of the oldest unacknowledged crisis alert (0 when none)",
)
_meter.create_observable_gauge(
    "agency.outbox.pending",
    callbacks=[_ops_gauge("outbox_pending")],
    description="Campaign messages waiting to be delivered",
)
_meter.create_observable_gauge(
    "agency.privacy.steps_due_soon",
    callbacks=[_ops_gauge("privacy_steps_due_soon")],
    description="Privacy-case steps due within 24 hours or late (legal deadlines)",
)
