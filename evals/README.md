# evals/ — sample briefs and expected checks

Run before every gate from Phase 2 on.

- `briefs/`: 5 sample campaign briefs, written once the `<confirm>` values in `brands/artec.yaml` are filled in. Copy `briefs/_template.yaml` to start each one.
- Each brief lists `seeded_violations`: problems deliberately planted (a banned claim, an unlisted price, a competitor name, a non-consented child asset). The Reviewer must catch every one.
- An eval passes when the pipeline produces a valid plan and drafts for every enabled channel, and the Reviewer flags all seeded violations.
