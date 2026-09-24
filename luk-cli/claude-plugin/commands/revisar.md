---
description: Resumen de mis ofertas guardadas y postulaciones en Luk / Digest of my saved Luk jobs and applications
---

The user asked for a digest of their own Luk data, so the private tools are in scope for this
request (rules of the `luk` skill apply, especially 6 and 7).

1. Call `my_saved_jobs` with `details: true` (full offers for up to 10 saved jobs: status,
   `valid_through`, salary) and `my_applications`. On `AUTH_REQUIRED`, follow `/luk:login` and
   retry once the user is logged in.
2. Saved jobs, as a table: Cargo | Empresa | Estado (`status`: open, expired or closed) | Vence
   (`valid_through`) | Sueldo | Link. Flag offers that expire within 7 days and those already
   expired or closed. Saved jobs without details show their card data only.
3. Applications, as a table: Cargo | Empresa | Postulado (`applied_at_text`) | Estado
   (`status_text`), exactly as Luk shows them; do not reinterpret the raw text.
4. Close with a short summary: counts, what expires soon, and next steps, e.g. `luk open <slug>`
   so the user applies to a saved offer themselves. Never apply.
5. If `warnings` says a parser is unverified, tell the user the lists may be incomplete; a
   `luk debug capture` helps fix that, but run it only if they ask.

Use the user's language for headers and text. Keep this data in the conversation: never write it
to files, commits, artifacts or other tools unless the user asks.
