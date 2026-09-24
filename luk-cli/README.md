# luk-cli

A read-only personal command line client for [Luk](https://www.takealuk.com) (takealuk.com, also
luk.cl), plus a Claude Code plugin (skill + MCP server) built on it.

> **Unofficial.** Not affiliated with, endorsed or sponsored by Luk or Buk. "Luk" and "Buk" are
> their owners' marks. Use it with your own account, within Luk's Terms.

- **Public commands** (search, offers, companies, locations) are anonymous: no cookie is sent.
- **Private commands** (your saved jobs, applications and CVs) use a session you create **once,
  yourself**, by logging in to Luk in a real browser window. luk-cli never sees or stores your
  password.
- **Read-only.** Only `GET` requests go to takealuk.com. luk-cli never saves offers and never
  applies. `luk open <slug>` opens an offer in your browser and you click "Postular" yourself.
- **Polite.** At least 1.5 s between requests across every luk process, at most 240 requests per
  rolling hour, an honest `luk-cli/<version>` User-Agent, and it stops (exit 4) when Luk blocks.

Spec: [docs/spec.md](docs/spec.md). Endpoints and live verification: [docs/endpoints.md](docs/endpoints.md).
JSON schemas: [docs/schema/](docs/schema/). Governance:
[ADR-0001](../docs/adr/0001-luk-cli-read-only-personal-client.md). luk-cli is standalone: it never
imports its sibling packages [luk-scraper](../luk-scraper) (public crawler) or
[luk-assist](../luk-assist) (the form pre-filler), so the plugin stays read-only by construction.

## Install

Python 3.10 or newer.

**With uv (recommended).** From the repository root:

```
uv pip install -e "luk-cli[mcp,dev]"
```

Add `--python <path-to-python>` to pick a specific interpreter. This puts the `luk` command in
that Python's `Scripts`/`bin` directory. The `[mcp]` extra is needed only for the Claude Code
plugin; `[dev]` adds pytest.

**With pip.** Editable installs need pip 21.3 or newer (PEP 660), so upgrade pip first:

```
python -m pip install -U "pip>=21.3"
python -m pip install -e "luk-cli[mcp,dev]"
```

**Browser for `luk login`.** Playwright's bundled Chromium is used when present. Only if
`luk doctor` reports it missing, install it with the command it prints
(`"<python>" -m playwright install chromium`), or log in with Microsoft Edge or Chrome instead
(`luk login --browser msedge`).

**Check the installation:**

```
luk --version
luk doctor            # offline: interpreter, dependencies, [mcp], `luk` on PATH, Chromium/Edge, paths, session
luk doctor --live     # also fetches and parses one page of each public type (about 7 requests)
```

## Logging in

```
luk login
```

A browser window opens on Luk's sign-in page. Log in with your own account (email and password,
Google or LinkedIn). The window closes by itself once luk-cli confirms the session with a
read-only request to `/saved_jobs`. Options:

- `--browser auto|chromium|chrome|msedge`: `auto` (default) tries the bundled Chromium, then
  Edge, then Chrome.
- `--timeout S`: how long to wait (default 300 s, or `LUK_LOGIN_TIMEOUT`).
- `--force`: log in again even if the saved session still works (for example, to switch
  accounts).

If you are already logged in, `luk login` says so and exits. If Luk sends you to finish your
profile (onboarding), finish it in the browser; the session is saved anyway.

**If Google refuses the window** ("This browser or app may not be secure"), luk-cli prints these
fallbacks, and the window stays open:

1. Use another browser: `luk login --browser msedge` or `luk login --browser chrome`.
2. Log in with LinkedIn, or with email and password. A Google-only account can get a password
   through "¿Olvidaste tu contraseña?" on Luk's sign-in page.
3. `luk login --paste-cookie` (no browser window). In your normal browser, logged in to Luk, copy
   the value of the `_portal_de_empleos_session` cookie for `www.takealuk.com` (DevTools →
   Application → Cookies), or a whole `Cookie:` header or "Copy as cURL" command. Run the command
   **in your own terminal** and paste at the hidden prompt. It refuses to run without a terminal,
   never echoes the value, keeps only that one cookie, and saves it only after Luk accepts it.
   There is no option or environment variable that takes the value.

Other session commands:

```
luk whoami                    # the account of the saved session (name/email from Luk's header)
luk session status [--check]  # file path, age, cookie names/domains/expiry (never values); --check asks Luk
luk session refresh           # reopen the login window starting from the saved session
luk logout                    # delete the saved session on this computer
```

`luk logout` is local only: it deletes the saved files but does not end the session on Luk's
server. To end every session, change your Luk password or sign out on the website.

## Commands

Every data command takes `--json` (one JSON document on stdout, see [docs/schema/](docs/schema/)).
The list commands `search`, `companies`, `saved` and `applications` also take `--csv` (UTF-8 with
BOM). Without either, a table is printed. Notes and warnings go to stderr. Global options:
`--verbose` (one stderr line per HTTP request; never headers, cookies or bodies) and `--version`.

### Public (anonymous)

```
luk search analista                                  # default location: Chile (locations=1021)
luk search "analista financiero" contador --limit 30 # each quoted ROLE is one pill; pills are OR'd
luk search analista --location Santiago              # resolved via Luk's area search; prints "Ubicación: …"
luk search analista --location-id 1348               # an exact area id (see `luk areas`)
luk search analista --country CO --country PE        # other countries instead of Chile
luk search analista --worldwide                      # every country
luk search analista --posted-within 1w --type full_time --type intern
luk search analista --min-salary 1000000 --currency CLP
luk search analista --page 2 --offset 5 --json       # continue where --limit stopped: next_page, next_offset

luk show analista-demo-empresa-demo-01               # a slug, or https://www.takealuk.com/job_offers/<slug>
luk suggest "analista fin" --limit 5                 # Luk's search suggestions
luk areas santiago                                   # area ids: 1318 Provincia, 1348 Comuna, 17863 Ciudad…
luk areas "ñuñoa" --for companies                    # accents optional: "nunoa" works too
luk similar-roles "analista financiero" --limit 9
luk companies --query banco
luk companies --location Santiago --limit 48
luk company empresa-demo-01 --page 2                 # a company and one page (20) of its offers
```

Search notes:

- Roles are job titles matched against offer titles, not skills. With few results, try
  `luk similar-roles`.
- Only one location mode at a time: `--location`/`--location-id`, `--country`, or `--worldwide`.
- `--limit` (1-150, default 15) fetches pages from `--page` on, at most 10 pages per call.
  Results are sorted by relevance, not by date.
- To continue any list (`search`, `companies`, `saved`, `applications`), run it again with
  `--page <next_page> --offset <next_offset>` from the `--json` result (the table caption shows them):
  whatever the `--limit`, nothing is skipped. Luk sometimes lists one offer on two pages (and can
  re-rank between calls), so an offer may come back twice — dedupe by slug. `--offset` counts within
  the start page only (0–49).
- The salary filters only return offers that show a salary. A currency needs a bound.
- Zero results, or a page past the last one, exit 0 with an empty list. `luk company --page` past
  the last page shows the last page (Luk redirects there) with a note naming it.

### Private (need `luk login`)

```
luk saved                      # your saved jobs
luk saved --details --json     # plus the full offer for up to 20 of them (one request each)
luk applications --csv         # your applications and the status Luk shows
luk cvs                        # names and dates of your CVs (never downloads the files)
```

### Tools

```
luk open analista-demo-empresa-demo-01             # 1-5 offers in your browser (you apply there)
luk open empresa-demo-01 --company                 # no browser can start (e.g. over SSH)? exit 1 + the URL
luk debug capture /saved_jobs  # a scrubbed copy of one allowlisted page, for fixing a parser
luk schema job_search          # the JSON Schema of a --json document (--out DIR writes one file per kind)
luk doctor --print-mcp-json    # an absolute MCP server config for this interpreter
luk mcp                        # the MCP stdio server (used by the plugin)
```

### Exit codes

| Exit | JSON `error.code` | Meaning |
|---|---|---|
| 0 | — | OK, including empty results |
| 1 | `INVALID_ARGUMENT`, `INTERNAL` | Usage error, invalid input, or another error |
| 2 | `AUTH_REQUIRED` | Not logged in, session expired, or Luk redirected to onboarding |
| 3 | `NOT_FOUND` | Unknown offer, company or area |
| 4 | `NETWORK`, `RATE_LIMITED`, `BLOCKED`, `BUDGET_EXCEEDED` | Network error, rate limit, block, or hourly budget used up |
| 5 | `SITE_CHANGED` | Luk's page structure changed (`luk debug capture <path>` helps fix it) |
| 130 | `INTERRUPTED` | Interrupted |

Under `--json`, an error prints `{"schema_version": 2, "kind": "error", "error": {"code",
"exit_code", "message", "hint"}}` on stdout and one line on stderr.

## Files

| Path (Windows) | Contents |
|---|---|
| `%LOCALAPPDATA%\luk-cli\session.json` | The saved session: the filtered takealuk.com cookies only |
| `%LOCALAPPDATA%\luk-cli\meta.json` | Name, email, login and validation times, browser |
| `%LOCALAPPDATA%\luk-cli\login.lock`, `session.lock` | Locks (never delete them while luk runs) |
| `%LOCALAPPDATA%\luk-cli\Cache\` | `algolia.json` and `areas\` (24 h), `ratelimit.json`, `captures\` |

On macOS and Linux the same files live in the platformdirs config and cache directories for
`luk-cli` (for example `~/.config/luk-cli` and `~/.cache/luk-cli` on Linux). `luk doctor` prints
the exact paths.

### Environment variables

| Variable | Default | Notes |
|---|---|---|
| `LUK_BASE_URL` | `https://www.takealuk.com` | https origin only |
| `LUK_SESSION_PATH` | `<config>/session.json` | Refused inside a git work tree or a OneDrive, Dropbox or Google Drive folder |
| `LUK_LOGIN_TIMEOUT` | `300` | Seconds |
| `LUK_USER_AGENT` | `luk-cli/<version> (personal read-only client)` | Must start with `luk-cli/` and never imitate a browser |
| `LUK_ALGOLIA_APP_ID`, `LUK_ALGOLIA_API_KEY`, `LUK_ALGOLIA_INDEX` | discovered from `/` | All three set: discovery is skipped |
| `LUK_MAX_REQ_PER_HOUR` | `240` | Can lower the hourly budget, never raise it |
| `LUK_MCP_PRIVATE` | `1` | `0`: the private MCP tools are not registered and `account_status` omits name/email |

## Security notes

- **`session.json` is a bearer credential.** Whoever copies it is logged in to Luk as you until
  the session ends on the server. Never commit, sync or share it. luk-cli refuses a
  `LUK_SESSION_PATH` inside a git work tree or a cloud-synced folder.
- **Windows permissions.** The files rely on the per-user ACL of `%LOCALAPPDATA%`. On macOS and
  Linux the directory is `0700` and the files `0600`.
- **Session lifetime.** The Luk cookie has no expiry date; the server decides when it ends.
  `luk session status --check` tells you whether it still works.
- **Changing your Luk password** invalidates the existing sessions (Devise), including the saved
  one; run `luk login` again afterwards.
- **`luk logout` is local only.** It does not end the session on Luk's server (see above).
- **TLS-inspecting proxies** (corporate or antivirus HTTPS scanning) can read the cookie in
  transit. Avoid private commands behind one.
- **Secrets hygiene.** Cookie values are never printed, logged or put in error messages;
  `--verbose` shows only method, URL, status and timing. Public commands and the Algolia
  suggestions client never send the cookie.
- **Captures.** `luk debug capture` scrubs your name, email, RUT, phone numbers, tokens, signed
  URLs and CV file names before writing, and refuses to write if your identity survives. Review a
  capture before sharing it. Real private pages never go into git
  (`tests/fixtures/private_real/` is ignored).

## Claude Code plugin

The plugin in `claude-plugin/` gives Claude the `luk` MCP server (tools
`mcp__plugin_luk_luk__<tool>`), a skill with the rules above, and three slash commands:
`/luk:buscar <cargos> [en <lugar>] [--recientes]`, `/luk:revisar` (a digest of your saved jobs and
applications) and `/luk:login`.

**Install** (after installing luk-cli with the `[mcp]` extra):

```
/plugin marketplace add <absolute path to this repo>/luk-cli
/plugin install luk@luk-local
```

For development, load it without installing: `claude --plugin-dir luk-cli/claude-plugin`.
Enable it at project or local scope for job-search work. `/mcp` should then show `plugin:luk:luk`
as connected.

**Tools.** Public: `search_jobs`, `get_jobs`, `related_roles`, `find_locations`,
`search_companies`, `get_company`, `account_status`. Private (they send your session):
`my_saved_jobs`, `my_applications`, `my_cvs`. Every tool is read-only.

**Permissions.** Keep the three private tools on "ask": do **not** add
`mcp__plugin_luk_luk__my_saved_jobs`, `…__my_applications` or `…__my_cvs` to
`permissions.allow`. Allowing the public tools is fine. To remove the private tools entirely, set
`LUK_MCP_PRIVATE=0`.

**Login from Claude.** When a tool answers `AUTH_REQUIRED`, Claude runs `luk login` in the
background and tells you a window is opening; you type your own credentials. Claude never asks
you for a password, cookie or token and never runs `--paste-cookie`.

**If the wrong `luk` starts.** `.mcp.json` runs `luk mcp` from PATH. With several Pythons on PATH
a different `luk` may win (`luk doctor` flags this). Then use the absolute config printed by
`luk doctor --print-mcp-json`, which runs `<this python> -m luk_cli mcp`, in your MCP settings.

## Development

```
python -m pytest luk-cli/tests -q
```

The tests are offline (network blocked in `conftest.py`). `tests/fixtures/public/` holds minimized
pages that keep the structure of Luk's public pages; every employer, title, slug, text, id and key
in them is **fake**, while place names, counts, salaries and dates are kept as captured (see
[tests/fixtures/README.md](tests/fixtures/README.md)). `tests/fixtures/synthetic/` holds synthetic
private pages. Real private
captures placed in `tests/fixtures/private_real/` (git-ignored, never committed) are tested
automatically on your machine. After a model change, bump `SCHEMA_VERSION` and regenerate the
schemas with `python -m luk_cli schema --out luk-cli/docs/schema`.

**Not yet verified live:** the logged-in pages (`/saved_jobs`, `/profile/application_histories`,
`/profile/cvs`, the logged-in header used by `whoami`, the onboarding redirect). Their parsers
are provisional and warn `parser unverified` until checked against real captures (spec §8.1).

## License

MIT. See [LICENSE](LICENSE).
