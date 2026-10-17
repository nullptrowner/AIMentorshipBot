import logging

import structlog
from prometheus_client import Counter, Gauge, Histogram

TASKS = Counter("studio_tasks_total", "Task outcomes", ["kind", "outcome"])
GENERATIONS = Counter("studio_generations_total", "Generation outcomes", ["model", "status"])
GENERATION_SECONDS = Histogram(
    "studio_generation_seconds",
    "Generation latency",
    ["model"],
    buckets=(10, 30, 60, 120, 300, 600, 1200, 3600),
)


JOB_COUNTS = Gauge("studio_job_count", "Persisted generation count", ["model", "status"])
QUEUE_COUNTS = Gauge("studio_queue_count", "Persisted task counts", ["state"])
JOB_LATENCY = Gauge("studio_job_mean_seconds", "Mean persisted generation duration", ["model"])


def configure_logging(level: str) -> None:
    # No request bodies, headers, input/output URLs, tokens or exception messages are logged.
    logging.basicConfig(level=level, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
