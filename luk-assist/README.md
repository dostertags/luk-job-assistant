# luk-assist

luk-assist pre-fills [Luk](https://www.takealuk.com) (takealuk.com) job application forms in a browser
window you can see, and then **stops**. You check every answer and click **"Enviar postulación"**
yourself.

> **Unofficial.** luk-assist is not affiliated with, endorsed by or sponsored by Luk or Buk. "Luk" and "Buk"
> are trademarks of their owners. Use luk-assist with your own account and within Luk's Terms. You are
> responsible for everything you send.

## What it does

1. It reads a list of offers. The list can come from [luk-scraper](../luk-scraper), from luk-cli's
   `--json` output, or from a text file with one offer URL or slug per line.
2. It opens each offer's apply tab (`https://www.takealuk.com/job_offers/{slug}?tab=apply`) in a new tab
   of a Chromium window.
3. It waits for the application form to load. Then it types your answers into the free-text questions
   of that form that are still empty. Every Luk form asks these three:
   - *"Indica tu lugar de residencia (calle, comuna, ciudad)"*: gets your `comuna` answer
   - *"¿Cuál es tu expectativa de renta mensual líquida?"*: gets your `salary`, as digits only
   - *"Comenta tu experiencia relacionada al cargo"*: gets your `experiencia` answer

   If no application form appears (you are not logged in, the offer is closed, or it takes
   applications on another site), it types nothing on that offer and marks it `FORM_NOT_FOUND`.
4. It **stops**. The console shows what it typed and what it left blank for you, then waits for Enter.
   In the browser you review the answers, fill in anything missing, pick your CV, tick what you agree to,
   and click "Enviar postulación" yourself. You can also skip the offer.
5. It writes a review queue: a JSON record of what it typed on each offer.
6. It keeps the browser open until **you** have closed every tab. Closing one tab never closes the
   others.

## Red lines

Tests enforce every rule below. They are not configurable.

- **It only pre-fills and never submits.** No code path submits a form, clicks, or presses keys (Enter
  included). The browser contract (`FormBrowser`) has three methods: `open`, `detect_fields` and `fill`.
  `tests/test_red_lines.py` scans the syntax tree of every module in the package (subpackages
  included). It fails on:
  - acting on the page: `.click(`, `.dblclick(`, `.tap(`, `.press(`, `.type(`, `.check(`,
    `.select_option(`, `.set_input_files(`, `.submit(`, `.dispatch_event(`, `.drag_and_drop(`,
    `.focus(`, and any use of `.keyboard`, `.mouse` or `.touchscreen`;
  - page scripts: `.evaluate(`, `.evaluate_all(`, `.wait_for_function(`, init scripts and exposed
    functions;
  - raw HTTP through your logged-in browser (`ctx.request.post(`, `page.request.fetch(`), request
    routing (`.route(`, `.unroute(`, `.route_from_har(`) and `.set_content(`;
  - leaving a tab you are reviewing: `.reload(`, `.go_back(`, `.go_forward(`. `.goto(` is allowed in
    one place only, on a new tab;
  - a browser launch without the literal `headless=False`, or with extra Chromium `args=`,
    `ignore_default_args=` or `**kwargs`;
  - network libraries, luk-cli or luk-scraper, whether imported statically, through
    `importlib.import_module` / `__import__`, or through `exec` / `eval`;
  - any function whose name contains submit, send, enviar, postular or apply_now.
- **It never ticks consent boxes, radios or selects, and never uploads files.** Choosing your CV and
  giving consent are up to you.
- **It never types your password.** You log in to Luk yourself, in the window it opens. It never fills
  password, PIN, one-time-code, verification-code, token or card fields. It recognises them by their
  label ("Contraseña", "Código de verificación", "Código de 6 dígitos", "Token", "Security code"...),
  by their `autocomplete` (`one-time-code`, `current-password`, `new-password`, `cc-*`), and by short
  numeric code inputs.
- **It never makes up answers.** All answers start empty. It fills a field only when your answers file
  (or your CV) has a value for that kind of question. Anything else stays blank for you. The generic
  sentence is opt-in (`--fill-generic`), goes only into free-text areas, and states no fact about you.
  It never fills e-mail, phone or RUT fields unless you add an explicit `per_question` entry for them.
- **It only types into the application form.** It looks for fields only inside the form that holds the
  "Enviar postulación" button, or inside Luk's application frame. It never searches the rest of the page.
- **It never overwrites your typing.** It skips fields that already hold text. Right before typing, it
  checks that the field is still the same question, still empty, and still not a secret field.
- **The browser window is always visible.** There is no headless mode.
- **It is polite to Luk.** It opens at most one offer every 2 seconds, and at most 10 per run unless you
  pass `--limit`.
- **It works on its own.** It never imports luk-cli or luk-scraper.

## Setup

Python 3.10 or newer. luk-assist is **not published on PyPI**, so always install it from your
checkout of this repository, never with a bare `pip install` of its name (another package with that
name could exist there). Editable installs need pip 21.3 or newer, so update pip first. From the root of
your luk-job-assistant checkout:

```
python -m pip install -U "pip>=21.3"
python -m pip install -e "luk-assist[browser,pdf]"
python -m playwright install chromium
```

From any other folder, use the path to your checkout instead:
`python -m pip install -e "<path to luk-job-assistant>/luk-assist[browser,pdf]"`.

`[browser]` installs Playwright, which is required. `[pdf]` installs pypdf, which you need only for
`--cv`.

## Your answers

```
luk-assist --init-answers
```

This command creates your answers file from a template. It never overwrites a file that already exists.
The file lives in your user folders, outside any repository:

| OS                   | Answers file                                            |
|----------------------|---------------------------------------------------------|
| Windows (cmd)        | `%LOCALAPPDATA%\luk-assist\answers.json`                |
| Windows (PowerShell) | `$env:LOCALAPPDATA\luk-assist\answers.json`             |
| macOS                | `~/Library/Application Support/luk-assist/answers.json` |
| Linux                | `~/.config/luk-assist/answers.json`                     |

The template has the same content as [answers.example.json](answers.example.json): every value is a
`<placeholder>`. luk-assist treats placeholders as empty and never types them. Replace only the values
that are true for you, and leave the rest empty (`""`). The file has three parts:

- **`profile`**: answers by type of question: `titulo`, `experiencia`, `comuna`, `idioma`, `edad`,
  `licencia`, `movilizacion`.
- **`fixed`**: `salary` (expected monthly net salary in CLP, digits only), `availability`,
  `cover_letter`, and `generic` (the sentence used only with `--fill-generic`).
- **`per_question`**: a map from part of a question to your exact answer. Keys ignore case and accents.
  These entries win over the rules below.

**Salary.** `fixed.salary` goes, as digits only, **only** into a question about your *expected* pay.
The question must name pay (renta, sueldo, salario, remuneración, pretensión, líquido...) **and** an
expectation (expectativa, pretensión, aspiración, esperas, esperada, "cuánto quieres ganar"). Every other
question about pay stays blank, even with `--fill-generic`, unless a `per_question` entry answers it:

- your current or last pay (*"¿Cuál es tu sueldo actual?"*, *"último sueldo"*);
- gross pay (*"renta bruta"*);
- pay with no expectation in the question (*"Renta"*);
- your experience with payroll (*"Comenta tu experiencia en remuneraciones"*, *"cálculo de sueldo
  bruto a líquido"*). Questions like these that mention experiencia get your `experiencia` answer.

The other questions are matched to an answer by keywords. The first matching row wins, and each keyword
must start a word:

| The question mentions (e.g.)                                       | Answer used          |
|--------------------------------------------------------------------|----------------------|
| disponibilidad, fecha de inicio, incorporación                     | `fixed.availability` |
| carta, presentación, motivación, mensaje, cuéntanos sobre ti       | `fixed.cover_letter` |
| título, profesión, carrera, estudios, formación, nivel educacional | `profile.titulo`     |
| experiencia, trayectoria, te has desempeñado, área                 | `profile.experiencia` |
| edad                                                               | `profile.edad`       |
| licencia                                                           | `profile.licencia`   |
| inglés, idioma, portugués                                          | `profile.idioma`     |
| comuna, residencia, dónde vives, ubicación, dirección, sector      | `profile.comuna`     |
| movilización, vehículo, auto propio, locomoción                    | `profile.movilizacion` |

**CV (optional).** `--cv my-cv.pdf` reads your CV on your own machine. It uses the CV only to fill answers
that are **empty** in your answers file, and it takes only what it finds:

- a summary section ("Resumen", "Perfil profesional", "Summary"): `experiencia`
- a `Título:` or `Profesión:` line: `titulo`
- a `Comuna:`, `Residencia:`, `Dirección:` or `Ciudad:` line: `comuna`
- only an explicit `Idiomas:`, `Idioma(s):` or `Languages:` line: `idioma`. Items that say no, ninguno,
  sin, not or none are dropped. A mere mention of English (a school name, a course, "Inglés: No") is
  never turned into an answer.

If you already set a value in your answers file, the CV does not replace it.

## Workflow

**1. Get a list of offers.** Pick one:

```
# with luk-scraper (see ../luk-scraper/README.md): export to JSONL or CSV
# with luk-cli, in cmd, macOS or Linux:
luk search analista --json > offers.json
# with luk-cli, in PowerShell (saves UTF-8):
luk search analista --json | Out-File -Encoding utf8 offers.json
# or by hand: offers.txt with one offer URL or slug per line
```

Windows PowerShell 5.1 saves `>` output as UTF-16. luk-assist reads that too: it detects UTF-8,
UTF-16 and UTF-32 files by their byte-order mark. The `Out-File -Encoding utf8` form keeps the file
readable by other tools as well. CSV files may use `,`, `;` (Excel in Spanish locales) or tabs, and
may be saved by Excel as "CSV" (Windows-1252) or "CSV UTF-8".

**2. Run luk-assist:**

```
luk-assist --listings offers.jsonl --cv my-cv.pdf
```

- **First run.** A Chromium window opens on Luk's home page. Log in there yourself, with e-mail, Google or
  LinkedIn. luk-assist keeps its own browser profile, so this login lasts across runs. Press Enter in the
  console to start.
- **At each offer.** The console lists what was typed and what was left blank. Review the tab, fill in
  the rest, and click "Enviar postulación" yourself if you want to apply. Then press Enter for the next
  offer. Ctrl+C stops the run and keeps the review queue so far.
- **When you are done.** Close the tabs or the browser window. luk-assist waits until no tab is left:
  closing one tab (even the first) never closes the others. Each offer opened in its own tab, so
  luk-assist never navigates away from a form you are still sending.

**Already logged in with luk-cli?** Pass the session file that `luk login` saved to reuse that login in a
fresh window. On Windows the file is in your local app data folder:

```
# cmd
luk-assist --listings offers.jsonl --storage-state "%LOCALAPPDATA%\luk-cli\session.json"
# PowerShell
luk-assist --listings offers.jsonl --storage-state "$env:LOCALAPPDATA\luk-cli\session.json"
```

That file is a login credential. Keep it private.

**`--no-pause`** opens and pre-fills every offer in its own tab without stopping. Review each tab
afterwards, then close the window.

### Options

| Option | Meaning |
|--------|---------|
| `--listings FILE` | Offers to open: `.jsonl`, `.csv`, `.json` (a list, or luk-cli `--json`), or a `.txt` with one URL or slug per line |
| `--cv PDF` | Your CV. It fills only answers that are empty in your answers file |
| `--answers PATH` | Your answers file. Default: see the table above |
| `--init-answers` | Create the answers template, never overwriting an existing file, and exit |
| `--limit N` | Open at most N offers. Default: 10 |
| `--storage-state PATH` | Use a Playwright storage-state file, such as luk-cli's session, instead of luk-assist's browser profile |
| `--out PATH` | Where to write the review queue. Default: `<user data dir>/luk-assist/review_queue.json` |
| `--no-pause` | Pre-fill every offer without stopping, then review the tabs |
| `--fill-generic` | Opt-in: put a neutral sentence in free-text questions you have no answer for |

Exit codes: 0 done, 1 no offers found, 2 bad input, 3 Playwright or Chromium missing, 130 interrupted.

### Review queue

The review queue has one record per offer. It shows what luk-assist typed and what is still up to you:

```json
{
  "slug": "analista-comercial-empresa-demo-01",
  "title": "Analista Comercial",
  "company": "Empresa Demo 01 SpA",
  "url": "https://www.takealuk.com/job_offers/analista-comercial-empresa-demo-01?tab=apply",
  "answers": {
    "Indica tu lugar de residencia (calle, comuna, ciudad)": "Comuna Demo, Santiago",
    "¿Cuál es tu expectativa de renta mensual líquida?": "1500000"
  },
  "left_blank": ["Comenta tu experiencia relacionada al cargo"],
  "fields_detected": 3,
  "status": "READY_FOR_REVIEW — review and click \"Enviar postulación\" yourself"
}
```

`status` is one of:

- `READY_FOR_REVIEW`: the form was found and pre-filled. Review it and send it yourself.
- `FORM_NOT_FOUND`: the page opened, but no application form appeared (you are not logged in, the
  offer is closed, or it takes applications on another site). Nothing was typed.
- `NOT_OPENED`: the page could not be opened. Nothing was typed.

The summary at the end of a run counts the offers in each case.

## Privacy

- Your answers file, your CV, the browser profile (which holds your Luk login cookies) and the review
  queue all stay in your user folders. None of them goes in a repository. luk-assist warns you if you
  point any of them inside a git repository. **Never commit them.** This package's `.gitignore` excludes
  `answers.json`, `review_queue.json`, `session.json` and PDFs as a safety net.
- The browser profile is in `<user data dir>/luk-assist/browser-profile`. On Windows that is
  `%LOCALAPPDATA%\luk-assist\browser-profile` (cmd) or `$env:LOCALAPPDATA\luk-assist\browser-profile`
  (PowerShell). Delete that folder to log out and forget everything.
- luk-assist sends no telemetry and does no network I/O of its own. The only site it opens is
  takealuk.com, in the window you see. It reads your CV locally.

## How it works, and its limits

Luk loads the application form after the page itself, inside a Hotwire Turbo Frame, from
`/job_offers/{slug}/external_application_form`. This was last verified live on 2026-09-23. luk-assist
waits up to 20 s for the "Enviar postulación" button or a textarea inside the Turbo Frame. It then looks
only inside the `<form>` that contains that button, or else inside the application Turbo Frame. If
neither appears, the offer is marked `FORM_NOT_FOUND` and nothing is typed: you are not logged in, the
offer is closed, or the offer takes applications on another site.

If Luk changes its markup, luk-assist can fail to find the fields. It then fills nothing, so a failure
never sends anything.

## Development

Install the package in editable mode with its test extra (from the root of your checkout), then run
the tests from the package folder. These commands work the same in cmd, PowerShell and POSIX shells:

```
python -m pip install -U "pip>=21.3"
python -m pip install -e "luk-assist[dev]"
cd luk-assist
python -m pytest -q
```

The tests run offline. They use a fake `FormBrowser`, a fake `playwright` module and CV text in memory, so
no network, browser or PDF is needed. All people and companies in the tests are fictional.

## License

MIT. See [LICENSE](LICENSE). The license covers the code. It does not change the red lines above.
