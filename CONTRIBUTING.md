# Contributing

Thanks for helping! This repo holds three independent Python packages:

| Package | What it is | Governance |
|---|---|---|
| [`luk-cli`](luk-cli) | Read-only personal client + Claude Code plugin | [ADR-0001](docs/adr/0001-luk-cli-read-only-personal-client.md) |
| [`luk-scraper`](luk-scraper) | Polite crawler for public offers → JSONL/CSV | [ADR-0002](docs/adr/0002-luk-scraper-polite-public-crawler.md) |
| [`luk-assist`](luk-assist) | Pre-fills application forms, then stops | [ADR-0003](docs/adr/0003-luk-assist-prefill-and-stop.md) |

## Red lines (non-negotiable)

Pull requests that cross any of these will be closed, however they are framed:

1. **Never submit an application.** `luk-assist` pre-fills and stops. No code may click, press
   keys, submit forms, tick consent boxes, pick radios or selects, upload files, or run without
   a visible window. The user clicks "Enviar postulación".
2. **Never handle passwords.** Users log in themselves in a real browser window. No tool asks
   for, stores, logs or types a password.
3. **Read-only toward Luk.** `luk-cli` and `luk-scraper` send only `GET` requests.
4. **No evasion.** Honest User-Agents, conservative rate limits, back off then stop on
   403/429/challenge pages. No stealth flags, fake browser UAs, proxies or header rotation.
   `luk-scraper` honours robots.txt.
5. **No real personal data or third-party content in the repo.** Fixtures are minimized pages
   with **fake** content. Never commit real captures, sessions, answers files, CVs or real
   people's or employers' data.
6. **Never fabricate answers.** `luk-assist` fills only what the user provided; the rest stays
   blank.
7. **Packages stay independent.** `luk_cli` never imports `luk_assist` or `luk_scraper`, and
   they never import it (enforced by a test).

## Development

```bash
python -m pip install -U "pip>=21.3"   # PEP 660 editable installs
python -m pip install -e "./luk-cli[mcp,dev]" -e "./luk-scraper[dev]" -e "./luk-assist[dev]"
cd luk-cli && python -m pytest -q
cd ../luk-scraper && python -m pytest -q
cd ../luk-assist && python -m pytest -q
```

Tests are offline (fixtures and fakes; no network, no browser).

Scrapers and parsers document the reverse-engineered mechanism (endpoints, URL scheme, selectors,
quirks) in their module docstrings, with the date it was verified live. Update that date when you
re-verify. If Luk's markup changed, capture
the page locally (`luk debug capture <path>` for luk-cli). Then turn it into a **minimized fake
fixture**: keep the structure, invent the content. Never commit the capture.

## Pull request checklist

- [ ] Tests green in every package you touched; new behaviour has tests.
- [ ] `gitleaks dir -v .` is clean.
- [ ] No real names, emails, phones, RUTs, employers, slugs, keys or tokens in code, docs or
      fixtures (use `Paz Prueba`, `paz.prueba@example.com`, `Empresa Demo 01 SpA`,
      `11.111.111-0` (invalid check digit on purpose, so it is nobody's RUT), phone `900000000`,
      `https://example.com/...`).
- [ ] No red line crossed (see above).
