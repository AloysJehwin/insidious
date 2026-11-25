"""Prometheus metrics for Insidious (names and buckets fixed by docs/CONTRACTS.md)."""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, generate_latest

BUCKETS = (0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1, 1.5, 2, 3, 5, 8)
UNMATCHED_ROUTE = "unmatched"

REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency by route template",
    ["route", "method", "status"],
    buckets=BUCKETS,
)
REQUESTS_TOTAL = Counter("http_requests_total", "HTTP requests by route template", ["route", "method", "status"])
FEATURE_SHED_TOTAL = Counter(
    "insidious_feature_shed_total", "Times a degradable feature returned its fallback", ["feature"]
)
FLAG_STATE = Gauge("insidious_flag_state", "Boolean flag state as last evaluated at environment level", ["flag"])
ORDERS_TOTAL = Counter("insidious_orders_total", "Orders placed", ["checkout_flow", "gateway"])
PAYMENT_FAILURES_TOTAL = Counter(
    "insidious_payment_failures_total", "Payment gateway failures seen at checkout", ["gateway"]
)

# Database pool / query metrics (insidious.db). ``db`` label = primary | replica.
DB_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5)
DB_POOL_SIZE = Gauge("insidious_db_pool_size", "Max connections the pool can hand out (pool_size + max_overflow)",
                     ["db"])
DB_POOL_IN_USE = Gauge("insidious_db_pool_in_use", "Connections currently checked out of the pool", ["db"])
DB_POOL_WAIT_SECONDS = Histogram(
    "insidious_db_pool_wait_seconds", "Time from asking the pool for a connection until getting one", ["db"],
    buckets=DB_BUCKETS,
)
DB_QUERY_SECONDS = Histogram(
    "insidious_db_query_seconds", "SQL statement execution time", ["db", "op"], buckets=DB_BUCKETS,
)


def route_template(scope: dict[str, Any]) -> str:
    """The matched route's template (e.g. /product/{id}); never the raw path, to keep label cardinality bounded."""
    route = scope.get("route")
    path = getattr(route, "path_format", None) or getattr(route, "path", None)
    return str(path) if path else UNMATCHED_ROUTE


class MetricsMiddleware:
    """Pure ASGI middleware: records http_request_duration_seconds and http_requests_total per route template."""

    def __init__(
        self,
        app: Callable,
        exclude_routes: Iterable[str] | None = None,
        route_aliases: dict[str, str] | None = None,
    ) -> None:
        self.app = app
        self.exclude = set(exclude_routes or ())
        # e.g. {"/api/checkout": "/checkout"} so API and page traffic share the SLO route label guardian queries
        self.aliases = dict(route_aliases or {})

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: dict) -> None:
            if message.get("type") == "http.response.start":
                status_holder["status"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = route_template(scope)
            if route not in self.exclude:
                route = self.aliases.get(route, route)
                method = scope.get("method", "GET")
                status = str(status_holder["status"])
                REQUEST_DURATION.labels(route=route, method=method, status=status).observe(time.perf_counter() - start)
                REQUESTS_TOTAL.labels(route=route, method=method, status=status).inc()


def set_flag_gauges(env_states: dict[str, Any]) -> None:
    for name, val in env_states.items():
        if isinstance(val, bool):
            FLAG_STATE.labels(flag=name).set(1 if val else 0)


def render_latest() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def metrics_endpoint_factory(before_render: Callable[[], None] | None = None):
    """Return a Starlette endpoint serving /metrics."""
    from starlette.responses import Response

    # async on purpose: rendering is cheap, and a sync endpoint would queue behind saturated
    # threadpool work under load, so Prometheus scrapes would time out exactly when they matter.
    async def metrics_endpoint(request: Any) -> Response:
        if before_render is not None:
            try:
                before_render()
            except Exception:  # noqa: BLE001
                pass
        body, ctype = render_latest()
        return Response(body, media_type=ctype)

    return metrics_endpoint
