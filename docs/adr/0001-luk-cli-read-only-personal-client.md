# ADR-0001 — luk-cli: a read-only personal Luk client + Claude Code plugin

- **Status:** accepted (2026-09-23)
- **Deciders:** maintainer

## Context

`luk-cli` is a command-line client for Luk (takealuk.com) that searches public job offers and
reads the user's **own** saved jobs, applications and CVs. It ships as a **Claude Code plugin**
(skill + MCP server) so an assistant can use it through typed tools. Spec:
[`luk-cli/docs/spec.md`](../../luk-cli/docs/spec.md).

Governance questions had to be settled first:

1. **The private pages are the point, and they need a login.** `/saved_jobs` and `/profile/*`
   are only reachable when logged in (anonymous requests get a `302` to `/users/sign_in`).
   Login is by email + password, Google or LinkedIn. The only credential is the
   browser-session cookie `_portal_de_empleos_session`.
2. **robots.txt disallows those private pages** (and the sign-in pages) for crawlers.
3. **Automation must never act for the user.** Nothing may apply, save or submit on the
   user's behalf, and nothing may evade the site's defences.
4. **Personal data stays personal.** Application history, CV names and email must never end
   up in the repository, in logs or in another tool's output.

## Decision

- **luk-cli is a personal user agent, not a crawler.** It acts for one logged-in user, on that
  user's own pages, interactively. The bulk public crawler is a separate package
  ([ADR-0002](0002-luk-scraper-polite-public-crawler.md)) with its own, stricter rules.
- **Manual login, typed by the user.**
  - A headed browser window opens and the user types their own credentials (or uses
    Google/LinkedIn).
  - The CLI never asks for, stores, logs or types a password, and never fills or reads the
    login form.
  - It saves only the resulting takealuk.com cookies; success is detected by a read-only probe.
  - A TTY-only `--paste-cookie` fallback exists for when Google refuses the automated window.
  - An assistant may *launch* `luk login`, but never operates the window or asks for secrets.
- **robots.txt scope.** robots.txt governs crawlers; luk-cli is an interactive client acting
  on the user's own account, so the robots-disallowed private pages (`/saved_jobs`,
  `/profile/*`) are in scope. Every path a **public** command reads (`/job_offers`, offer and
  company pages, area and role lookups) is allowed by robots.txt anyway (the Disallow list is
  quoted in `luk-cli/docs/endpoints.md`). Politeness is enforced by:
  - ≥1.5 s between requests across all luk processes and threads;
  - a budget of ≤240 requests per rolling hour;
  - backoff that honours `Retry-After`.

  This scope decision does **not** extend to `luk-scraper`, which honours robots.txt.
- **Read-only.** Only `GET` requests go to takealuk.com, enforced in code. There is no
  save-job, no apply and no submit code path. The apply hand-off is `luk open <slug>`: the user
  clicks "Postular" themselves.
- **No evasion.** An honest UA, `luk-cli/<version> (personal read-only client)`. No stealth or
  automation-hiding flag, no fake browser UA, no reading of the user's real browser profile or
  cookie stores. A 403 or challenge page means stop (exit 4), never a retry with other headers.
- **Data minimisation.** Public commands are anonymous. The session cookie is sent only for
  private commands, and only to `https://www.takealuk.com`.
- **Isolation.** `luk_cli` never imports `luk_scraper` or `luk_assist`, and neither imports
  `luk_cli`; an offline AST test enforces it. So the plugin can never gain form-filling or
  crawling abilities by accident. Real captured private pages stay local (git-ignored).
- **Plugin scope.** MCP tools are read-only: no login, logout, open, capture or file-writing
  tools. Private tools can be switched off with `LUK_MCP_PRIVATE=0`. Job and company text is
  treated as untrusted third-party data.

## Consequences

- Reviewers must not flag the login flow or the robots.txt scope as violations; equally, this
  ADR must not be cited to add login or ignore robots.txt in `luk-scraper`.
- **`session.json` is a bearer credential.** It lives in the per-user config directory
  (`%LOCALAPPDATA%\luk-cli` on Windows) and is refused inside a git work tree or a
  cloud-synced folder. `luk logout` is local-only; changing the Luk password invalidates the
  server-side session.
- **Account risk is bounded but not zero.** Private requests are attributable to the user's Luk
  account, hence the hourly budget and the rule that public commands stay anonymous.
- **Logged-in page structure is only partly verified.** Private-page parsers ship against
  synthetic fixtures and are checked against scrubbed local captures (`luk debug capture`).
- **Escape hatch.** If Luk blocks the honest UA, objects to this use, or ships an official
  API/feed: stop and revisit this ADR.
