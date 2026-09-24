---
description: Buscar ofertas de trabajo en Luk / Search Luk job offers (read-only; Chile by default)
argument-hint: <cargos> [en <lugar>] [--recientes]
---

Search Luk for job offers, following the rules of the `luk` skill.

Arguments: $ARGUMENTS

1. Read the arguments:
   - `<cargos>`: job titles separated by commas, e.g. `analista financiero, contador` →
     `roles: ["analista financiero", "contador"]` (at most 5, each at most 50 characters).
   - `en <lugar>` (optional): the last "en …" is the place only when it names a place (a city,
     comuna, region or country); "ingeniero en minas" is a job title. Pass the place as
     `location`. Without a place, search Chile (the default). If the place is outside Chile, ask
     the user before searching.
   - `--recientes` (optional): offers from the last week → `posted_within: "1w"`.
   - No titles: ask the user which job titles to search for.
2. Call `search_jobs` with those arguments.
3. With fewer than 5 results, call `related_roles` for the main title, search again with the most
   relevant related titles or without the filters, and say that you widened the search.
4. Show the offers as a table: Cargo | Empresa | Ubicación | Sueldo | Modalidad | Publicado | Link,
   in the user's language. Add the total, the location Luk applied (`effective_location`) and any
   relevant `warnings`.
5. Offer next steps: more results, details of an offer (`get_jobs`), or `luk open <slug>` so the
   user can apply in their browser (they click "Postular"). Never apply.
