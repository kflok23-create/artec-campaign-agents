# Progress

Current phase: **1 — Data and assets (in progress)**

Gate reports are appended below by `/gate`, newest last. Each report records what was built, how to test it, what is still open, and the owner's decision.

| Phase | Name | Status | Gate passed |
| --- | --- | --- | --- |
| 0 | Foundation | Passed | 2026-10-18 |
| 1 | Data and assets | In progress | — |
| 2 | Thinking agents, dry run | Not started | — |
| 3 | Creative | Not started | — |
| 4 | Approval and first channels | Not started | — |
| 5 | TikTok and LinkedIn | Not started | — |
| 6 | Feedback loop | Not started | — |
| 7 | Hardening | Not started | — |

## Platform access applications

Details and official sources: [`PLATFORM_ACCESS.md`](PLATFORM_ACCESS.md).

| Platform | Application | Submitted | Status |
| --- | --- | --- | --- |
| TikTok | Developer app with Content Posting API (unaudited). Audit itself waits for Phase 5: it needs the finished integration. | — | Owner to register |
| LinkedIn | Community Management API, Development tier (needs business email and Page super admin) | — | Owner to apply |

## Decisions log

| Date | Decision | By |
| --- | --- | --- |
| 2026-10-17 | Railway project `artec-campaign-agents`, all services in Singapore (`asia-southeast1-eqsg3a`) | Kah |
| 2026-10-17 | Claude API daily cap $5.00 for Phases 0–2 (`brands/artec.yaml`), revisit before Phase 2 evals | Kah |
| 2026-10-17 | TikTok: try the API anyway; export mode until approved; attempt the audit in Phase 5 | Kah |
| 2026-10-17 | Deploy from GitHub `main` (deploys on push); repo stays public for now | Kah |
| 2026-10-17 | Brand time zone `Asia/Kuala_Lumpur` (MY and SG are both UTC+8) | Claude, flagged |
| 2026-10-17 | DB driver asyncpg (works on Windows dev and Linux); Railway settings recorded in `RAILWAY.md` because Config as Code is deprecated | Claude, flagged |
| 2026-10-18 | Asset tags from folder and file names plus one Claude Haiku look per image (tags, description, people/children); indexer budget $1.00 per run under the daily cap | Kah |
| 2026-10-18 | Consent by folder convention: files under a `consent-cleared` folder are cleared; everything else is not | Kah |
| 2026-10-18 | Public asset URLs served by `web` from Drive at unguessable `/media/a/<token>` URLs; only tagged assets that show no children or are consent-cleared | Kah |
| 2026-10-18 | Railway watch paths skip redeploys for docs-only commits | Kah |
| 2026-10-18 | Asset search is Postgres full-text search (no paid embedding service) | Claude, flagged |

## Gate reports

### Phase 0 — Foundation — 2026-10-17

**Gate criteria**

Times are UTC on 2026-10-17. Deployment IDs are Railway's.

| Criterion | Result | Evidence |
| --- | --- | --- |
| Deploys green | Met | `web` (`6831b285…`) and `worker` (`2f85cc59…`) both `SUCCESS` for commit `6ce9e5d`; Postgres `SUCCESS`. Railway environment status: 3 of 3 services online, 0 failures, 0 warnings. Migration ran in the pre-deploy step: `Running upgrade  -> 0001, Phase 0: agent_runs and spend_ledger.` `GET https://web-production-0c48.up.railway.app/health` returned `200 {"status":"ok","service":"web","db":"ok","commit":"6ce9e5d"}`. |
| The worker survives a restart | Met | Restarted at 22:54:36. Log, in order: `shutdown signal received` (SIGTERM), `worker stopped cleanly`, `worker starting`, `smoke check already passed for this deployment; skipping`, `heartbeat`. No second paid call; the deployment stayed `SUCCESS` with 0 crashes. The skip reads the earlier row from Postgres, so it also shows the row survived the restart. |
| One SDK call is logged with its cost | Met | 22:53:41 `agent call finished`: `agent=smoke`, `agent_run_id=1`, `model=claude-haiku-5-5`, `status=ok`, `cost_usd=0.000237`, 4 input and 137 output tokens (plus cached prompt tokens), 2,901 ms. Then `smoke check passed` with output `{"ok":true,"brand_name":"Artec","word_count":1}`: the model called our `get_brand_name` tool and returned schema-valid JSON. The ledger hold is settled before that log line is written. |

Tests: `uv run pytest`: 59 passed, 1 skipped (the paid live test; the production smoke call stands in
for it). `uv run ruff check .` and `uv run ruff format --check .` are clean.

**What was built**

- Railway project `artec-campaign-agents` in Singapore: `web`, `worker` and `Postgres` (volume in
  Singapore too), deployed from GitHub `main`. Settings in `docs/RAILWAY.md`.
- One Docker image for both services (`Dockerfile`), non-root, with the Agent SDK's bundled CLI, so
  no Node.js.
- Config: `services/config.py` reads environment variables (secrets as `SecretStr`) and validates
  `brands/artec.yaml` strictly. The locked rules (no generative video, no model-rendered text, fal.ai
  at most $2.50, no auto-approve, at most 2 revisions) cannot be loosened in YAML.
- Database: `agent_runs` and `spend_ledger` (`db/models.py`), Alembic migration `0001` with a lock so
  two services can migrate at once safely.
- Budget guard (`services/budget.py`): reserves a ceiling before every Claude call under a Postgres
  lock, refuses if the daily cap ($5.00, brand time zone) would be passed, then settles the actual
  cost. Tested with 10 parallel calls against a small cap: exactly the allowed number went through.
- Agent runner (`agents/runner.py`): every call writes one `agent_runs` row and one ledger entry,
  output is validated against a Pydantic schema and retried once, then fails. Agents are locked
  down: no built-in Claude Code tools, no settings files or memory, only their own tools, and every
  secret except the API key is blanked from the agent process. The runner checks the tool list the
  CLI reports and stops before any spend if anything extra appears.
- Worker (`worker/`): heartbeat loop, clean shutdown on SIGTERM, one smoke call per deployment
  (skipped on restart), and `python -m worker status` (today's spend against the cap and the last 10
  agent runs).
- Web (`app/`): `/health` with a database check; API docs pages turned off.
- Logs are JSON for Railway and redact secret values.
- Local development: `compose.yaml` (Postgres 17), `.env.example`, commands in `CLAUDE.md`.
- `docs/PLATFORM_ACCESS.md`: TikTok, LinkedIn and Meta requirements checked against official docs,
  with your checklists.

**How to test it**

1. Health: open https://web-production-0c48.up.railway.app/health. Expect `"status":"ok"`,
   `"db":"ok"` and the deployed commit.
2. Smoke call: Railway dashboard, `worker`, latest deployment, Logs. Find `smoke check passed` and
   the `agent call finished` line above it with `cost_usd`. A `heartbeat` line appears every 60 s.
3. Restart: `worker`, the deployment's menu, Restart. Within a minute the logs show `worker stopped
   cleanly`, then `smoke check already passed for this deployment; skipping`, and no new
   `agent call finished` line.
4. Locally: `docker compose up -d`, `uv sync`, `uv run pytest`, `uv run ruff check .`.
5. Optional, production tables: register an SSH key with Railway (`railway ssh keys github`), run
   `railway link` (pick `artec-campaign-agents`, `production`), then
   `railway ssh --service worker -- python -m worker status`.

**Cost so far**

- Claude API: $0.000237 (one settled `spend_ledger` row in production: the smoke call). Local test
  runs had no API key and recorded $0.
- fal.ai: $0 (no client until Phase 3).
- Railway hosting is not in `spend_ledger`; see Railway billing. Note that v1 is still running on
  Railway too (item 2 below).

**Open issues and questions**

1. **Platform applications not yet submitted.** The Phase 0 scope says to submit both. They need you;
   checklists are in `PLATFORM_ACCESS.md`. Neither blocks Phase 1.
   - TikTok: register the developer app now. The audit itself cannot go in during Phase 0: TikTok
     requires a demo of the finished end-to-end integration and a public website with Privacy Policy
     and Terms, and it rejects apps still in development. Its rules also list "A utility tool to help
     upload contents to the account(s) you or your team manages" as not acceptable, which describes
     this system, so plan on export mode for TikTok.
   - LinkedIn: needs a business email on Artec's domain and the super admin of the Artec Page.
     A rejected app cannot reapply, so it is worth getting these right first.
2. **v1 is still running.** The brief says v1 is retired, but Railway project `humble-endurance`
   ("Hermes Agent", with a volume) is online. Can you confirm it no longer posts anywhere? I
   recommend stopping it before Phase 4 at the latest, so two systems never post for Artec, and
   keeping its volume until we know nothing else needs carrying over. I have not touched it.
3. **Proposed brief corrections.** I have not edited `BUILD_BRIEF.md`; say which to apply.
   - a. Phase 0: "Submit the TikTok audit" becomes "Register the TikTok app"; the audit moves to
     Phase 5 (item 1).
   - b. Meta: posts made while the app is in Development mode are visible only to people with a role
     on the app. The Phase 4 gate ("first approved post live") needs the app switched to Live mode.
   - c. LinkedIn: add the Development tier requirements (business email, Page super admin, a new
     app with Community Management as its only product).
   - d. Replace the third-party links in "Channel publishing requirements" with the official pages
     listed in `PLATFORM_ACCESS.md`.
   - e. Data model: there is no table for the Analyst's daily digest and weekly report, which the
     Strategist reads. I propose a `reports` table in Phase 6.
   - f. "Run" is not defined, yet fal.ai is capped at $2.50 per run and `spend_ledger` is keyed by
     run. I lean towards one run = one post slot's creative production, across its channel variants
     and revisions. Needed by Phase 3.
   - g. Media hosting is not covered. Instagram fetches media from a public HTTPS URL, and TikTok
     only from a verified domain. Options: serve rendered media from `web` on a subdomain of
     Artec's domain (free; also satisfies TikTok verification), or a Railway bucket (paid; would need
     your approval). Needed by Phase 3.
4. **Public repo.** No secrets are in it, but the brand config and, from Phase 2, the agent prompts
   and brand voice will be. I recommend making it private before Phase 2. Railway's GitHub app would
   then need access to the private repo.
5. **Two commits are not on GitHub yet.** The docs commit `6757770` (its push was stopped when the
   machine ran low on memory) and this gate report. Every push to `main` redeploys both services and
   makes one smoke call (about $0.0003). Shall I push? I also suggest Railway watch paths, so commits
   that only touch `docs/` or Markdown files do not redeploy.
6. **Migration log lines show as errors in Railway.** Alembic writes plain text to stderr, so Railway
   marks `INFO` lines as `error`. Cosmetic; I will route Alembic through the JSON logger in Phase 1.
7. **Production tables.** I could not read them directly: `railway ssh` needs an SSH key on your
   Railway account. Today's evidence comes from logs. Register a key if you want me to check tables
   in later gates.
8. **Brand config still unconfirmed:** `audience`, `voice.tone`, `cadence`,
   `assets.drive_folder_id`, `approval.approvers[0]`. Phase 1 needs the Drive folder ID plus a
   Google service account with read access to the Shared Drive (`GOOGLE_SERVICE_ACCOUNT_JSON`).
   Phase 2 needs audience, voice and cadence.
9. **Agent SDK trade-offs, for the record.** Each call starts the bundled Claude Code CLI as a
   subprocess: about 3 s for the smoke call, and a 388 MB image. The cost recorded is the SDK's own
   estimate; `agent_runs.usage` also stores our figure computed from tokens, using the price table
   in `services/pricing.py` (dated 2026-10-06), which needs updating if prices change.

**Owner decision:** `passed` (Kah, 2026-10-18)
