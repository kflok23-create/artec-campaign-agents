# publishers/ — one adapter per channel

Plain code, no agents. Built in Phases 4 (Instagram, Facebook) and 5 (TikTok, LinkedIn).

Every adapter exposes the same interface: publish an approved variant with an idempotency key, check whether a post already exists, and fetch metrics.

- `instagram`: Content Publishing API, create-then-publish; read the live post cap before publishing.
- `facebook`: Pages API, `POST /{page-id}/feed` with a Page access token.
- `tiktok`: Content Posting API, Direct Post; posts stay private until the content audit passes.
- `linkedin`: Posts API `/rest/posts` as the organization; needs Community Management API approval.
- `export`: builds a ready-to-post package for any channel not yet live.

Before building each adapter, check the platform's current official docs and flag any difference from the brief.
