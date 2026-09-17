
# Running Upstrace on PostgreSQL

Upstrace speaks two SQL dialects. Everything that differs between them —
`DESCRIBE` versus `information_schema.columns`, float rounding, bulk insert,
parameter style — lives in `src/upstrace/dialect.py` behind a seven-method
interface. Nothing else in the codebase knows which warehouse it is talking to,
which is why the commands below are identical to the DuckDB ones.

Both dialects run in CI on every push. See
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

## Install

```bash
pip install "upstrace[postgres]"
```

The `postgres` extra pulls `psycopg`. The default install leaves it out, because
most people running the DuckDB demo do not need a Postgres driver.

You will also need `dbt-postgres`:

```bash
pip install "dbt-postgres==1.9.*"
```

## The worked example

`postgres-demo/` is a complete dbt project: one source table, three models, a
deterministic seed, and a fault you can inject. It exists so the Postgres path
has something reproducible to be tested against — the CI job runs exactly what
follows.

### 1. Start Postgres

```bash
docker run -d --name upstrace-pg \
  -e POSTGRES_USER=upstrace \
  -e POSTGRES_PASSWORD=upstrace \
  -e POSTGRES_DB=analytics \
  -p 5433:5432 \
  postgres:16
```

Port 5433 keeps this out of the way of a Postgres you may already run on 5432.
It is the port `postgres-demo/upstrace.yml` and `profiles.yml` both expect.

### 2. Seed and build

```bash
cd postgres-demo

docker exec -i upstrace-pg psql -U upstrace -d analytics < seed.sql
dbt run --profiles-dir .
```

`seed.sql` writes 90,000 orders across 90 days using `generate_series` — no
`random()`, no `now()`. Two people running it get byte-identical data, which is
the only way the fault test below means anything.

### 3. Check the configuration

```bash
upstrace config
```

dialect: postgres

warehouse: postgresql://upstrace:***@localhost:5433/analytics

The password is masked. `upstrace config` output is meant to be pasteable into
an issue.

### 4. Profile, twice

```bash
upstrace profile
upstrace profile
```

Two runs over unchanged data. In `previous_run` mode drift compares the last two
profile runs, so one run would compare against whatever came before it.

### 5. Confirm it stays quiet

```bash
upstrace drift --fail-on warning
```

No drift above threshold. Nothing moved.

Half the value of a data quality tool is not crying wolf. If this step is not
silent, nothing after it is trustworthy.

## Injecting a fault

```bash
docker exec -i upstrace-pg psql -U upstrace -d analytics < fault.sql
dbt run --profiles-dir .
upstrace profile

upstrace drift
upstrace rca
```

`fault.sql` scales the last 30 days of `raw.orders.amount` by 1.6 — a plausible
upstream change, not a corrupted file. Nothing goes null and no constraint is
violated. Every dbt model still builds green.

INCIDENT 1 - CRITICAL root: orders (source)
columns affected : amount
also drifted : agg_daily_orders, fct_orders, stg_orders
evidence:

- amount.distinct_count: 137.0000 -> 274.0000 (100.0%)
- amount.mean_value: 35.2144 -> 56.3430 (60.0%) since 2024-03-01
- amount.mean_value: 35.1582 -> 42.1915 (20.0%)
- amount.max_value: '60.320000' -> '96.512000'

1 root cause(s) explain 4 affected node(s).

Two things in that output are worth reading twice.

**The root is the source, not the aggregate.** Four nodes drifted. The rule —
*a node is a root if it drifted and none of its ancestors did* — names `orders`
and files the other three as blast radius. Twenty-two signals were raised in
total and twenty-one of them are downstream noise.

**The same column appears twice at different magnitudes.** Per-day the mean
moved 60.0%; measured over the whole table it moved 20.0%. The fault touches 30
days out of 90, so a whole-table average dilutes it by two thirds. Per-partition
profiling is what keeps a one-month change from reading as a rounding error.

To start over, re-run `seed.sql` — it drops with `CASCADE`, so it is safe to run
over an existing build.

## Your own Postgres project

In your `upstrace.yml`:

```yaml
dialect: postgres
warehouse: postgresql://user:password@host:5432/database
```

That is the whole change. `UPSTRACE_DSN` overrides the connection string if you
would rather keep the credential out of the file, and `UPSTRACE_DIALECT`
overrides the dialect.

Requirements are the same as on DuckDB: a dbt project whose `manifest.json`
Upstrace can read, and at least one date or timestamp column on the tables you
care about, so per-day profiles have an axis to group on.

## What does not work on Postgres yet

- 
- **The fault-injection benchmark.** `upstrace eval` is a test fixture built on DuckDB's `read_parquet`, not part of the engine.
- **The dashboard.** The FastAPI app (`uvicorn upstrace.api:app`) reads DuckDB
  directly rather than going through the dialect layer. On a Postgres project it
  returns a 503 saying so. `upstrace report` produces the same incident view as
  a self-contained HTML file and does work.

## Adding a third warehouse

Subclass `Dialect` in `src/upstrace/dialect.py`, implement the seven abstract
methods, and register it in `get_dialect()`. Nothing outside that file changes.
`DuckDBDialect` and `PostgresDialect` are about sixty lines each and sit side by
side as reference.

One note from writing the second one: the Postgres driver is wrapped, not
swapped. `_PgConnection` makes `psycopg` honour the contract DuckDB's connection
already had — `execute()` returns something fetchable, `?` placeholders are
translated to `%s` — so roughly a hundred call sites did not have to change.
Adapting the driver to the codebase is cheaper than adapting the codebase to the
driver.
