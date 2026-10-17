---
description: Write the gate report for the current phase and stop for sign-off
---

The current phase is finished or blocked. Write its gate report and stop.

1. Read `docs/PROGRESS.md` to confirm the current phase, and the phase's gate criteria in `docs/BUILD_BRIEF.md`.
2. Run the test suite and, from Phase 2 on, the evals in `evals/`. Do not skip failures; report them.
3. Append a report to the "Gate reports" section of `docs/PROGRESS.md` with these headings:
   - **Phase N — <name> — <today's date>**
   - **Gate criteria:** each criterion from the brief, marked met / not met, with the evidence (command output, log line, URL).
   - **What was built:** files and services, briefly.
   - **How to test it:** exact commands or clicks the owner can run.
   - **Cost so far:** Claude API and fal.ai spend from `spend_ledger`, if it exists yet.
   - **Open issues and questions:** anything unresolved, plus any place the brief looks wrong.
   - **Owner decision:** leave as `pending`.
4. Update the phase table at the top of `docs/PROGRESS.md` (status only; leave "Gate passed" blank).
5. Commit with the message `Gate report: Phase N`.
6. Show me the report and stop. Do not start the next phase until I reply "Gate passed".

When I reply "Gate passed": set the owner decision to `passed`, fill in today's date in the phase table, move "Current phase" to the next phase, commit, then start that phase.
