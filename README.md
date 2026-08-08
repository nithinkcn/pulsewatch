# pulsewatch

[![CI](https://github.com/nithinkcn/pulsewatch/actions/workflows/ci.yml/badge.svg)](https://github.com/nithinkcn/pulsewatch/actions/workflows/ci.yml)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Scheduled uptime monitoring for HTTP and TCP endpoints, with **alerting on state transitions rather than on individual failed probes**.

Built with FastAPI, Celery, PostgreSQL and Redis. Every endpoint is typed, every state change is tested, and the whole thing comes up with one `docker compose up`.

---

## Why this exists

I built a CCTV monitoring backend for a statewide government health deployment — thousands of cameras across hospitals, where a camera that quietly stops recording is a blind spot nobody discovers until footage is needed. This is the open-source distillation of the parts that turned out to matter.

The naive version of this service is about forty lines: loop over some URLs, `GET` each one, send an email on failure. It breaks in production for reasons that are not obvious until you have watched it happen:

| Naive approach | What goes wrong | What pulsewatch does |
|---|---|---|
| Alert on every failed poll | One endpoint down for six hours at a 60s interval = 360 alerts. The team mutes the channel by day two. | Alerts fire on **transitions**. One outage produces one incident. |
| Trust a single failed probe | One dropped packet pages someone at 3am. | A target must fail *N* times consecutively before it counts as down. |
| One Celery Beat entry per target | Beat's schedule is process state — every target edit needs a restart. | Beat runs **one** dispatcher task; targets are ordinary database rows. |
| Probe without a timeout | A host that accepts a connection and never replies holds a worker forever, silently starving every other check. | Every probe is bounded, and the bound is capped service-wide. |
| Stamp "last checked" when the probe finishes | A slow target gets re-enqueued on every tick while its first check is still running. Queue grows without bound. | Stamped at **dispatch** time. |
| Store one row per poll and query it for status | "What is down right now?" scans a table that grows forever. | Current state lives on `target`; outages live in `incident`. `check` is append-only history nobody queries for status. |

## Architecture

```
                    ┌──────────────┐
   Celery Beat ────▶│  dispatcher  │  every 10s: "which targets are due?"
   (one entry)      └──────┬───────┘
                           │ enqueues one job per due target
                           ▼
                    ┌──────────────┐     ┌────────────┐
                    │  run_check   │────▶│ PostgreSQL │  check + incident + target state
                    │  (N workers) │     └────────────┘
                    └──────┬───────┘
                           │ publishes only on transition
                           ▼
                    ┌──────────────┐
                    │ Redis pub/sub│
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │   FastAPI    │──▶ WS /ws/alerts   (live)
                    │  (M replicas)│──▶ REST /api/v1/*  (history + config)
                    └──────────────┘
```

**Two-stage dispatch.** Beat is deliberately dumb — it fires one cheap task on a fixed tick. That task asks PostgreSQL which targets are due, using each target's own interval, and fans out one job per target. Adding a target takes effect on the next tick with nothing restarted.

**Redis pub/sub between workers and sockets.** A worker has no idea which API replica an operator's browser is connected to. Publishing to a channel means any replica can relay to its own clients, so the API scales horizontally. Pub/sub is fire-and-forget by design: the durable record is the `incident` table, which the dashboard loads on connect. The socket carries liveness, not history.

**Sync workers, async API.** The request path is async (asyncpg) because it is IO-bound. Celery tasks are synchronous (psycopg) because driving an async engine from a Celery task means an event loop per task, and asyncpg connections do not outlive their loop. One `DATABASE_URL`, one set of models, two drivers.

### The state machine

The interesting logic is one pure function in [`app/checks/evaluator.py`](app/checks/evaluator.py) — no database, no clock, no network:

```python
def evaluate(state, *, probe_ok, failure_threshold, recovery_threshold) -> Evaluation:
    """Fold one probe result into a target's state."""
```

It returns the new state, whether the confirmed status changed, and whether an incident should be opened or closed. Because it is pure, the behaviour that actually matters — flap damping, one-incident-per-outage, not closing incidents that were never opened — is covered by fast unit tests with no fixtures at all. See [`tests/test_evaluator.py`](tests/test_evaluator.py).

## Quick start

```bash
git clone https://github.com/nithinkcn/pulsewatch.git
cd pulsewatch
cp .env.example .env
docker compose up --build
```

API docs at <http://localhost:8000/docs>.

Add something to watch:

```bash
curl -X POST localhost:8000/api/v1/targets \
  -H 'content-type: application/json' \
  -d '{
        "name": "example",
        "address": "https://example.com",
        "check_type": "http",
        "interval_seconds": 30,
        "timeout_seconds": 5,
        "failure_threshold": 3
      }'
```

Watch alerts live:

```bash
websocat ws://localhost:8000/ws/alerts
```

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/targets` | Register something to monitor |
| `GET` | `/api/v1/targets` | List, filterable by `group` and `status` |
| `PATCH` | `/api/v1/targets/{id}` | Partial update |
| `DELETE` | `/api/v1/targets/{id}` | Remove (cascades to checks and incidents) |
| `GET` | `/api/v1/targets/{id}/checks` | Recent probe history |
| `GET` | `/api/v1/incidents?open_only=true` | What is broken right now |
| `WS` | `/ws/alerts` | Live transition stream |
| `GET` | `/livez` · `/readyz` | Liveness (no dependencies) · readiness (checks them) |

## Local development

```bash
uv venv --python 3.13
uv pip install -e ".[dev]"

make test          # unit tests + integration tests against real Postgres
make lint          # ruff + mypy --strict
make migrate       # alembic upgrade head
```

Integration tests use [testcontainers](https://testcontainers.com/) — they start a real PostgreSQL instance, run the real migrations against it, and tear it down. No SQLite substitution, because the schema depends on PostgreSQL specifics (partial indexes, `make_interval`) that SQLite would silently not exercise.

Set `TEST_DATABASE_URL` to point the suite at a database you already have, and testcontainers is skipped entirely:

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/pulsewatch_test make test
```

CI uses that path with a service container, so the runner never has to manage nested containers.

## Design notes

A few decisions worth defending:

- **Enums are `VARCHAR` + `CHECK`, not native PostgreSQL enums.** Adding a value to a native enum requires `ALTER TYPE`, which historically could not run inside a transaction — an avoidable hazard in an automated deploy.
- **Failed probes are not retried.** A target being unreachable is the signal this system exists to capture, not an error to paper over. Only infrastructure faults on our side reach the Celery retry policy.
- **`worker_prefetch_multiplier = 1`.** Probe durations vary by three orders of magnitude between a healthy local service and a dead one. Prefetching lets one worker sit on a batch of slow checks while another idles.
- **`redirects` are not followed on HTTP probes.** A `301` to a login page is not a healthy service, and following it silently would hide exactly that.
- **Alerts are published after the transaction commits.** Publishing inside would risk announcing an outage that then rolled back.
- **`check` is pruned on a schedule; `incident` is kept forever.** One is unbounded machine exhaust, the other is the record with long-term value.

## Not built here

Scope is deliberate. This is a monitoring core, not a product:

- No notification channels (email, Slack, PagerDuty) — the transition event is published; delivery is a separate concern.
- No UI. The WebSocket and REST API are the interface.
- No auth. It is an internal service; put it behind your gateway.
- No multi-region probing, no TLS-expiry checks, no synthetic browser checks.

## License

MIT
