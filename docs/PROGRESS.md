# Progress

Current phase: **0 — Foundation (in progress)**

Gate reports are appended below by `/gate`, newest last. Each report records what was built, how to test it, what is still open, and the owner's decision.

| Phase | Name | Status | Gate passed |
| --- | --- | --- | --- |
| 0 | Foundation | In progress | — |
| 1 | Data and assets | Not started | — |
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

## Gate reports
