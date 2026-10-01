"""Load profile of a clinic tenant: mostly chat, plus the dashboards staff keep open.

    uv run --group load locust -f loadtest/locustfile.py --headless \
        -u 40 -r 10 -t 60s --host http://127.0.0.1:8000 --csv out/run

The tenant key comes from LOAD_API_KEY (default "load-key"; the server needs it in
API_KEYS). Run the server with LLM_BACKEND=fake to measure the orchestrator itself
(API, graph, Postgres, guardrails) without a model provider's latency; with real models
the provider's latency dominates. See docs/load-test.md for method and results."""

from __future__ import annotations

import os
import random
import uuid

from locust import HttpUser, between, events, task

KEY = {"X-API-Key": os.environ.get("LOAD_API_KEY", "load-key")}
QUESTIONS = [
    "How do we deploy this service with Docker and Kubernetes?",
    "Necesito una estrategia de contenido para LinkedIn",
    "Our React bundle is 4MB, how do we speed it up?",
    "¿Cómo mejoro el SEO de la página de la clínica?",
    "Write a short onboarding email for new patients",
]


@events.test_start.add_listener
def seed(environment: object, **_: object) -> None:
    """A few patients with visits, so the dashboards read real rows."""
    import httpx

    host = getattr(environment, "host", None) or "http://127.0.0.1:8000"
    with httpx.Client(base_url=host, headers=KEY, timeout=30) as client:
        for i in range(30):
            pid = f"load-{i}"
            client.post("/v1/crm/patients", json={"id": pid, "display_name": f"Paciente {i}"})
            client.post(
                "/v1/crm/appointments",
                json={"patient_id": pid, "starts_at": "2026-09-01T10:00:00-05:00", "price": 40},
            )


class ClinicStaff(HttpUser):
    wait_time = between(0.5, 2.0)

    @task(6)
    def chat(self) -> None:
        # A new conversation each time: every request writes fresh checkpoints.
        self.client.post(
            "/v1/chat",
            json={"question": random.choice(QUESTIONS), "thread_id": uuid.uuid4().hex},  # noqa: S311
            headers=KEY,
            name="POST /v1/chat",
        )

    @task(2)
    def insights(self) -> None:
        self.client.get("/v1/insights/summary", headers=KEY, name="GET /v1/insights/summary")

    @task(1)
    def agenda(self) -> None:
        self.client.get("/v1/crm/appointments", headers=KEY, name="GET /v1/crm/appointments")

    @task(1)
    def catalog(self) -> None:
        self.client.get("/v1/agents", headers=KEY, name="GET /v1/agents")
