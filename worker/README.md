# worker/ — scheduler and state machine

Always-on Railway service. Built from Phase 0 onwards.

- Polls `tasks` with `SELECT ... FOR UPDATE SKIP LOCKED` and runs one step per task.
- Moves each post variant through the lifecycle in `docs/BUILD_BRIEF.md` ("Post lifecycle"). Only this code changes a variant's status.
- Calls agents and checks their output against `schema.py`; retries invalid output once, then fails the task.
- Calls publishers only for approved variants, only when `PUBLISHING_ENABLED=true`, always with an idempotency key.
- Runs the budget guard before every paid call.
- Must survive a restart with no lost or duplicated work.
