# Railway setup

Project **artec-campaign-agents** (workspace "Kah Fai Lok's Projects"), environment `production`.
All services run in **Southeast Asia, Singapore** (`asia-southeast1-eqsg3a`).

Railway's Config as Code (`railway.toml` / `railway.json`) is deprecated: new services cannot use
it, and existing files stop being read on 2026-12-01. Its replacement, Infrastructure as Code
(`.railway/railway.ts`), needs Node and does not yet document Dockerfile paths, restart policy or
database regions. Until it does, the settings live in Railway and this file records them. If you
change a setting in the dashboard, update this file in the same commit.

## Services

| | web | worker | Postgres |
| --- | --- | --- | --- |
| Source | GitHub `kflok23-create/artec-campaign-agents`, branch `main` | same | image `ghcr.io/railwayapp-templates/postgres-ssl:18` |
| Build | `Dockerfile` | `Dockerfile` (same image) | — |
| Start command | `python -m app` | `python -m worker` | template default |
| Pre-deploy | `python -m alembic upgrade head` | `python -m alembic upgrade head` | — |
| Healthcheck | `/health`, 120 s timeout | none (no HTTP port) | — |
| Restart policy | on failure, 10 retries | always | template default |
| Draining | default | 30 s (lets an in-flight agent call finish) | 60 s (template) |
| Replicas | 1 | 1 | 1 |
| Public domain | yes (`/health`; inbox from Phase 4) | none | none (private network only) |

Both services run migrations before they deploy. An advisory lock in `db/migrations/env.py` makes
the second one wait and then find the schema already at head. The worker also refuses to start
work until the schema matches its code (`wait_for_schema`).

## Variables

Never paste secret values into this file, a prompt or a log. Secrets are set by the owner in the
Railway dashboard.

| Variable | web | worker | Notes |
| --- | --- | --- | --- |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` | `${{Postgres.DATABASE_URL}}` | private network URL |
| `BRAND` | `artec` | `artec` | selects `brands/<brand>.yaml` |
| `PUBLISHING_ENABLED` | `false` | `false` | global kill switch; stays false until Phase 4 |
| `LOG_LEVEL` | `INFO` | `INFO` | |
| `ANTHROPIC_API_KEY` | — | **secret, set by owner** | the web service never calls Claude |
| `WORKER_HEARTBEAT_SECONDS` | — | `60` | |
| `WORKER_SMOKE_ON_BOOT` | — | `true` | Phase 0 smoke call, once per deployment |
| `SMOKE_MODEL` | — | `claude-haiku-5-5` | |

Railway provides `RAILWAY_DEPLOYMENT_ID`, `RAILWAY_GIT_COMMIT_SHA` and `PORT` on every deployment.

## Useful commands

```bash
railway link                      # once: pick artec-campaign-agents / production
railway logs --service worker     # live worker logs
railway ssh --service worker      # shell in the worker container, then:
python -m worker status           #   recent agent_runs and today's Claude spend
```
