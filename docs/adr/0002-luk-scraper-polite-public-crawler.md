# ADR-0002 — luk-scraper: a polite crawler for Luk's public job offers

- **Status:** accepted (2026-09-23)
- **Deciders:** maintainer

## Context

`luk-scraper` exports Luk's (takealuk.com) public job offers to JSONL/CSV, for personal job
search, alerts or analysis. Recon on 2026-09-23 found:

1. **The data is public.** The listing `/job_offers` and the detail pages `/job_offers/{slug}`
   need no login, paywall or captcha. Cards and a full `schema.org/JobPosting` JSON-LD block are
   in the server-rendered HTML (Rails + Hotwire/Turbo). The listing paginates as `?page=N`.
2. **robots.txt allows it.** `User-agent: * → Allow: /`, then `Disallow`s for private and
   account pages (`/profile`, `/saved_jobs`, `/users/*` sign-in/up pages, `/onboarding`,
   `/admin/`, `/flipper`), the employer back-office sub-paths (`/companies/profile`,
   `/companies/profile/edit`, `/companies/job_offers/`, `/companies/sign_in`,
   `/companies/registration`), Turbo-frame partials (`/job_offers/*/recommendations`), `/blog`,
   `/wp-*`/`/wordpress*`/`/backup*` probes and crawl-budget variants (`?sort_by=`, `?locale=`).
   Listing and detail pages are not disallowed. (Re-checked live 2026-09-24; the verbatim group
   is kept in `luk-scraper/tests/conftest.py` and the tests run against it.)
3. **There is no public JSON API** (`/job_offers.json` → 406, `/api/*` → 404), so the crawler
   parses HTML.
4. **Luk is multi-country** (Chile, Colombia, Peru, Mexico, Brazil); the listing accepts a
   `countries[]` filter.

## Decision

- **Public pages only, no login, ever.** The crawler never sends a session cookie and never
  touches `/users/*`, `/profile`, `/saved_jobs` or any other disallowed path.
- **Honour robots.txt, with no way around it.** It is checked before every crawl, and a
  disallowed URL aborts the run. The public API has no switch to skip or replace it (`crawl()`
  has no `check_robots`/`robots_policy`, and `RobotsPolicy` has no allow-all mode). The site's
  policy is installed on the fetcher, which checks every request before sending it, including
  the full query and each redirect hop. Redirects may land only on `/job_offers` or
  `/job_offers/<slug>`, with an exact path boundary. `?sort_by=` and `?locale=` (also `sort_by[]`,
  any case) are never sent.
- **No credentials, ever.** An injected HTTP session is scrubbed before every request: cookies,
  `Cookie`/`Authorization`/`Proxy-Authorization` headers and netrc.
- **Identify honestly.** User-Agent `luk-scraper/<version> (+<contact>)`, where the contact is an
  http(s) URL the operator sets (`LUK_SCRAPER_CONTACT`, default: this project's GitHub page).
  E-mail addresses are refused, so nobody's address ends up in other sites' logs. The fetcher
  rejects any other User-Agent, so it can never imitate a browser.
- **Be slow.** ≥2 s between requests by default, with a hard floor of 1 s.
- **Back off, then stop.** 403/429/5xx trigger exponential backoff that honours `Retry-After`;
  when retries run out the crawl stops. No header rotation, proxies or other evasion.
- **Chile by default,** `countries[]=Chile` **plus `worldwide=1`**, so the scope doesn't depend on
  the operator's IP. Without `worldwide=1`, Luk ANDs the country filter with the visitor's geo-IP
  location (checked live 2026-09-24 from Chile: `countries[]=Colombia` returned 0 offers,
  1430 with `worldwide=1`; `countries[]=Chile` gave 1890 vs 1894). Country names are validated
  against Luk's five checkbox values (Brasil, Chile, Colombia, México, Perú).
- **PK = the offer slug,** Luk's own canonical URL key. The numeric `identifier.value` from the
  JSON-LD is kept as `external_id`. The site repeats cards across pages, so offers are deduped
  by slug.
- **Output to files** (JSONL or CSV), nothing else. `luk-assist` can read them.

## Consequences

- A full Chile crawl is roughly 120 listing pages plus one detail request per offer if
  enrichment is on, so it takes a while by design. `--date 3d` gives cheap incremental runs.
- The site's HTML can change; parser failures surface as errors, never as silent bad data.
- **Escape hatch.** If Luk blocks the honest UA, changes robots.txt, objects to this use, or
  ships an official API/feed: stop and revisit this ADR.
