# db/ — models and migrations

Built in Phase 1. Tables and key columns are listed in `docs/BUILD_BRIEF.md` under "Data model".

- Every schema change goes through a migration; never edit the production database by hand.
- `publications.idempotency_key` is unique.
- `channel_tokens.token` is encrypted with `TOKEN_ENCRYPTION_KEY`.
