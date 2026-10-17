# services/ — shared plain-code services

- `drive_index`: indexes the Shared Drive asset bank into `assets` (tags, kind, `consent_cleared`, public URL). Phase 1.
- `render`: draws final images with Pillow, including all overlay text. Phase 3.
- `fal_client`: fal.ai calls for background extension only, always behind the budget guard. Phase 3.
- `budget`: checks `spend_ledger` before every paid call and records cost after it; pauses the queue at a cap. Phase 0 onwards.
- `tokens`: encrypts, stores and refreshes platform tokens; warns before expiry. Phases 4 to 7.
