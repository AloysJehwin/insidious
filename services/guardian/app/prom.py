"""Prometheus HTTP API client and the PromQL from CONTRACTS.md."""
from __future__ import annotations

import math
import re

import httpx

from .log import log


class PromError(Exception):
    """Prometheus unreachable or returned an error (distinct from an empty result)."""


def _route_matcher(route: str, api_alias: bool) -> str:
    """Exact match per CONTRACTS.md. With api_alias, also match the JSON API twin
    (`/api/checkout` for `/checkout`) because k6 drives the JSON API."""
    if not api_alias or route.startswith("/api/"):
        return f'route="{route}"'
    twin = "/api" + route if route != "/" else "/api/"
    return f'route=~"{re.escape(route)}|{re.escape(twin)}"'


def p95_query(route: str, window: str = "30s", api_alias: bool = False) -> str:
    m = _route_matcher(route, api_alias)
    return f"histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{{{m}}}[{window}])) by (le))"


def error_rate_query(route: str, window: str = "30s", api_alias: bool = False) -> str:
    m = _route_matcher(route, api_alias)
    return (
        f'sum(rate(http_requests_total{{{m},status=~"5.."}}[{window}])) '
        f"/ clamp_min(sum(rate(http_requests_total{{{m}}}[{window}])), 1e-9)"
    )


def rps_query(window: str = "30s") -> str:
    return f"sum(rate(http_requests_total[{window}]))"


REPLICAS_QUERY = 'count(up{job="store-api"} == 1)'

# Database pool SLO (metrics from the SDK's insidious.db). Utilisation = sum(in_use) / sum(size).
DB_ROUTE_KEY = "db:primary"


def db_in_use_query(db: str = "primary") -> str:
    return f'sum(insidious_db_pool_in_use{{db="{db}"}})'


def db_size_query(db: str = "primary") -> str:
    return f'sum(insidious_db_pool_size{{db="{db}"}})'


def db_utilisation_query(db: str = "primary") -> str:
    return f"{db_in_use_query(db)} / clamp_min({db_size_query(db)}, 1)"


def db_wait_p95_query(db: str = "primary", window: str = "30s") -> str:
    return (f'histogram_quantile(0.95, sum(rate(insidious_db_pool_wait_seconds_bucket{{db="{db}"}}[{window}])) '
            f"by (le))")


class PromClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient):
        self.base_url = base_url.rstrip("/")
        self.client = client

    async def scalar(self, query: str) -> float | None:
        """Return the first sample's value; None for an empty result or NaN. Raises PromError on failure."""
        try:
            r = await self.client.get(f"{self.base_url}/api/v1/query", params={"query": query})
        except httpx.HTTPError as e:
            raise PromError(f"{type(e).__name__}: {e}") from e
        if r.status_code != 200:
            raise PromError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            body = r.json()
        except ValueError as e:
            raise PromError("non-JSON response") from e
        if body.get("status") != "success":
            raise PromError(str(body.get("error") or body)[:200])
        data = body.get("data") or {}
        result = data.get("result")
        if data.get("resultType") == "scalar" and isinstance(result, list) and len(result) == 2:
            return _num(result[1])
        if not result:
            return None
        try:
            return _num(result[0]["value"][1])
        except (KeyError, IndexError, TypeError):
            log("prom_unexpected_shape", query=query)
            return None


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f
