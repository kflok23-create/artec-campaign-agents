# app/ — FastAPI web service

Built in Phase 0 (`/health`) and Phase 4 (approval inbox).

- `/health`: liveness for Railway.
- Approval inbox: list variants in `awaiting_approval` and `escalated`, show media, caption and Reviewer reasons; approve, reject with a note, or edit the caption then approve. Protected by `INBOX_USERNAME` / `INBOX_PASSWORD`.
- Export packages: download final media plus caption for channels in export mode.
- OAuth callbacks for Meta, TikTok and LinkedIn; tokens are stored encrypted in `channel_tokens`.
- A cost view from `spend_ledger` (Phase 7).

The web service never runs agents or publishes; it only writes approval rows. The worker does the rest.
