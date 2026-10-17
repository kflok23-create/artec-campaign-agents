# Artec Campaign Agents

A multi-agent marketing system for Artec. It runs 24/7 on Railway and turns a campaign brief into reviewed, human-approved posts for Instagram, Facebook, TikTok and LinkedIn.

**Status:** scaffold only. Claude Code builds it phase by phase from [`docs/BUILD_BRIEF.md`](docs/BUILD_BRIEF.md). Progress is tracked in [`docs/PROGRESS.md`](docs/PROGRESS.md).

## How it works

```text
Campaign brief
  -> Strategist -> Asset Curator -> Copywriter -> Creative Producer -> Reviewer
  -> Human approval (inbox) -> Publishers (Instagram, Facebook, TikTok, LinkedIn)
  -> metrics -> Analyst -> weekly report -> Strategist
```

- Code orchestrates; six agents make the judgment calls.
- Postgres is the shared board: every step reads and writes rows, so any task can restart after a crash.
- Nothing publishes without a human approval and `PUBLISHING_ENABLED=true`.

## Getting started with Claude Code

1. Clone the repo and open it in Claude Code.
2. Run `/kickoff`. Claude Code reads the brief, restates the architecture, asks its questions, then starts Phase 0.
3. At the end of each phase Claude Code runs `/gate` and stops. Review the report in `docs/PROGRESS.md`.
4. Reply **"Gate passed"** to move to the next phase.

Before Phase 2, fill in the `<confirm>` values in [`brands/artec.yaml`](brands/artec.yaml). The open questions are listed at the end of the brief.

## Secrets

Copy `.env.example` to `.env` for local runs. In production, set the same names as Railway variables. Never commit `.env`.

## Repo layout

| Folder | What goes there |
| --- | --- |
| `app/` | FastAPI web service: approval inbox, OAuth callbacks, `/health` |
| `worker/` | Scheduler loop and the post state machine |
| `agents/` | One folder per agent: `prompt.md`, `schema.py`, `tools.py` |
| `publishers/` | One adapter per channel, plus `export` for manual-post packages |
| `services/` | Drive indexer, fal.ai client, Pillow renderer, budget guard, token manager |
| `brands/` | Brand configs (`artec.yaml`) |
| `db/` | Models and migrations |
| `evals/` | Sample briefs and expected checks, run before each gate |
| `docs/` | Build brief, kickoff prompt, progress log |
