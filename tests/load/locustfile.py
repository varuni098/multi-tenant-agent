"""Load test for the agent API: docker compose --profile load run --rm locust

Read-only traffic (it never changes your dev data): theme questions and page listings that
reach the model, the settings view, and off-topic prompts that the harness answers without
the model. Each simulated user signs in as one of the seeded users, round robin, so the
per-user rate limit (20/min) is not what's being measured.

Tune OLLAMA_NUM_PARALLEL (compose, default 1) with this: more parallel requests raise
throughput until the GPU saturates, at the cost of per-request latency and VRAM.
"""

import itertools
import random

from locust import HttpUser, between, task

from app.harness.auth import create_token

USERS = itertools.cycle(
    [
        ("a1b2c3d4-e5f6-7890-abcd-ef0123456789", "viewer@acme.test", "viewer"),
        ("a1b2c3d4-e5f6-7890-abcd-ef0123456789", "editor@acme.test", "editor"),
        ("a1b2c3d4-e5f6-7890-abcd-ef0123456789", "admin@acme.test", "admin"),
        ("b2c3d4e5-f6a7-8901-bcde-f01234567890", "admin@globex.test", "admin"),
    ]
)
READ_PROMPTS = [
    "What theme is the login page using?",
    "Which pages do we have?",
    "What is the layout of the login page?",
    "Is the login page dark or light?",
]
OFF_TOPIC = ["What's the capital of France?", "Tell me a joke"]


class AgentUser(HttpUser):
    wait_time = between(2, 5)

    def on_start(self) -> None:
        tenant, email, role = next(USERS)
        self.client.headers["Authorization"] = f"Bearer {create_token(tenant, email, role, 120)}"

    @task(5)
    def ask(self) -> None:
        self.client.post("/v1/chat", json={"prompt": random.choice(READ_PROMPTS)}, name="/v1/chat (model)")

    @task(2)
    def settings(self) -> None:
        self.client.get("/v1/settings")

    @task(1)
    def off_topic(self) -> None:
        self.client.post("/v1/chat", json={"prompt": random.choice(OFF_TOPIC)}, name="/v1/chat (no model)")
