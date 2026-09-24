# Luk endpoints used by luk-cli

**Unofficial; not affiliated with, endorsed or sponsored by Luk or Buk.**

Reverse-engineered surface of https://www.takealuk.com (Rails 8 + Hotwire/Turbo,
`html[lang="es-CL"]`) as luk-cli uses it. Source of truth for behaviour: [spec.md](spec.md) (spec v3);
governance: [ADR-0001](../../docs/adr/0001-luk-cli-read-only-personal-client.md)
(`docs/adr/0001-luk-cli-read-only-personal-client.md` at the repository root).

**Placeholders.** Employer names, offer slugs and titles seen live are replaced in this document
with fake ones (`empresa-demo-01`, `Empresa Demo 01 SpA`, `analista-demo-empresa-demo-01`, …),
and Luk's public Algolia credentials with `<ALGOLIA_APP_ID>` / `<ALGOLIA_SEARCH_KEY>`. Endpoints,
params, selectors, status codes, totals and dates are as observed.

**Verification markers**

- **recon 2026-09-23**: seen live in the anonymous recon run (honest UA
  `luk-cli/0.1 (recon; personal read-only client)`); the page's structure is kept in a fake fixture.
- **Phase 0 2026-09-23**: seen live in the Phase-0 run (§4 below).
- **smoke 2026-09-24**: re-checked live through the finished `luk` command (§6 below).
- _(unverified — needs login)_: not observed yet. Confirm with `luk debug capture` after the
  first login (spec §8.1), then update this file with the date.

**Rules for every takealuk.com request** (spec §1, §4.7, §4.9)

- `GET` only, `https` only, host `www.takealuk.com` only (`LUK_BASE_URL`). Redirects are followed
  by hand (max 5), and every hop is checked the same way.
- Headers: `User-Agent: luk-cli/<version> (personal read-only client)` and `Accept-Language: es-CL`.
  HTML requests send `Accept: text/html,application/xhtml+xml`; JSON requests send
  `Accept: application/json`.
- Never send the `Turbo-Frame` header (a frame response drops the layout and `html[lang]`).
  Never send `locale=` or `sort_by=`. Never read or cache the CSRF token, because GETs need none.
- Rate: at least 1.5 s between requests across all luk processes, and at most 240 requests per
  rolling hour.
- **Auth** column: `anon` = the anonymous client, whose jar refuses every cookie. `session` = the
  private client, carrying `_portal_de_empleos_session`.

## 1. Public endpoints

### 1.1 `GET /` (home)

| | |
|---|---|
| Auth | `anon` (Algolia discovery, `doctor --live`) · `session` (`whoami`) |
| Params | none |
| Verified | recon 2026-09-23 (anonymous) |
| Fixture | `root.html` |

- **Response:** full HTML.
- **Anonymous marker:** `header#main-header a[href="/users/sign_in"]`.
- **Algolia config.** Discover it at runtime from this page; never hardcode or commit it.
  - `[data-search-pills-application-id-value]`: the app id (`^[A-Z0-9]{10}$`), written here as
    `<ALGOLIA_APP_ID>`.
  - `[data-search-pills-search-api-key-value]`: the public search-only key, written here as
    `<ALGOLIA_SEARCH_KEY>`.
  - `input[data-query-suggestions-index]`: the index, `JobOffer_query_suggestions`.
- **Logged-in markers** _(unverified — seen in logged-in markup during design, never captured)_:
  - `[aria-label^="Avatar de"]`
  - `#header-user-menu .header-dropdown-name`
  - `.header-dropdown-email`

### 1.2 `GET /job_offers` (job search)

| | |
|---|---|
| Auth | `anon` |
| Verified | recon 2026-09-23; params and pagination in Phase 0 2026-09-23 |
| Fixtures | `search_analista.html`, `search_p2.html`, `search_worldwide.html`, `search_zero.html`, `search_last.html`, `search_past_last.html`, `search_single_page.html`, `takealuk_list.html` |

| Param | Format | Status | Notes |
|---|---|---|---|
| `job_positions` | pills joined with `,` (≤50 chars each, ≤512 UTF-8 bytes in total) | recon | Pills are OR'd. Zero pills is allowed per spec §5.1 (not exercised in Phase 0). |
| `locations` | area ids joined with `,` | recon | Echoed in `input#locations` (`value` plus `data-initial-area-options` JSON). |
| _(no location param)_ | — | Phase 0 ✓ | The server applies the visitor's geo-IP default. For a visitor in Chile that is 1021 Chile (same total as explicit `locations=1021`). luk-cli always sends a location mode. |
| `worldwide` | `1` | **Phase 0 ✓** | Clears the location default. 735 results vs 274 for Chile. `input#locations` becomes `value=""` and **has no** `data-initial-area-options` attribute. The site JS (`syncWorldwideParam`) sends it when the location pill is empty. |
| `page` | int ≥ 1 | recon + Phase 0 | Past the last page: `200`, 0 cards, same total (see traps). |
| `job_types[]` | repeated: `full_time` `part_time` `contractor` `intern` `per_diem` `other` | **Phase 0 ✓** | |
| `date` | `last_day` `last_3_days` `last_week` `last_month` `last_3_months` `last_6_months` | **Phase 0 ✓** | |
| `min_salary` | integer | **Phase 0 ✓** | Returns only offers that show a salary. Observed overlap semantics: offer max ≥ bound. |
| `max_salary` | integer | **Phase 0 ✓** | Returns only offers that show a salary. Observed overlap semantics: offer min ≤ bound. |
| `salary_currency` | `CLP` `COP` `PEN` `MXN` `BRL` | **Phase 0 ✓** | The select re-renders enabled and `selected` once a bound is present. A bound without a currency behaved like CLP (4 = 4). |
| `countries[]` | repeated, accented UTF-8: `Brasil` `Chile` `Colombia` `México` `Perú` | **Phase 0: only together with `worldwide=1`** | Without `locations` and without `worldwide`, the server ANDs it with the geo-IP default (Chile), so every non-Chile country returns 0. With `worldwide=1&countries[]=Colombia` it returns 288, all in Colombia. |
| `company_names`, `sort_by`, `locale` | — | never sent | `company_names` is JS-only; the other two are robots-disallowed variants. |

**Response.** Full HTML, containing `form#flexible-search-form` (GET `/job_offers`). The filter
controls sit outside the form and carry `form="flexible-search-form"`.

- **Results frame:** `turbo-frame#job_offers_results`.
  - Total: the integer in `.job-offers-results-count b`.
- **Cards:** `div.job-offer-card[data-scroll-restore-slug]`, 15 per page, relevance-sorted.
  - Title: `h2.job-offer-card__title` (collapse whitespace).
  - Company: `p.item-title`; empty means confidential.
  - Location: `span.break-words`.
  - Salary: `span.tag-primary`.
  - Labels: `span.tag-navy`.
  - Age: the `.item-subtitle > span` starting "Hace".
  - Each card also holds a `save_later` POST form. Never submit it.
- **Salary tag formats seen:**
  - `CLP $1.000.000 - $1.200.000` (illustrative): `.` is the thousands separator.
  - `COP $3.000.000,00 - $3.000.000,00` (illustrative): this one also has a **decimal comma**.
- **Labels (`span.tag-navy`) seen:** `Jornada Completa`, `Presencial`, `Prácticas`, `20 horas`,
  `30 horas`.
- **Ages seen:** `Hace menos de 1 hora`, `Hace N horas`, `Hace N día(s)`, `Hace N mes(es)`. The
  age may be absent.
- **Pagination** is a `nav.pagination-nav` inside the frame:
  - Other pages: `a[aria-label="Página N"]`.
  - Current page: `span[aria-current="page"].pagination-item-current`. Its text is N, and it has
    **no aria-label**.
  - Next: `a[aria-label="Página siguiente"]`. On the last page it is a
    `span[aria-disabled="true"]`.
  - Locale links (`?locale=en&page=2`) sit in the footer, outside the nav.
  - A search with ≤15 results has **no nav**.
  - Luk positions a list by `page=N` alone (its pagination links change nothing else).
    luk-cli's cursor (`next_page` + `next_offset`; `--offset` / MCP `offset`) is client-side:
    it requests `page=next_page` and drops the first `next_offset` cards. The same holds for
    `/companies` (24 per page) and the private lists.
- **Related roles:** `a.similar-role-suggestion-pill[data-role-name]`. There are 3 on normal
  pages and 0 on a zero-result nonsense query.
- **Zero results:**
  - The frame is present, `.job-offers-results-count b` = `0`, there are no cards, no nav and no
    related-role pills.
  - The empty-state text is "No hay ofertas que coincidan con tu búsqueda exacta".

### 1.3 `GET /job_offers/{slug}` (job detail)

| | |
|---|---|
| Auth | `anon` |
| Verified | recon 2026-09-23 (200); unknown slug in Phase 0 2026-09-23 (410) |
| Fixtures | `detail.html`, `detail_410.html`, `takealuk_detail.html` |

- **200.** The page carries:
  - JSON-LD `JobPosting` with `title`, `description` (HTML, whose first `<p>` is the "Cargo:…"
    boilerplate), `datePosted`, `validThrough`, `employmentType`, `workHours`, optional
    `baseSalary`, `jobLocation.address`, `hiringOrganization{name, sameAs}` and
    `identifier.value` (a string).
  - JSON-LD `BreadcrumbList`, where position 3 is the company.
  - DOM:
    - Title: `.job-offer-show-title h1`.
    - Company: `.job-offer-show-title a[href^="/companies/"]`.
    - Full location: `span.break-words`.
    - Modality: `span.tag-navy`.
    - "N vacante(s)".
    - The `h3` "Descripción" and `h3` "Requerimientos" headings, each followed by
      `div.markdown-content[data-text-toggle-target="fullText"]`.
  - The nav/footer links `/companies/home`, `/integrations` and `/pricing` are never the company.
- **Unknown slug → `410 Gone`, not 404.** The error page is minimal:
  - `<title>Error - Luk</title>`, `h1.error-title` "Esta oferta ya no está disponible",
    `.error-code` "410".
  - It has **no** `header#main-header` and **no** JSON-LD.
  - Maps to exit 3 / `NOT_FOUND` (spec §5.2 "404/410").
- **Closed or expired offer: not observed.**
  - The oldest `job_offers` URL in `/sitemap.xml` (placeholder `asesor-demo-empresa-demo-04`,
    lastmod 2026-04-14) is still open (`validThrough` 2026-12-22).
  - Finding a closed offer would have cost more than 3 requests, so the check was skipped
    (§10 cost rule).
  - The 410 wording ("ya no está disponible") suggests that closed offers are also 410. That is
    an inference, not an observation.
  - No in-page closed-state marker is known.

### 1.4 `GET /flexible_search/areas` (location lookup)

| | |
|---|---|
| Auth | `anon` |
| Params | `q=<text>`; optional `context=companies` (the companies page's `data-areas-search-url`) |
| Headers | `Accept: application/json` |
| Verified | jobs context recon 2026-09-23; `context=companies` Phase 0 2026-09-23 |
| Fixtures | `areas.json` (`q=santiago`), `areas_companies.json` (`q=santiago&context=companies`) |

- **Response:** `{"areas":[{id, name, display_path, area_type, area_type_label, offer_count,
  depth, visitor_country_match}]}`, sorted by `offer_count` in descending order.
- **Companies context:** the same keys, the same ids and the same order for `santiago`.
  - Only `offer_count` differs. For 1318 it is 601 in the jobs context and 284 in the companies
    context.
- **Wrong param name:** `query=` is ignored and returns the default list. The site JS uses `q`
  (`fetchJson(…, {queryParam: "q"})`).

### 1.5 `GET /job_titles/similar_roles`

| | |
|---|---|
| Auth | `anon` |
| Params | `role_name=<r>&page=1&ring=1&limit=9` |
| Headers | `Accept: application/json` |
| Verified | recon 2026-09-23 |
| Fixture | `similar.json` |

- **Response:** `{"items":[str…],"next_page":2,"next_ring":1,"resolved_name":"Analista Financiero"}`.
- **Wrong param name:** `query=` returned 400 in the first recon run.

### 1.6 Algolia query suggestions (not takealuk.com)

| | |
|---|---|
| Request | `POST https://<ALGOLIA_APP_ID>-dsn.algolia.net/1/indexes/*/queries` |
| Auth | public search key; no cookies, no Origin, no Referer |
| Headers | `X-Algolia-Application-Id: <ALGOLIA_APP_ID>`, `X-Algolia-API-Key: <ALGOLIA_SEARCH_KEY>` (both discovered at runtime from Luk's homepage, §1.1) |
| Verified | recon 2026-09-23 (`JobOffer_query_suggestions`, "analista fin") |
| Fixture | none public (synthetic per spec §8.1) |

- **Request body:** `{"requests":[{"indexName":…,"query":…,"hitsPerPage":N,"attributesToRetrieve":["query","popularity"]}]}`.
- **Response:** the hits are in `results[0].hits[]` as `{query, popularity}`.
- **Never** query the raw `JobOffer` index. It includes closed offers.

### 1.7 `GET /companies` (company directory)

| | |
|---|---|
| Auth | `anon` |
| Params | `q=<text>`, `page=N`, `locations=<id>` (**Phase 0 ✓**) |
| Verified | recon 2026-09-23; `locations` in Phase 0 2026-09-23 |
| Fixtures | `companies.html`, `companies_q.html` (`q=banco`), `companies_location.html` (`locations=1318`) |

- **No geo default.** `/companies` shows 2152 companies with `input#locations` `value=""` and
  `data-initial-area-options="[]"`.
  - `locations=1318` re-renders `value="1318"` with its area JSON, and the total drops to 226.
- **Frame:** `turbo-frame#companies_marketplace_results`. The total is in its first
  `b.text-primary`.
- **Cards:** `a.company-card[href^="/companies/"]`, 24 per page.
  - 24 hidden `div.company-card.company-card--static.loading-company` skeletons come first. Never
    select bare `.company-card`.
  - Card fields: `.company-card__name`, `.company-card__location span`, `.company-card__tags
    span.tag-navy`, `.company-card__offers` and `.company-card__offer-locations span`.
- **Pagination:** as in search (`?page=N`). With `locations`, `?locations=1318&page=2`.

### 1.8 `GET /companies/{slug}` (company page)

| | |
|---|---|
| Auth | `anon` |
| Params | `page=N` (**Phase 0 ✓**) |
| Verified | recon 2026-09-23; pagination in Phase 0 2026-09-23 (`empresa-demo-03`, 29 offers) |
| Fixtures | `company.html` (2 offers, no nav), `company_paginated.html` (p1, 20 cards), `company_paginated_p2.html` (p2, 9 cards) |

- **Name:** `h1`.
- **Location:** `div.text-caption.text-gray span`.
  - **Not** bare `.text-caption span`. Its first match is the breadcrumb
    `ol.breadcrumb__list.text-caption`, which yields "›" and then the company name.
- **Offer count:** `#section-job-offers h2.subsection-header` ("29 ofertas activas").
- **Jobs:** the search cards inside `turbo-frame#company_job_offers_results`, **20 per page**
  (not 15).
- **Pagination:** a `nav.pagination-nav` inside that frame, with `?page=N` hrefs.
  - It has the same current-page `span[aria-current="page"]` trap as search.
  - Page 2 still shows "29 ofertas activas".
- **Past the last page:** unlike search (200 with 0 cards), `?page=99` answers `302` to the last page
  (`?page=2` for `empresa-demo-03`; seen live 2026-09-24 during code review). luk reports the page Luk served,
  never the requested number, and adds the note `page 99 is past the last page (2)`.

### 1.9 Seen but not used by luk-cli

- `/sitemap.xml`: a flat urlset of 4512 URLs, 4101 of them `/job_offers/{slug}` with `lastmod`. It was
  used only in Phase 0, to look for a closed offer.
- `/robots.txt` (re-checked live 2026-09-24): `User-agent: *`, `Allow: /`, then Disallows for
  `/profile`, `/saved_jobs`, the `/users/*` sign-in/up pages, `/onboarding`, `/admin/`, `/flipper`,
  the employer back-office sub-paths (`/companies/profile`, `/companies/profile/edit`,
  `/companies/job_offers/`, `/companies/sign_in`, `/companies/registration`),
  `/job_offers/*/recommendations`, `/blog`, `/wp-*`, `/wordpress*`, `/backup*`, and the
  `?sort_by=` / `?locale=` variants. Everything luk-cli's **public** commands read (`/`,
  `/job_offers`, `/job_offers/{slug}`, `/companies`, `/companies/{slug}`, `/flexible_search/areas`,
  `/job_titles/similar_roles`) is allowed. The only disallowed paths luk-cli requests are the
  user's own private pages (`/saved_jobs`, `/profile/*`), under the design decision in ADR-0001.
  The verbatim group is in `luk-scraper/tests/conftest.py` (`LUK_LIKE_ROBOTS`).

## 2. Private endpoints

The private endpoints use the `session` auth. Anonymous behaviour was verified in recon on
2026-09-23: a `302` with `Location: https://www.takealuk.com/users/sign_in`.

| Endpoint | Used by | Logged-in structure |
|---|---|---|
| `GET /saved_jobs` | `saved`, the login probe (`max_redirects=0`: 200 = logged in), `session status --check`, MCP `account_status(check=True)` | _(unverified — needs login)_: list container, card markup, pagination, empty state |
| `GET /profile/application_histories` | `applications` | _(unverified — needs login)_: rows, status texts, dates |
| `GET /profile/cvs` | `cvs` (metadata only, never downloads) | _(unverified — needs login)_: CV names, updated-at text |
| `GET /` with session | `whoami` | _(unverified — never captured)_: header markers in §1.1 |
| `/onboarding` redirect target | the "finish your profile" exit 2 | _(unverified — needs login)_ |

- **Session cookie.** `_portal_de_empleos_session` is secure, httponly and samesite=lax, with
  **no Expires**. Expiry is server-side and invisible.
  - `remember_user_token` is not expected for email login, and is unverified for OAuth.
- **Login page** `/users/sign_in`. The user operates it in the browser window. The CLI never
  fills or reads it.
  - `form#new_user` POSTs `user[email]` and `user[password]`, with no remember-me field.
  - Google login: `POST /users/auth/google_oauth2`.
  - LinkedIn login: `POST /users/auth/linkedin`.
  - Google One Tap is also offered.

## 3. Never called

- `POST /job_offers/{slug}/save_later`, and any apply ("Postular") path.
- `POST /users/sign_in`, `/users/auth/*`, `/users/google_one_tap`, `/users/sign_out`, and anything
  that destroys, deletes, confirms or unsubscribes.
- The `Turbo-Frame` header, and the `locale=` and `sort_by=` params.
- The raw Algolia `JobOffer` index.

## 4. Phase-0 live checks — 2026-09-23

**Run conditions**

- Every request was anonymous. The cookie jar refused all cookies, and no `Cookie` header was
  sent on any request.
- UA `luk-cli/0.1.0 (personal read-only client)`; `Accept-Language: es-CL`.
- Redirects were not followed.
- Requests were at least 1.6 s apart (observed ≥ 2 s).
- **23 requests in total** (cap 30). Every one returned 200 except #16 (410). There was no 403,
  429 or challenge.

**Verification rule (spec §2.2).** A param passes only if both of these hold:

- the control is re-rendered as checked, selected or valued;
- the total differs from the same query without the param.

The baseline is #1, `job_positions=analista&locations=1021`, with a total of 274.

| # | Request (`/job_offers?job_positions=analista…` unless shown) | Status | Total | Compared with | Control re-rendered | Result |
|---|---|---|---|---|---|---|
| 1 | `&locations=1021` (baseline) | 200 | 274 | — | `input#locations` = 1021 | baseline (19 pages) |
| 2 | `&locations=1021&job_types[]=part_time&job_types[]=intern` | 200 | 2 | #1: 274 | both checkboxes `checked` | **PASS** `job_types[]` (multi-value) |
| 3 | `&locations=1021&date=last_week` | 200 | 38 | #1: 274 | radio `last_week` `checked` | **PASS** `date` |
| 4 | `&locations=1021&min_salary=1000000` | 200 | 4 | #1: 274 | `min_salary` `value="1000000"`, enabled | **PASS** `min_salary` |
| 5 | `&locations=1021&max_salary=1000000` | 200 | 5 | #1: 274 | `max_salary` `value="1000000"`, enabled | **PASS** `max_salary` |
| 6 | `&locations=1021&min_salary=1000000&salary_currency=COP` | 200 | 0 | #4: 4 | option `COP` `selected`, select enabled | **PASS** `salary_currency` |
| 7 | `&locations=1021&min_salary=1000000&salary_currency=CLP` | 200 | 4 | #4: 4 | option `CLP` `selected` | same total: CLP matches the no-currency behaviour (the CLI's actual request shape) |
| 8 | _(no location param)_ | 200 | 274 | #1: 274 | `input#locations` = 1021 (geo-IP) | geo default confirmed = Chile |
| 9 | `&countries[]=Colombia` | 200 | 0 | #8: 274 | `Colombia` `checked`; `input#locations` still 1021 | **FAIL (semantic)**: ANDed with geo default |
| 10 | `&countries[]=México&countries[]=Perú` | 200 | 0 | #8: 274 | both `checked` (UTF-8 accents OK) | **FAIL (semantic)**: same cause |
| 11 | `&worldwide=1` | 200 | 735 | #1: 274 | `input#locations` `value=""`, no area JSON; pagination hrefs carry `worldwide=1` | **PASS** `worldwide` (735 > 274) |
| 12 | `&worldwide=1&countries[]=Colombia` | 200 | 288 | #11: 735 | `Colombia` `checked`; all 15 cards in Colombia | **PASS** `countries[]` **with `worldwide=1`** |
| 13 | `/job_offers?job_positions=zzqxwvkj&locations=1021` | 200 | 0 | — | — | zero-result markup captured |
| 14 | `&locations=1021&page=19` | 200 | 274 | — | 4 cards; next = disabled `span` | last page captured. **Trap:** highest `Página N` = 18 |
| 15 | `&locations=1021&page=20` | 200 | 274 | — | 0 cards; nav links up to `Página 19`; next disabled | past-last page = empty, not an error |
| 16 | `/job_offers/zzzz-no-existe-12345` | **410** | — | — | error page | unknown slug = 410 Gone |
| 17 | `/sitemap.xml` | 200 | 4101 offers | — | — | used to pick the oldest offer |
| 18 | `/job_offers/asesor-demo-empresa-demo-04` | 200 | — | — | `validThrough` 2026-12-22 | still open → no closed/expired sample (skipped further search) |
| 19 | `/flexible_search/areas?q=santiago&context=companies` (JSON) | 200 | 5 areas | `areas.json` | — | **PASS** same shape (1318 first) |
| 20 | `/companies` | 200 | 2152 | — | `input#locations` `value=""` | companies baseline (90 pages) |
| 21 | `/companies?locations=1318` | 200 | 226 | #20: 2152 | `input#locations` `value="1318"` + area JSON | **PASS** companies `locations` |
| 22 | `/companies/empresa-demo-03` | 200 | 29 offers | — | 20 cards; nav `Página 2` | **PASS** company pagination exists |
| 23 | `/companies/empresa-demo-03?page=2` | 200 | 29 offers | — | 9 cards; current = `span[aria-current]` 2 | **PASS** `?page=N` |

### 4.1 Verdicts: what the CLI and MCP may register

| Feature | Verdict | Register as |
|---|---|---|
| `job_types[]` | pass | `--type T…` / `job_types` (all 6 enum values from the form; `part_time` and `intern` were exercised live) |
| `date` | pass | `--posted-within 24h\|3d\|1w\|1m\|3m\|6m` / `posted_within` (the 6 radio values; `last_week` was exercised) |
| `min_salary` | pass | `--min-salary N` / `min_salary` |
| `max_salary` | pass | `--max-salary N` / `max_salary` |
| `salary_currency` | pass | `--currency C` / `currency` (send CLP when a bound is given) |
| `worldwide=1` | pass | `--worldwide` / `worldwide` |
| `countries[]` without `locations` (spec §5.1 mode 3 as written) | **fail** | Do not send it this way. It returns 0 for every non-Chile country. |
| `countries[]` + `worldwide=1` | pass | `--country C…` / `countries`, sent as `worldwide=1&countries[]=<accented name>…`, never with `locations` |
| `/companies?locations=<id>` | pass | `luk companies --location` / MCP `search_companies.location` (resolve with `context=companies`) |
| `/flexible_search/areas?context=companies` | pass | `luk areas --for companies` / `find_locations(context="companies")` |
| Company pagination | pass | `luk company --page` / `get_company(page)`: 20 per page, `?page=N` |
| Closed/expired offer rendering | not observed | `status: "closed"` has no known marker; `expired` = `validThrough` < today; 410 → exit 3 |

### 4.2 Corrections to spec v3 found in Phase 0

1. **`last_page`.** The current page is `span[aria-current="page"]`, with no aria-label.
   - On the last page, the highest `Página N` is N-1 (page 19 of 19 shows 18; company page 2 of 2
     shows 1).
   - So `last_page` = max(the `Página N` labels, the current-page number) inside the nav. No nav
     means a single page.
2. **Company pages list 20 jobs per page**, not 15. Measure `per_page`; never hardcode it.
3. **Company location selector.** Bare `.text-caption span` hits the breadcrumb ("›"). Use
   `div.text-caption.text-gray span`. This also applies to the recon `company.html`.
4. **An unknown job slug returns 410**, not 404. Both map to exit 3.
5. **`countries[]` needs `worldwide=1`.** Otherwise it is ANDed with the geo-IP default location.
   This changes spec §5.1 mode 3.
6. **Salary parsing.** COP tags carry a decimal comma (e.g. `$3.000.000,00`). The salary filters return
   only offers that show a salary, with overlap semantics.
7. **Labels.** `span.tag-navy` also carries `Prácticas` (→ `intern`) and work-hours labels
   (`20 horas`, `30 horas`). Unknown labels stay in `labels`.
8. **Worldwide pages** have no `data-initial-area-options` attribute at all, so
   `effective_location` is null. Parse the attribute as optional.

## 5. Fixture inventory (`tests/fixtures/public/`)

**Anonymisation.**

- **Structure kept.** Every file keeps the markup Luk served on its capture date: tags, classes,
  nesting, attributes, and the card, skeleton and pagination counts and totals that the traps
  below rely on.
- **Content faked.** The public repository holds no real Luk content. Employer names, offer
  titles, descriptions, slugs, logos, asset URLs and any person are replaced with fake data
  (`Empresa Demo NN SpA`, `empresa-demo-NN`, `Paz Prueba`, `https://example.com/…`), and the
  Algolia credentials in `root.html` with fake values.
- **Tokens.** These values read `SCRUBBED`:
  - every `authenticity_token` input value;
  - `meta[name=csrf-token]` content;
  - `data-google-one-tap-csrf-token-value`.
- **Nonces.** `meta[name=csp-nonce]` and `script[nonce]` are empty on these pages, so there was
  nothing to replace.
- **Line endings.** Recon files keep the CRLF line endings from their Windows capture. Phase-0
  files are the raw server bytes (LF).

| File | Source | Captured | Traps / purpose |
|---|---|---|---|
| `root.html` | `/` | recon 2026-09-23 | anonymous marker; Algolia attrs; One Tap csrf |
| `search_analista.html` | `/job_offers?job_positions=analista&locations=1021` | recon 2026-09-23 | 21 filter controls; salary on 2/15 cards; related-role pills; per-card `save_later` forms |
| `search_p2.html` | `/job_offers?job_positions=analista&page=2` | recon 2026-09-23 | server-default location; no salaries; a card without age; locale `page=` links outside the nav |
| `search_worldwide.html` | `/job_offers?job_positions=analista&worldwide=1` | Phase 0 #11 | total 735; empty `input#locations` without area JSON; CLP and COP salaries; 5 countries |
| `search_zero.html` | `/job_offers?job_positions=zzqxwvkj&locations=1021` | Phase 0 #13 | total 0; no cards, nav or related pills; empty-state text |
| `search_last.html` | `/job_offers?job_positions=analista&locations=1021&page=19` | Phase 0 #14 | 4 cards; disabled next; highest `Página N` = 18 while the current page is 19 |
| `search_past_last.html` | `…&page=20` | Phase 0 #15 | total 274 with 0 cards (page > last_page, so `[]`, exit 0) |
| `search_single_page.html` | `…&locations=1021&job_types[]=part_time&job_types[]=intern` | Phase 0 #2 | 2 cards, **no nav**; `Prácticas` and `30 horas` labels; checked `job_types[]` |
| `detail.html` | `/job_offers/analista-demo-empresa-demo-01` | recon 2026-09-23 | `/companies/home` nav links; boilerplate first `<p>`; vacancies and modality only in the DOM |
| `detail_410.html` | `/job_offers/zzzz-no-existe-12345` | Phase 0 #16 | HTTP **410** error page; no header, no JSON-LD |
| `companies.html` | `/companies` | recon 2026-09-23 | 24 real + 24 skeleton cards; 90 pages |
| `companies_q.html` | `/companies?q=banco` | recon 2026-09-23 | 11 real + 24 skeleton cards; 1/11 cards has tags; 1 card has no location |
| `companies_location.html` | `/companies?locations=1318` | Phase 0 #21 | `input#locations` echo (1318 + area JSON); total 226; 10 pages |
| `company.html` | `/companies/empresa-demo-02` | recon 2026-09-23 | 2 job cards, no nav; footer company links; breadcrumb `.text-caption` trap |
| `company_paginated.html` | `/companies/empresa-demo-03` | Phase 0 #22 | 20 cards; nav `Página 2`; "29 ofertas activas" |
| `company_paginated_p2.html` | `/companies/empresa-demo-03?page=2` | Phase 0 #23 | 9 cards; current page `span` = 2, highest label = 1 |
| `areas.json` | `/flexible_search/areas?q=santiago` | recon 2026-09-23 | 1318 / 1348 / 17863 ambiguity |
| `areas_companies.json` | `/flexible_search/areas?q=santiago&context=companies` | Phase 0 #19 | same shape; company-context `offer_count` |
| `similar.json` | `/job_titles/similar_roles?role_name=analista financiero&page=1&ring=1&limit=9` | recon 2026-09-23 | `resolved_name` |
| `takealuk_list.html` | `/job_offers` (hand-trimmed extra case) | 2026-09-23 | hand-trimmed, so not structurally faithful: `html[lang="es"]`, no layout or header, results frame and cards only, `authenticity_token` with `name` before `value` |
| `takealuk_detail.html` | `/job_offers/{slug}` (hand-trimmed extra case) | 2026-09-23 | hand-trimmed: 2 JSON-LD blocks plus a bare `<h1>` (no `.job-offer-show-title`, no DOM sections), single-quoted attributes |

## 6. Live smoke — 2026-09-24 (spec §9.3)

**Run conditions.** The installed `luk` 0.1.0 (Python 3.10 on Windows 11), anonymous (no session
file existed), each command with `--json`, through the tool's own cross-process limiter.
`ratelimit.json` recorded **15 takealuk.com requests**, each at least 1.50 s after the previous
one, plus 2 Algolia POSTs. One more takealuk request came from the MCP stdio check below. There
was no 403, 429, challenge or retry. Every `--json` document validated against its model
(`models.KIND_MODELS[kind]`), and every required anchor of §5.5 was present, so no parser or
fixture needed a change.

| Command | Exit | Result |
|---|---|---|
| `luk doctor --live` | 0 | Algolia discovery from `/` (app `<ALGOLIA_APP_ID>`, index `JobOffer_query_suggestions`); search: 272 offers for `analista` in Chile; job detail parsed; 5 areas for `santiago`; 9 similar roles; 2152 companies; a company page parsed; 5 suggestions |
| `luk search analista --location Santiago --limit 5 --json` | 0 | exactly 5 results, non-empty slugs and titles, `total` > 0, `effective_location.id` 1318; `Ubicación: Santiago, Región Metropolitana, Chile [Provincia] (1318)` plus 3 alternatives |
| `luk search analista --posted-within 1w --json` | 0 | `query.filters.posted_within` = `1w` (sent as `date=last_week`) |
| `luk show analista-demo-empresa-demo-01 --json` | 0 | JSON-LD + DOM merged; `status` open |
| `luk suggest "analista fin" --json` | 0 | suggestions returned (Algolia config from the 24 h cache) |
| `luk areas santiago --json` | 0 | includes 1318 (answered from the 24 h `areas/` cache filled by `doctor --live`) |
| `luk similar-roles "analista financiero" --json` | 0 | `resolved_name` = "Analista Financiero" |
| `luk companies --query banco --json` | 0 | cards with slug and name |
| `luk company empresa-demo-01 --json` | 0 | name and jobs frame parsed |
| `luk saved --json` (no session) | 2 | `AUTH_REQUIRED` error document; no request sent |

**MCP stdio check (same day).** `luk mcp` started from PATH and was driven with the official
`mcp` client: `initialize`, then `tools/list` returned 10 tools (`search_jobs`, `get_jobs`,
`related_roles`, `find_locations`, `search_companies`, `get_company`, `account_status`,
`my_saved_jobs`, `my_applications`, `my_cvs`), all `readOnlyHint` and `openWorldHint`.
`account_status` answered `session_present: false`. `search_jobs(roles=["analista"],
max_results=3)` returned 3 results out of 272. An unregistered argument (`sort_by`) was refused
with `INVALID_ARGUMENT` before any request.

**Drift against the fixtures (data only, not structure).** The Chile total for `analista` moved
from 273/274 to 272. The areas response now spells the alternative "Santiago de Surco, Lima,
Perú" where `areas.json` has "Peru".

## 7. Still unverified

These need a first `luk login` with a real account (spec §8.1): run `luk debug capture` on `/`,
`/saved_jobs` (empty and with items), `/profile/application_histories` (at least two statuses)
and `/profile/cvs`, review the scrubbed captures, copy them to `tests/fixtures/private_real/`
(git-ignored; `tests/test_private_real.py` then runs on them), derive the selectors and flip
`parsers.VERIFIED`.

- The logged-in header markers (`whoami` name/email, early login probes).
- The list container, cards, pagination and empty state of the three private pages.
- The `/onboarding` redirect target.
- Whether OAuth logins set `remember_user_token`.
- How Luk renders a closed or expired offer (none found in Phase 0; an unknown slug is 410).
