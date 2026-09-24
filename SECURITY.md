# Security policy

## Reporting a vulnerability

Please report security issues **privately**, through GitHub's "Report a vulnerability" button
(Security → Advisories) on this repository. Include steps to reproduce and the affected package
(`luk-cli`, `luk-scraper` or `luk-assist`). Please don't put details in a public issue.

If that button is not available, open a public issue titled **"Security contact request"** with
**no details**. The maintainer will reply and arrange a private channel.

Especially in scope:

- anything that could leak a user's Luk session (`session.json`), cookies, answers, CV or
  browser profile, into logs, output files, error messages or third parties;
- any path by which `luk-assist` could submit an application, click, press keys, tick a
  consent box or run without a visible window;
- any path by which `luk-cli` or `luk-scraper` could write to Luk (non-`GET` requests), or evade
  rate limits, blocks or robots.txt (for the scraper).

## Keeping your own data safe

- **`session.json` is a bearer credential.** Whoever has it is logged in to Luk as you. It lives
  in your user config directory. Never commit, sync or share it.
- **Your answers file, CV and luk-assist browser profile** live in your user directories.
  Never copy them into a repository. The root `.gitignore` blocks the usual file names, but
  review `git status` before committing.
- **Captures** from `luk debug capture` are scrubbed, but review them before sharing. Real
  captures belong in `tests/fixtures/private_real/`, which is git-ignored.
- If you think a secret was committed, rotate it: change your Luk password to end the
  sessions, then purge the file from history.

## Scanning

CI runs [gitleaks](https://github.com/gitleaks/gitleaks) on every push and pull request. Run it
locally before publishing a fork: `gitleaks git -v .` (history) and `gitleaks dir -v .`
(working tree).
