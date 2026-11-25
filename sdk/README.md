# insidious

Festive-season readiness SDK for self-hosted Python web apps. When a sale night brings 10x traffic, Insidious lets
you shed heavy features with a feature flag instead of a deploy, emits the SLO metrics an alerting loop needs, and
lets you resize your database pool (or move reads to a replica) live from Flagsmith.

- **Flag-driven load shedding**: `@fk.degradable("recommendations", fallback=...)` returns a cheap fallback whenever
  the flag (or its parent `heavy_features`) is off. Flags come from [Flagsmith](https://flagsmith.com) with local
  evaluation, or from a YAML file when no key is set (offline mode).
- **SLO metrics**: per-route latency histograms and request counters for Prometheus, plus shed and order counters.
- **DB scaling hooks**: SQLAlchemy pool and query metrics, pool size from remote config, read/write split to a
  replica behind a flag.

## Install

```bash
pip install insidious            # flags, degradation, metrics
pip install "insidious[db]"      # + SQLAlchemy 2 and psycopg 3 for the database helpers
```

Python 3.10+.

## Quickstart (FastAPI)

```python
from fastapi import FastAPI
from sqlalchemy import text
from insidious import Insidious

fk = Insidious.from_env()            # FLAGSMITH_SERVER_KEY unset = offline defaults from flags.yaml
app = FastAPI()
fk.install(app)                       # metrics middleware + GET /metrics

@fk.degradable("recommendations", fallback=lambda pid: [])
def recommend(pid: int) -> list[int]:
    return expensive_model(pid)       # skipped while the flag is off

@app.get("/product/{pid}")
def product(pid: int):
    with fk.database().read().connect() as conn:   # replica when read_from_replica is ON
        row = conn.execute(text("SELECT name FROM products WHERE id = :id"), {"id": pid}).first()
    return {"name": row and row[0], "recs": recommend(pid)}
```

Also available: `fk.is_enabled(flag)`, `fk.config("payment_gateway", default="razorpay")`,
`fk.variant("checkout_flow", identity=session_id, traits={"cart_value": 2400})`, `fk.identify(...)` and
`fk.track("order_placed", identity=..., properties={...})`.

## Configuration

| Env var | Meaning |
|---|---|
| `FLAGSMITH_SERVER_KEY` | Server-side environment key. Unset = offline mode (defaults from `flags.yaml`). |
| `FLAGSMITH_API_URL` | Self-hosted Flagsmith API URL (optional). |
| `FLAGSMITH_REFRESH_SECONDS` | Local-evaluation refresh interval (default 5). |
| `INSIDIOUS_CONFIG` / `INSIDIOUS_FLAGS` | Paths to `insidious.yaml` and `flags.yaml` (searched in cwd and parents otherwise). |
| `INSIDIOUS_DEV=1` | Allow local flag overrides (`fk.set_dev_overrides`) for rehearsals without a Flagsmith admin key. |
| `DATABASE_URL` | Primary database for `fk.database()`. Unset = SQLite file in the temp dir (`INSIDIOUS_SQLITE_URL` overrides). |

## Flags it expects

| Flag | Type | Default | Used for |
|---|---|---|---|
| `heavy_features` | flag | on | Parent switch: off sheds every child below at once |
| `recommendations`, `hd_images`, `live_reviews` | flag | on | Degradable features (children of `heavy_features`) |
| `checkout_flow` | multivariate | `full` | A/B test (`full` / `lite`), bucketed by identity |
| `payment_gateway` | remote config | `razorpay` | Failover target |
| `festive_collection` | flag | off | Progressive rollout |
| `read_from_replica` | flag | off | Route reads to `db_read_url` |
| `db_read_url` | remote config | `""` | SQLAlchemy URL of the read replica |
| `db_pool` | remote config (JSON) | `{"pool_size":5,"max_overflow":0,"timeout_s":2}` | Live pool sizing |
| `agent_config`, `ai_agent` | remote config / flag | see repo | Used by the Insidious ops agent, not by the SDK |

Segments are evaluated too: the default `high_value_cart` segment (trait `cart_value > 2000`) keeps
`heavy_features` on for big carts while everyone else is shed.

## Metrics it emits

| Metric | Type | Labels |
|---|---|---|
| `http_request_duration_seconds` | histogram | `route` (template, e.g. `/product/{id}`), `method`, `status` |
| `http_requests_total` | counter | `route`, `method`, `status` |
| `insidious_feature_shed_total` | counter | `feature` |
| `insidious_flag_state` | gauge | `flag` (1/0) |
| `insidious_orders_total` | counter | `checkout_flow`, `gateway` |
| `insidious_db_pool_size` | gauge | `db` = `primary`/`replica` (pool_size + max_overflow) |
| `insidious_db_pool_in_use` | gauge | `db` |
| `insidious_db_pool_wait_seconds` | histogram | `db` (asking the pool for a connection until getting one) |
| `insidious_db_query_seconds` | histogram | `db`, `op` = `select`/`insert`/`update`/`other` |

Example alert inputs: checkout p95
`histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{route="/checkout"}[30s])) by (le))`,
pool utilisation `sum(insidious_db_pool_in_use{db="primary"}) / sum(insidious_db_pool_size{db="primary"})`.

## Database scaling

```python
db = fk.database()        # FlagRoutedDatabase: primary from DATABASE_URL
db.write()                # always the primary engine (orders, carts)
db.read()                 # replica engine while read_from_replica is ON and db_read_url is set, else primary
db.stats()                # {"primary": {...pool numbers...}, "replica": {...} | None, "read_from_replica": bool}
```

- Flag state is re-checked at most every 5 s. When `db_pool` changes, a new primary engine is built and the old one
  is disposed; in-flight connections finish on the old pool. Invalid JSON keeps the running pool.
- `postgres://` and `postgresql://` URLs use psycopg 3. Postgres engines get `pool_pre_ping` and a 3 s connect
  timeout; passwords are masked in `stats()` and logs.
- Instrument any other engine with `from insidious import instrument_engine; instrument_engine(engine, db="primary")`.

## Links

Full project (store demo, guardian SLO loop, Claude ops agent, Kubernetes manifests):
https://github.com/aloysjehwin/insidious

MIT licensed.
