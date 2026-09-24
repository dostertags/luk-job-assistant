# Test fixtures

Everything here is offline test data. None of it is real personal data or real third-party content.

## `public/`

Minimized copies of public takealuk.com pages. The markup structure is the one captured on 2026-09-23:
tags, nesting, the classes and ids the parsers select on, pagination, JSON-LD and the §8.1 traps (skeleton
cards, the breadcrumb `›`, `/companies/home` links in the header and footer, locale links outside the
pagination nav, whitespace and `&nbsp;` inside titles). Everything else was removed: scripts, styles, SVG
paths, images, dialogs, forms, analytics and most utility classes.

All content is fake:

| Real content | Fake replacement |
| --- | --- |
| employer names / slugs | `Empresa Demo NN SpA` / `empresa-demo-NN` |
| job titles | `<Role> Demo NN` (for example `Analista Demo 01`) |
| offer slugs | `<title-slug>-<company-slug>` (for example `analista-demo-01-empresa-demo-01`) |
| descriptions and requirements | `Párrafo de ejemplo N…`, `Tarea de ejemplo N.`, `Requisito de ejemplo N.` |
| logos, employer RUTs | `https://example.com/logo.png`, `11.111.111-0` |
| offer ids, avatar ids | `10001`, `10002`, … and small sequential numbers |
| Algolia application id / search key | `TESTAPPID0` / `fake-search-key` |

Place names, area ids, generic labels ("Jornada Completa", "Presencial"), related-role names, counts,
salaries and dates stay as captured, because the tests assert on them. `areas.json`, `areas_companies.json`
and `similar.json` hold only place names, generic role names and counts.

To add a public fixture: capture the page with `luk debug capture <path>`, keep only the markup a parser
needs, and replace every employer name, title, text, image, id and key with fake data as above. Never commit
a raw capture.

## `synthetic/`

Hand-written pages, marked `SYNTHETIC:` on line 1 (a test checks the marker). They stand in for the private
pages (saved jobs, applications, CVs, the logged-in header) and for pages never observed live (a challenge
page, a 404). The fake identity is "Paz Prueba" / `paz.prueba@example.com`. `scrub_worst_case.html`
deliberately contains fake personal data (sentinel names, emails, RUTs, phone numbers and tokens) that
the capture scrubber must remove.

## `private_real/`

Real scrubbed captures of your own private pages, used by `test_private_real.py` when present. This
directory is git-ignored and must never be committed.
