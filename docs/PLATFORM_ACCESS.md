# Platform access: what the owner needs to set up

Checked against official platform documentation on 2026-10-17. The brief's platform sources were
third-party blogs; these are the official pages. Each adapter's phase re-checks them before
building it.

## TikTok — Content Posting API (decision 2026-10-17: try the API; export mode until approved)

What TikTok's rules say:

- Until an app passes TikTok's audit, Direct Post content is `SELF_ONLY`, the posting account must
  be set to private, and at most 5 users can post in 24 hours.
  [content-sharing-guidelines](https://developers.tiktok.com/doc/content-sharing-guidelines)
- The audit needs a demo video of "the complete end-to-end flow of the up-to-date integrations",
  "an externally facing fully developed website" with Privacy Policy and Terms of Service links
  visible without a menu, and "Apps that are still in development or testing will not be
  approved." [app-review-guidelines](https://developers.tiktok.com/doc/app-review-guidelines)
- **Risk:** the intended-use rules list as "Not acceptable: A utility tool to help upload contents
  to the account(s) you or your team manages." That describes this system, so the audit may be
  refused. Export mode is the fallback either way.
- The posting UI must load fresh creator info, make the user pick a privacy level from a dropdown
  with no default, leave interaction toggles off by default, offer commercial-content disclosure,
  show a preview and an explicit consent line before upload. Our approval inbox would have to host
  this (Phase 5).
- Photos are pulled from a verified domain or URL prefix (`PULL_FROM_URL`), JPEG or WebP, up to
  1080p. A subdomain on Artec's own domain allows DNS verification.
  [media transfer guide](https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide)
- Tokens: access 24 hours, refresh 365 days.

Phase 0 actions (owner):

- [ ] Create a TikTok developer account and register an app with the Content Posting API
      (sandbox/unaudited). Do not submit the audit yet: it needs the finished integration.
- [ ] Decide where the public website with Privacy Policy and Terms lives (needed for the audit).
- [ ] Decide on a media subdomain on Artec's domain (also helps Meta and LinkedIn).

## LinkedIn — Community Management API, Development tier

- Posting as the Company Page (`/rest/posts`, `w_organization_social`) needs the Community
  Management API. [increasing access](https://learn.microsoft.com/en-us/linkedin/marketing/increasing-access)
- Development tier application: a registered legal organisation, a **verified business email**
  ("Personal email addresses won't pass"), legal name, registered address, website and privacy
  policy. The app must be **new, with the Community Management API as its only product**, and be
  verified by a **super admin of the LinkedIn Page**. Finish the vetting survey within 21 days.
  A rejected app cannot reapply.
  [app review](https://learn.microsoft.com/en-us/linkedin/marketing/community-management-app-review)
- Development tier allows 500 calls per app per day, which should cover Artec's volume. Standard
  tier needs a screencast of the working integration (Phase 5 at the earliest).
- Tokens: access 60 days; refresh tokens (1 year, not extended by refreshing) after approval.
  [refresh tokens](https://learn.microsoft.com/en-us/linkedin/shared/authentication/programmatic-refresh-tokens)
- No scheduling in the API (our scheduler times posts); `Linkedin-Version` header required.

Phase 0 actions (owner):

- [ ] Confirm who is super admin of the Artec LinkedIn Page.
- [ ] Get a business email on Artec's own domain for the application.
- [ ] Create a new LinkedIn developer app, associate it with the Artec Page, have the super admin
      verify it, then request Community Management API (Development tier).

## Meta — Instagram and Facebook Page (Phase 4, nothing to submit now)

- App Review is not needed for a business's own accounts with Standard Access.
  [Instagram app review](https://developers.facebook.com/docs/instagram-platform/app-review)
- **But:** "Any data generated while an app is in Development mode, such as test posts, can only be
  seen by role users." Posts become public only once the app is switched to Live, so the Phase 4
  gate needs the app in Live mode. [app modes](https://developers.facebook.com/docs/development/build-and-test/app-modes)
- Instagram accepts JPEG only; carousels up to 10 items; the 24-hour post limit is read live from
  `content_publishing_limit` (the docs give both 50 and 100).
- Facebook Page posting needs `pages_manage_posts`, a Page token, and the CREATE_CONTENT, MANAGE
  and MODERATE tasks on the Page.

Phase 0 actions (owner):

- [ ] Check whether the v1 Meta developer app is still active (brief, "Decisions to confirm").
- [ ] Confirm the Instagram account is a professional account linked to the Facebook Page.
