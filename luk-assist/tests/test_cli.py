"""The CLI: parser, --init-answers, and a full offline run with a fake browser."""

from __future__ import annotations

import json

import pytest
from conftest import EXPERIENCE, REPO_ROOT, RESIDENCE, SALARY, FakeBrowser

from luk_assist import cli, runner
from luk_assist.browser import BrowserUnavailable


@pytest.fixture(autouse=True)
def no_politeness_delay(monkeypatch):
    """The fake browser makes no requests, so skip the real delay between offers."""
    slept: list[float] = []
    monkeypatch.setattr(runner, "_sleep", slept.append)
    return slept


class FakeSessionBrowser(FakeBrowser):
    """FakeBrowser plus the lifecycle the CLI drives (start / show / wait_until_closed / close)."""

    def __init__(self, storage_state=None, fail_start=False):
        super().__init__()
        self.storage_state = storage_state
        self.fail_start = fail_start
        self.events: list[str] = []

    def start(self):
        if self.fail_start:
            raise BrowserUnavailable("Playwright is not installed.")
        self.events.append("start")
        return self

    def show(self, url):
        self.events.append(f"show {url}")

    def wait_until_closed(self):
        self.events.append("wait_until_closed")

    def close(self):
        self.events.append("close")


@pytest.fixture
def workspace(tmp_path):
    listings = tmp_path / "offers.jsonl"
    listings.write_text(
        json.dumps({"slug": "analista-demo-01", "title": "Analista Demo", "company": "Empresa Demo 01 SpA"}) + "\n"
        + json.dumps({"slug": "ejecutivo-demo-02", "title": "Ejecutivo Demo", "company": "Empresa Demo 02 SpA"})
        + "\n",
        encoding="utf-8",
    )
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"profile": {"comuna": "Comuna Demo"}, "fixed": {"salary": "1.500.000"}}),
                       encoding="utf-8")
    return tmp_path, listings, answers


def _run(argv, browser=None, answers_for_input=None):
    made: list[FakeSessionBrowser] = []
    prompts: list[str] = []

    def factory(state):
        b = browser or FakeSessionBrowser(storage_state=state)
        b.storage_state = state
        made.append(b)
        return b

    def input_fn(prompt):
        prompts.append(prompt)
        return ""

    code = cli.main(argv, browser_factory=factory, input_fn=input_fn)
    return code, (made[0] if made else None), prompts


# --- parser -----------------------------------------------------------------------------------
def test_parser_options_and_defaults():
    parser = cli.build_parser()
    options = {s for a in parser._actions for s in a.option_strings}
    assert {"--listings", "--cv", "--answers", "--init-answers", "--limit", "--storage-state", "--out",
            "--no-pause", "--fill-generic"} <= options
    args = parser.parse_args(["--listings", "x.jsonl"])
    assert args.limit == cli.DEFAULT_LIMIT and not args.no_pause and not args.fill_generic


def test_there_is_no_option_to_submit_or_hide_the_browser():
    options = {s for a in cli.build_parser()._actions for s in a.option_strings}
    for forbidden in ("--headless", "--submit", "--send", "--auto", "--yes", "--password", "--enviar"):
        assert forbidden not in options


@pytest.mark.parametrize("value", ["0", "-3", "many"])
def test_limit_must_be_positive(value):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--listings", "x.jsonl", "--limit", value])


def test_listings_is_required(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


# --- --init-answers ---------------------------------------------------------------------------
def test_init_answers_creates_the_template_once(tmp_path, capsys):
    path = tmp_path / "cfg" / "answers.json"
    assert cli.main(["--init-answers", "--answers", str(path)]) == 0
    assert "Created" in capsys.readouterr().out
    path.write_text('{"profile": {"comuna": "Comuna Demo"}}', encoding="utf-8")
    assert cli.main(["--init-answers", "--answers", str(path)]) == 0
    assert "unchanged" in capsys.readouterr().out
    assert json.loads(path.read_text(encoding="utf-8")) == {"profile": {"comuna": "Comuna Demo"}}


# --- full run with a fake browser -------------------------------------------------------------
def test_full_run_prefills_pauses_and_writes_the_queue(workspace, capsys):
    tmp, listings, answers = workspace
    out = tmp / "queue.json"
    code, browser, prompts = _run(["--listings", str(listings), "--answers", str(answers), "--out", str(out)])
    assert code == 0
    stdout = capsys.readouterr().out
    assert cli.BANNER in stdout
    assert "none submitted" in stdout
    # login prompt on luk's home page, then one pause per offer
    assert browser.events[:2] == ["start", f"show {cli.LUK_HOME}"]
    assert browser.events[-2:] == ["wait_until_closed", "close"]
    assert len(prompts) == 3 and "never does" in prompts[0]
    assert browser.opened == ["https://www.takealuk.com/job_offers/analista-demo-01?tab=apply",
                              "https://www.takealuk.com/job_offers/ejecutivo-demo-02?tab=apply"]
    queue = json.loads(out.read_text(encoding="utf-8"))
    assert [r["slug"] for r in queue] == ["analista-demo-01", "ejecutivo-demo-02"]
    assert queue[0]["answers"] == {RESIDENCE: "Comuna Demo", SALARY: "1500000"}
    assert queue[0]["left_blank"] == ["Comenta tu experiencia relacionada al cargo"]
    assert all(r["status"].startswith("READY_FOR_REVIEW") for r in queue)


def test_summary_counts_offers_without_an_application_form(workspace, capsys):
    tmp, listings, answers = workspace
    out = tmp / "q.json"
    browser = FakeSessionBrowser()
    browser._no_form_on = {"ejecutivo-demo-02"}
    code, _, _ = _run(["--listings", str(listings), "--answers", str(answers), "--out", str(out), "--no-pause"],
                      browser=browser)
    assert code == 0
    stdout = capsys.readouterr().out
    assert "1 without an application form" in stdout and "none submitted" in stdout
    queue = json.loads(out.read_text(encoding="utf-8"))
    assert [r["status"] for r in queue] == [runner.STATUS, runner.STATUS_FORM_NOT_FOUND]


def test_no_pause_never_prompts(workspace):
    tmp, listings, answers = workspace
    code, browser, prompts = _run(["--listings", str(listings), "--answers", str(answers),
                                   "--out", str(tmp / "q.json"), "--no-pause"])
    assert code == 0 and prompts == []
    assert not any(e.startswith("show") for e in browser.events)
    assert len(browser.opened) == 2


def test_limit(workspace):
    tmp, listings, answers = workspace
    _, browser, _ = _run(["--listings", str(listings), "--answers", str(answers), "--out", str(tmp / "q.json"),
                          "--limit", "1", "--no-pause"])
    assert len(browser.opened) == 1


def test_no_pause_run_is_still_polite(workspace, no_politeness_delay):
    tmp, listings, answers = workspace
    _run(["--listings", str(listings), "--answers", str(answers), "--out", str(tmp / "q.json"), "--no-pause"])
    assert len(no_politeness_delay) == 1 and 0 < no_politeness_delay[0] <= runner.MIN_DELAY_S


def test_fill_generic_is_opt_in(workspace):
    tmp, listings, answers = workspace
    out = tmp / "q.json"
    base = ["--listings", str(listings), "--answers", str(answers), "--out", str(out), "--no-pause"]
    _run(base)
    assert EXPERIENCE not in json.loads(out.read_text(encoding="utf-8"))[0]["answers"]
    _run([*base, "--fill-generic"])
    filled = json.loads(out.read_text(encoding="utf-8"))[0]["answers"]
    assert filled[EXPERIENCE] == "Puedo ampliar esta información en una entrevista."


def test_storage_state_is_passed_to_the_browser_and_skips_the_login_prompt(workspace):
    tmp, listings, answers = workspace
    state = tmp / "session.json"
    state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    code, browser, prompts = _run(["--listings", str(listings), "--answers", str(answers),
                                   "--out", str(tmp / "q.json"), "--storage-state", str(state)])
    assert code == 0
    assert browser.storage_state == state
    assert not any(e.startswith("show") for e in browser.events)
    assert len(prompts) == 2  # only the per-offer pauses


def test_missing_storage_state_is_an_error(workspace):
    tmp, listings, answers = workspace
    code, browser, _ = _run(["--listings", str(listings), "--answers", str(answers),
                             "--storage-state", str(tmp / "nope.json")])
    assert code == 2 and browser is None


def test_cv_fills_only_empty_answers(workspace, monkeypatch, capsys):
    tmp, listings, answers = workspace
    pdf = tmp / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(cli, "text_from_pdf",
                        lambda _p: "Resumen: Analista con 6 años en Empresa Demo 01 SpA.\n\nComuna: Otra Comuna\n")
    out = tmp / "q.json"
    code, _, _ = _run(["--listings", str(listings), "--answers", str(answers), "--cv", str(pdf),
                       "--out", str(out), "--no-pause"])
    assert code == 0
    assert "CV: used for experiencia" in capsys.readouterr().out
    first = json.loads(out.read_text(encoding="utf-8"))[0]["answers"]
    assert first[RESIDENCE] == "Comuna Demo"  # your answers file wins over the CV
    assert first["Comenta tu experiencia relacionada al cargo"].startswith("Analista con 6 años")


def test_missing_cv_is_an_error(workspace):
    tmp, listings, answers = workspace
    code, _, _ = _run(["--listings", str(listings), "--answers", str(answers), "--cv", str(tmp / "nope.pdf")])
    assert code == 2


def test_bad_answers_file_is_an_error(workspace):
    tmp, listings, answers = workspace
    answers.write_text("{broken", encoding="utf-8")
    code, browser, _ = _run(["--listings", str(listings), "--answers", str(answers)])
    assert code == 2 and browser is None


def test_no_offers_exits_1(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n", encoding="utf-8")
    code, browser, _ = _run(["--listings", str(empty), "--answers", str(tmp_path / "a.json")])
    assert code == 1 and browser is None


def test_missing_listings_file_exits_2(tmp_path):
    code, _, _ = _run(["--listings", str(tmp_path / "nope.jsonl"), "--answers", str(tmp_path / "a.json")])
    assert code == 2


def test_missing_playwright_exits_3(workspace):
    tmp, listings, answers = workspace
    code, _, _ = _run(["--listings", str(listings), "--answers", str(answers), "--out", str(tmp / "q.json")],
                      browser=FakeSessionBrowser(fail_start=True))
    assert code == 3


def test_a_browser_that_fails_to_start_exits_3(workspace, capsys):
    tmp, listings, answers = workspace

    class Locked(FakeSessionBrowser):
        def start(self):
            raise RuntimeError("profile in use")

    code, _, _ = _run(["--listings", str(listings), "--answers", str(answers), "--out", str(tmp / "q.json")],
                      browser=Locked())
    assert code == 3 and "profile in use" in capsys.readouterr().err


def test_ctrl_c_at_the_login_prompt_exits_130_and_closes(workspace):
    tmp, listings, answers = workspace
    browser = FakeSessionBrowser()

    def interrupted(_prompt):
        raise KeyboardInterrupt

    code = cli.main(["--listings", str(listings), "--answers", str(answers), "--out", str(tmp / "q.json")],
                    browser_factory=lambda _s: browser, input_fn=interrupted)
    assert code == 130 and browser.events[-1] == "close" and browser.opened == []


def test_warns_when_personal_files_are_inside_a_git_repository(workspace, capsys):
    tmp, listings, answers = workspace
    repo = tmp / "some-repo"
    (repo / ".git").mkdir(parents=True)
    inside = repo / "answers.json"
    inside.write_text(answers.read_text(encoding="utf-8"), encoding="utf-8")
    assert cli.inside_git_work_tree(inside)
    _run(["--listings", str(listings), "--answers", str(inside), "--out", str(tmp / "q.json"), "--no-pause"])
    assert "inside a git repository" in capsys.readouterr().out


def test_default_queue_path_is_outside_the_repository():
    path = cli.default_queue_path().resolve()
    assert path.name == "review_queue.json" and "luk-assist" in path.parts
    assert REPO_ROOT not in path.parents
