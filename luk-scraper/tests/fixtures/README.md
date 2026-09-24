# Test fixtures

`listing.html` and `detail.html` are **minimized** takealuk.com pages: they keep the markup the
parser relies on (card classes, `data-scroll-restore-slug`, pagination links, the result count,
the `schema.org/JobPosting` JSON-LD), with the structure as captured on 2026-09-23.

Everything that could identify a real employer, offer or person is **fake**:

| Real content | Fake replacement |
| --- | --- |
| employer names | `Empresa Demo 01 SpA`, … |
| job titles, slugs, descriptions | generic fake titles, matching fake slugs, short fake Spanish text |
| logos and images | `https://example.com/logo.png` |
| employer RUT | `11.111.111-0` (invalid check digit on purpose) |
| ids, tokens | small fake numbers, `FAKE-TOKEN-…` |

Place names, generic labels ("Jornada Completa", "Presencial") and counts are kept, because the
parser is tested on them.

To add a fixture: save the page locally, keep only the markup the parser needs, replace every
employer, title, text, image, id and token with fake data as above, and start the file with a
`<!-- FIXTURE: … -->` comment. Never commit a raw capture.
