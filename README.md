# luk-job-assistant

A free, open-source toolkit for job seekers using **[Luk](https://www.takealuk.com)**
(takealuk.com), the Latin American job portal. Search offers from your terminal or from Claude,
export public offers to a spreadsheet, and pre-fill application forms so you only review and
click send.

> **Unofficial.** This project is not affiliated with, endorsed or sponsored by Luk or Buk.
> "Luk" and "Buk" are their owners' marks. Use it with your own account, within Luk's Terms,
> and at a polite pace. MIT-licensed, for personal and educational use.

## What's inside

| Package | What it does | Talks to Luk as |
|---|---|---|
| [**luk-cli**](luk-cli) | `luk` command + **Claude Code plugin** (skill + MCP server): search offers, read full offers, browse companies, and read *your own* saved jobs, applications and CVs | a read-only personal client (you log in yourself, once) |
| [**luk-scraper**](luk-scraper) | Crawls Luk's **public** offers (Chile by default) into **JSONL/CSV**, optionally with the full description, salary and dates | a polite crawler: honest UA, robots.txt, ≥2 s between requests |
| [**luk-assist**](luk-assist) | Opens each application form in a **visible** browser, **pre-fills** your answers (residence, expected salary, experience) and **stops**. You review and click "Enviar postulación" | your own logged-in browser window |

The three are independent Python packages (Python ≥ 3.10). Install only what you need.

## The rules this project lives by

- **It never submits an application.** `luk-assist` pre-fills and stops; there's no submit,
  click or keypress code anywhere, and a test enforces it. You decide, one offer at a time.
- **It never touches your password.** You log in yourself in a real browser window. Tools keep
  only the resulting Luk session, in your user folder, never in the repo.
- **It is read-only toward Luk** (`GET` requests only) and **polite**. It uses honest
  User-Agents and conservative rate limits, and it backs off, then stops, when Luk says no.
  No evasion.
- **It never invents answers.** Anything you didn't provide stays blank for you.
- **Your data stays on your machine.** Sessions, answers, CVs and browser profiles live in your
  user directories and are git-ignored.

Design decisions: [ADR-0001 luk-cli](docs/adr/0001-luk-cli-read-only-personal-client.md) ·
[ADR-0002 luk-scraper](docs/adr/0002-luk-scraper-polite-public-crawler.md) ·
[ADR-0003 luk-assist](docs/adr/0003-luk-assist-prefill-and-stop.md).

## Quickstart

```bash
git clone https://github.com/dostertags/luk-job-assistant
cd luk-job-assistant
python -m pip install -U "pip>=21.3"   # editable installs of pyproject-only packages need pip >= 21.3
```

The packages are installed **from this repository** (they are not published on PyPI).

**Search from the terminal or from Claude (luk-cli)**

```bash
python -m pip install -e "./luk-cli[mcp]"
luk search analista --location Santiago
luk show <slug>                 # the full offer
luk login                       # once, only for your saved jobs / applications / CVs
luk saved
```

For the Claude Code plugin, see [luk-cli/README.md](luk-cli/README.md#claude-code-plugin).

**Export public offers (luk-scraper)**

```bash
python -m pip install -e ./luk-scraper
luk-scraper crawl --out offers.jsonl --date 3d     # offers from the last 3 days, Chile
luk-scraper crawl --out offers.csv --max-pages 5   # a quick sample for a spreadsheet
```

**Pre-fill applications (luk-assist)**

```bash
python -m pip install -e "./luk-assist[browser,pdf]"
python -m playwright install chromium
luk-assist --init-answers                          # creates your answers file (in your user folder)
luk-assist --listings offers.jsonl --cv MyCV.pdf --limit 5
```

A browser window opens. Log in to Luk the first time. For each offer, luk-assist fills what it
can, then waits: you check, fix, click **"Enviar postulación"** yourself, and press Enter for the
next one.

## Development

```bash
python -m pip install -U "pip>=21.3"
python -m pip install -e "./luk-cli[mcp,dev]" -e "./luk-scraper[dev]" -e "./luk-assist[dev]"
(cd luk-cli && python -m pytest -q)
(cd luk-scraper && python -m pytest -q)
(cd luk-assist && python -m pytest -q)
```

All tests run offline, against minimized pages that keep the structure of Luk's pages. Every
employer, title, slug, text, id and key in them is **fake**; place names, counts, salaries and
dates are kept as captured, because the parsers are tested on them. No real person's data is
anywhere in the repo. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © 2026 Diego Ostertag.
