# ADR-0003 — luk-assist: pre-fill the application form, then stop

- **Status:** accepted (2026-09-23)
- **Deciders:** maintainer

## Context

Applying on Luk means opening `/job_offers/{slug}?tab=apply`. The form loads lazily in a
Turbo Frame (`/job_offers/{slug}/external_application_form`). Luk pre-fills name, email, phone
and the CV from the user's profile. Each offer then asks two or three free-text questions
(place of residence, expected net monthly salary, relevant experience) before the
**"Enviar postulación"** button.

Answering the same questions dozens of times is tedious, so it is tempting to automate the
whole thing. But a submitted application is **irreversible and outward-facing**. It reaches a
real employer, under the user's real name, with answers the user may never have read. Bulk
auto-applying can also get an account banned and wastes recruiters' time.

## Decision

- **luk-assist pre-fills and stops.** It opens each application form in a **visible** browser,
  fills the free-text answers, and leaves the form on screen. The user reviews, edits and clicks
  "Enviar postulación" themselves, one offer at a time.
- **No submit path exists, by construction.**
  - The browser contract (`FormBrowser`) has only `open`, `detect_fields` and `fill`.
  - Nothing clicks, presses keys, focuses, submits forms, ticks consent boxes, picks radios or
    selects, uploads files, runs page JavaScript or sends its own HTTP requests.
  - The only navigation is opening each offer in a **new tab**, so a form the user may be
    submitting is never navigated away from. Closing one tab never closes the others.
  - An offline test scans the package's AST and fails on any such call, including dynamic
    imports and extra browser launch flags.
- **Only the real application form.** If the form doesn't appear (not logged in, closed offer,
  an incomplete profile, an external application), nothing is typed anywhere. The offer is
  reported as `FORM_NOT_FOUND`. Each field is re-checked right before filling (same name, same
  label, still empty, not a password or verification-code field); otherwise it is skipped.
- **The user logs in themselves.** Either in luk-assist's own persistent browser profile (in the
  user's data directory, outside any repo), or by reusing the session saved by `luk login`
  (`--storage-state`). luk-assist never types or stores a password.
- **Never fabricate answers.**
  - Answers come from the user's own answers file, optionally seeded from their CV. The CV only
    contributes what it states explicitly: for example, languages come only from an
    "Idiomas:" line, and negations are ignored.
  - A question that no rule matches, or whose answer is empty, is left **blank** for the user.
    The expected-salary answer goes only into questions that ask for an *expected* salary, never
    into questions about current or gross pay, or experience with payroll.
  - A generic fallback sentence is opt-in only.
- **Always headed.** There is no headless mode: the whole point is that the user sees and
  decides.
- **Independent package.** `luk_assist` never imports `luk_cli`, and `luk_cli` never imports it.
  The read-only plugin and MCP tools can't fill forms.
- **Personal data stays local.** Answers, CVs and the browser profile live in the user's
  directories; the repo ships only neutral examples and `.gitignore` rules.

## Consequences

- Applying stays a human decision; luk-assist only removes the typing.
- Reviewers must reject any change that adds a submit/click/keypress path, auto-ticks consent,
  runs headless, or invents answers. Relicensing or "educational" framing does not change this.
- If Luk changes the form, fields are left blank rather than guessed; the user still sees and
  completes the form.
