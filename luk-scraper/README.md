# luk-scraper

A polite crawler that exports the **public** job offers of [Luk](https://www.takealuk.com)
(takealuk.com) to **JSONL** or **CSV**. Use it for your own job search, alerts or analysis.

> **Unofficial.** Not affiliated with, endorsed or sponsored by Luk or Buk. "Luk" and "Buk" are
> their owners' marks. Respect Luk's terms of use and use the data for personal or
> educational purposes. Don't republish employers' offers as your own, and don't
> run the crawler more often than you need.

luk-scraper is standalone. It never imports its siblings [luk-cli](../luk-cli) or
[luk-assist](../luk-assist), and it never logs in. Governance:
[ADR-0002](../docs/adr/0002-luk-scraper-polite-public-crawler.md).

## Polite-crawling policy

These rules are built into the code. None of them has a switch to turn it off.

- **Public pages only, no login.** It fetches only the listing (`/job_offers`), the offer pages
  (`/job_offers/{slug}`) and `robots.txt`. It sends only `GET` requests and never keeps or sends
  cookies or credentials: a `requests.Session` you hand to the Python API has its cookies and
  its `Cookie` / `Authorization` / `Proxy-Authorization` headers removed before every request.
  It never follows a redirect to a sign-in page, a sub-page or another site.
- **Honest identity.** The User-Agent is `luk-scraper/<version> (+<contact URL>)`, never a
  browser imitation, and the Python API refuses any other User-Agent. The contact defaults to
  this project's page. Set `LUK_SCRAPER_CONTACT` to your own URL, for example your fork, so
  site operators can reach you. E-mail addresses are refused, so personal data never lands in
  other people's server logs.
- **Slow by default.** It waits 2 s between requests, and never less than 1 s, whatever
  `--delay` says. If robots.txt sets a longer `Crawl-delay`, it waits that long instead.
- **robots.txt first.** robots.txt is read before every crawl. If it disallows the listing
  for our User-Agent, or can't be read, nothing is crawled (exit code 2). It is matched the
  RFC 9309 way: longest rule wins, `*` and `$` are wildcards. Every request, including each
  redirect hop, is checked against it before it is sent. There is no option, in the CLI or the
  Python API, to skip robots.txt or to supply a policy of your own. The `?sort_by=` and
  `?locale=` parameters that Luk's robots.txt disallows are never sent.
- **Back off, then stop.** 403, 429 and 5xx answers get exponential backoff (2, 4, 8, 16 s),
  and a `Retry-After` header is honoured. If Luk keeps refusing (403/429), the crawl **stops**
  and makes no further requests (exit code 2). There is no header rotation, proxy, stealth
  browser or any other evasion. If you are blocked, stop.
- **Bounded.** At most 400 listing pages per run. The crawl also stops at the last page, at an
  empty page, or after 3 pages in a row with nothing new.

## Install

Python 3.10 or newer. luk-scraper is **not on PyPI**: install it from this repository only. A
package with a similar name on PyPI would not be this project.

Editable installs need pip 21.3 or newer, so update pip first. Then, from this folder
(`luk-job-assistant/luk-scraper`):

```
python -m pip install -U "pip>=21.3"
python -m pip install -e .
```

For the tests, install `python -m pip install -e ".[dev]"` instead. From elsewhere, give the
path: `python -m pip install -e "<path to luk-job-assistant>/luk-scraper"`.

## Usage

```
luk-scraper crawl --out offers.jsonl                  # all public offers in Chile, enriched
luk-scraper crawl --out offers.jsonl --date 3d        # only the last 3 days (cheap daily run)
luk-scraper crawl --out offers.csv --max-pages 5      # a quick sample for a spreadsheet
luk-scraper crawl --out offers.jsonl --no-enrich      # listing cards only (much faster)
luk-scraper crawl --out offers.jsonl --countries Chile,Perú   # several countries
luk-scraper crawl --out offers.jsonl --countries colombia --no-enrich
luk-scraper --version
```

`python -m luk_scraper ...` works too.

**Countries.** Every listing request carries `worldwide=1` next to the countries. Without it,
Luk only returns offers from the country your IP address is in: checked live on 2026-09-24 from
Chile, `countries[]=Colombia` gave 0 offers and `countries[]=Colombia&worldwide=1` gave 1,430.
With it, the countries you ask for are the countries you get, wherever you run it.

| Option | Meaning |
|---|---|
| `--out PATH` | Output file. `.jsonl` / `.ndjson` gives JSON Lines, `.csv` gives CSV. Required. |
| `--countries A,B` | One or more of `Brasil`, `Chile`, `Colombia`, `México`, `Perú` (default `Chile`). Case and accents are ignored (`peru` means `Perú`); any other name is a usage error. |
| `--date X` | Only recent offers: `1d`, `3d`, `1sem`, `1mes`, `3meses` or `6meses`. |
| `--max-pages N` | Stop after N listing pages (15 offers each). |
| `--no-enrich` | Skip the offer pages. You lose the description, salary, dates and id. |
| `--enrich-limit N` | Fetch the offer page only for the first N offers. |
| `--delay S` | Seconds between requests (default 2; values below 1 are raised to 1). |
| `-q`, `--quiet` | Show only warnings and the final summary. |

**How long it takes.** Each listing page and each offer page is one request, 2 s apart. A full
Chile crawl (about 120 pages and 1,800 offers) takes about 4 minutes without enrichment, and
about an hour with it. For daily use, `--date 1d` or `--date 3d` keeps runs to a few minutes.

**Output.** Progress and a one-line summary go to stderr. The file is written at the end, in one
step, so an interrupted run never leaves a half-written file. Ctrl+C stops the crawl and keeps
the offers collected so far. If nothing was collected because of an error, an existing output
file is left untouched.

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Error or partial result (for example, some offer pages failed). The offers collected so far are written. |
| 2 | Stop: robots.txt disallows the crawl, or Luk kept answering 403/429. Don't retry and don't work around it. Also used by argparse for usage errors. |

## Output schema

One record per offer, deduplicated by `slug` (Luk repeats some cards across pages). JSONL keys
and CSV columns come in this order.

| Field | Type | Source | Description |
|---|---|---|---|
| `slug` | str | card | Luk's key for the offer, taken from its URL. Unique in a file. |
| `title` | str | card | Job title. |
| `company` | str / null | card | Employer name as shown on the card. |
| `location_text` | str / null | card | Location as shown, e.g. `Las Condes, Santiago, Región Metropolitana, Chile`. |
| `region` | str / null | card | For Chile, one of the 16 regions in canonical form (`Metropolitana`, `Valparaíso`, `Bío Bío`, ...). Otherwise the site's text. |
| `comuna` | str / null | card | First part of the location. |
| `country` | str / null | card | Last part of the location, e.g. `Chile`. |
| `workday` | str / null | card | e.g. `Jornada Completa`, `Jornada Parcial`. |
| `modality` | str / null | card | `Presencial`, `Remoto` or `Híbrido`. |
| `posted_relative` | str / null | card | e.g. `Hace 2 horas`. The card shows no absolute date. |
| `url` | str | built | `https://www.takealuk.com/job_offers/{slug}`. |
| `description` | str / null | offer page | Full description as plain text, one line per paragraph or bullet. |
| `salary_min` | int / null | offer page | Only when the offer publishes a salary greater than 0. |
| `salary_max` | int / null | offer page | Upper bound of a published range. |
| `published_at` | str / null | offer page | `YYYY-MM-DD` (JSON-LD `datePosted`). |
| `valid_through` | str / null | offer page | `YYYY-MM-DD` (JSON-LD `validThrough`). |
| `external_id` | str / null | offer page | Luk's numeric offer id (JSON-LD `identifier.value`). |
| `fetched_at` | str | crawler | When the listing page was read, UTC ISO 8601 (`2026-09-24T12:00:00+00:00`). |

Fields from the offer page stay empty with `--no-enrich`, or when that page failed.

**CSV notes.** The file is UTF-8 with a BOM, so Excel shows accents correctly. The offers are
third-party text, so a cell starting with `=`, `+`, `-` or `@` gets a leading `'`. That stops
spreadsheets from running it as a formula (CSV injection). Use JSONL when you need the exact
text.

## Feeding the results to luk-assist

[luk-assist](../luk-assist) reads this JSONL or CSV directly (it uses the `slug` field):

```
luk-scraper crawl --out offers.jsonl --date 3d
# filter offers.jsonl down to the ones you actually want to apply to, then:
luk-assist --listings offers.jsonl --cv MyCV.pdf --limit 5
```

luk-assist opens each application form in a visible browser, pre-fills your answers and
**stops**. You review and click "Enviar postulación" yourself. See its README.

## Python API

```python
from luk_scraper import crawler, output

offers, stats = crawler.crawl(countries=["Chile", "peru"], date_filter="last_3_days",
                              max_pages=3, enrich=False)
output.write(offers, "offers.jsonl")
print(stats["status"], stats["offers"])
```

`countries` takes the same names as `--countries` (`"peru"` becomes `"Perú"`); an unknown one
raises `ValueError` before any request. `crawl()` always reads robots.txt from the site first and
has no parameter to skip it or replace it. It raises `luk_scraper.robots.RobotsDisallowed` when
robots.txt forbids the crawl. `fetcher.Fetcher(user_agent=...)` accepts only the honest
`luk-scraper/<version> (+<URL>)` format and raises `ValueError` for anything else.

The `stats` dict returned by `crawl()` has `pages`, `offers`, `duplicates`, `enriched`, `errors`, `status`
(`OK`/`PARTIAL`/`ERROR`), `stop_reason`, `blocked`, `total_reported`, `last_page`, `requests`
and `duration_s`.

## How it works

The reverse-engineered markup is documented in the module docstrings, with the date it was
last verified live: `config.py` (URLs, parameters, robots.txt), `parser.py` (card and JSON-LD
layout), `crawler.py` (pagination and stop conditions), `fetcher.py` (retry policy) and
`robots.py` (matching rules). Luk has no public JSON API, so the crawler parses the
server-rendered HTML and the `schema.org/JobPosting` JSON-LD block on each offer page.

If Luk changes its markup, parsing failures show up as errors in the summary and exit code,
not as silently wrong data. To update a fixture, save the page locally and turn it into a
**minimized, fake** fixture: keep the structure and invent the content (`Empresa Demo 01 SpA`,
`https://example.com/...`). Never commit a real capture.

## Development

```
cd luk-scraper
python -m pytest -q
```

The tests are offline: fixtures, a fake fetcher and a fake HTTP session. There is no network,
and nothing really sleeps.

## License

MIT. See [LICENSE](LICENSE).
