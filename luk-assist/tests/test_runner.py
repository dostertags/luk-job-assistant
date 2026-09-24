"""The runner: open -> pre-fill -> STOP, one review record per offer, human pause per offer."""

from __future__ import annotations

import json

import pytest
from conftest import EXPERIENCE, RESIDENCE, SALARY, FakeBrowser, StrictBrowser

from luk_assist import runner
from luk_assist.answers import Answers, make_answer_fn
from luk_assist.targets import Target, apply_url

DEMO = Target(slug="analista-comercial-empresa-demo-01", title="Analista Comercial", company="Empresa Demo 01 SpA")


def test_assist_listing_prefills_and_stops(paz_answers):
    fake = FakeBrowser()
    record = runner.assist_listing(fake, DEMO, make_answer_fn(paz_answers))
    assert fake.opened == [apply_url(DEMO.slug)]
    assert record["slug"] == DEMO.slug
    assert record["title"] == "Analista Comercial" and record["company"] == "Empresa Demo 01 SpA"
    assert record["url"] == "https://www.takealuk.com/job_offers/analista-comercial-empresa-demo-01?tab=apply"
    assert record["answers"] == {
        RESIDENCE: "Calle Falsa 123, Comuna Demo, Santiago",
        SALARY: "1500000",
        EXPERIENCE: "6 años en análisis comercial en Empresa Demo 01 SpA.",
    }
    assert record["left_blank"] == []
    assert record["status"] == 'READY_FOR_REVIEW — review and click "Enviar postulación" yourself'


def test_empty_answers_leave_every_question_for_you():
    record = runner.assist_listing(FakeBrowser(), DEMO, make_answer_fn(Answers()))
    assert record["answers"] == {}
    assert record["left_blank"] == [RESIDENCE, SALARY, EXPERIENCE]
    assert record["status"].startswith("READY_FOR_REVIEW")


def test_assist_listing_calls_pause_once():
    seen = []
    runner.assist_listing(FakeBrowser(), DEMO, make_answer_fn(Answers()), pause=seen.append)
    assert [r["slug"] for r in seen] == [DEMO.slug]


def test_run_pauses_once_per_listing_and_only_uses_formbrowser(paz_answers):
    strict = StrictBrowser()
    seen = []
    targets = [Target("oferta-demo-1"), Target("oferta-demo-2"), Target("oferta-demo-3")]
    queue = runner.run(targets, strict, make_answer_fn(paz_answers), pause=lambda r: seen.append(r["slug"]),
                       sleep=lambda _s: None)
    assert [r["slug"] for r in queue] == ["oferta-demo-1", "oferta-demo-2", "oferta-demo-3"]
    assert seen == ["oferta-demo-1", "oferta-demo-2", "oferta-demo-3"]
    assert strict.opened == [apply_url(t.slug) for t in targets]


def test_run_without_pause_still_prefills_everything(paz_answers):
    fake = FakeBrowser()
    queue = runner.run([Target("oferta-demo-1"), Target("oferta-demo-2")], fake, make_answer_fn(paz_answers),
                       sleep=lambda _s: None)
    assert len(queue) == 2 and len(fake.filled) == 6


@pytest.mark.parametrize("stop", [KeyboardInterrupt, EOFError])
def test_ctrl_c_at_a_pause_stops_and_keeps_the_records(stop, paz_answers):
    def pause(record):
        if record["slug"] == "oferta-demo-2":
            raise stop

    fake = FakeBrowser()
    queue = runner.run([Target("oferta-demo-1"), Target("oferta-demo-2"), Target("oferta-demo-3")], fake,
                       make_answer_fn(paz_answers), pause=pause, sleep=lambda _s: None)
    assert [r["slug"] for r in queue] == ["oferta-demo-1", "oferta-demo-2"]
    assert len(fake.opened) == 2


def test_an_offer_that_cannot_be_opened_is_recorded_and_the_run_goes_on(paz_answers):
    class FlakyBrowser(FakeBrowser):
        def open(self, url):
            if "oferta-demo-1" in url:
                raise TimeoutError("page load timed out")
            super().open(url)

    seen = []
    fake = FlakyBrowser()
    queue = runner.run([Target("oferta-demo-1"), Target("oferta-demo-2")], fake, make_answer_fn(paz_answers),
                       pause=lambda r: seen.append(r["slug"]), sleep=lambda _s: None)
    assert [r["status"] for r in queue] == [runner.STATUS_NOT_OPENED, runner.STATUS]
    assert queue[0]["answers"] == {} and queue[0]["url"] == apply_url("oferta-demo-1")
    assert seen == ["oferta-demo-2"]  # no pause for an offer that never opened
    assert len(fake.filled) == 3


def test_run_waits_between_opens():
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    fake = FakeBrowser(fields=[])
    runner.run([Target("a-1"), Target("b-2"), Target("c-3")], fake, make_answer_fn(Answers()),
               delay_s=2.0, sleep=sleep, clock=lambda: now[0])
    assert slept == [2.0, 2.0]  # none before the first offer


def test_human_review_time_counts_towards_the_delay():
    now = [0.0]
    slept: list[float] = []

    def pause(_record):
        now[0] += 30.0  # you spent 30 s reviewing

    runner.run([Target("a-1"), Target("b-2")], FakeBrowser(fields=[]), make_answer_fn(Answers()),
               pause=pause, delay_s=2.0, sleep=slept.append, clock=lambda: now[0])
    assert slept == []


def test_duplicate_labels_do_not_overwrite_each_other():
    from luk_assist.browser import Field

    fields = [Field(name="a", label="Comuna", kind="text"), Field(name="b", label="Comuna", kind="text")]
    record = runner.assist_listing(FakeBrowser(fields), DEMO, make_answer_fn(Answers(profile={"comuna": "X"})))
    assert record["answers"] == {"Comuna": "X", "Comuna (2)": "X"}


def test_save_queue(tmp_path):
    record = runner.assist_listing(FakeBrowser(), DEMO, make_answer_fn(Answers()))
    out = runner.save_queue(tmp_path / "nested" / "review_queue.json", [record])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data[0]["slug"] == DEMO.slug and data[0]["status"] == runner.STATUS
    assert "Enviar postulación" in out.read_text(encoding="utf-8")  # UTF-8, not \\u escapes


def test_console_pause_shows_answers_and_blanks_and_waits_once():
    answer_fn = make_answer_fn(Answers(profile={"comuna": "Comuna Demo"}))
    record = runner.assist_listing(FakeBrowser(), DEMO, answer_fn)
    lines: list[str] = []
    prompts: list[str] = []
    runner.console_pause(record, input_fn=lambda p: prompts.append(p) or "", out=lines.append)
    text = "\n".join(lines)
    assert "Analista Comercial - Empresa Demo 01 SpA" in text
    assert f"+ {RESIDENCE}: Comuna Demo" in text
    assert f"- left blank for you: {SALARY}" in text
    assert "YOURSELF" in text and "never submits" in text
    assert len(prompts) == 1


def test_console_pause_says_when_no_form_was_found():
    record = runner.assist_listing(FakeBrowser(fields=[]), DEMO, make_answer_fn(Answers()))
    lines: list[str] = []
    runner.console_pause(record, input_fn=lambda _p: "", out=lines.append)
    assert any("not logged in" in line for line in lines)


# --- no application form: FORM_NOT_FOUND, never READY_FOR_REVIEW --------------------------------
def test_an_offer_without_an_application_form_is_form_not_found(paz_answers):
    fake = FakeBrowser(no_form_on={DEMO.slug})
    record = runner.assist_listing(fake, DEMO, make_answer_fn(paz_answers))
    assert record["status"] == runner.STATUS_FORM_NOT_FOUND
    assert not record["status"].startswith("READY_FOR_REVIEW")
    assert record["answers"] == {} and record["left_blank"] == [] and record["fields_detected"] == 0
    assert fake.filled == []  # nothing is typed on a page whose form was not found


def test_run_mixes_form_not_found_and_ready_records(paz_answers):
    fake = FakeBrowser(no_form_on={"oferta-demo-1"})
    queue = runner.run([Target("oferta-demo-1"), Target("oferta-demo-2")], fake, make_answer_fn(paz_answers),
                       sleep=lambda _s: None)
    assert [r["status"] for r in queue] == [runner.STATUS_FORM_NOT_FOUND, runner.STATUS]
    assert len(fake.filled) == 3


def test_console_pause_explains_form_not_found():
    record = runner.assist_listing(FakeBrowser(no_form_on={DEMO.slug}), DEMO, make_answer_fn(Answers()))
    lines: list[str] = []
    runner.console_pause(record, input_fn=lambda _p: "", out=lines.append)
    text = "\n".join(lines)
    assert "No application form found" in text and "not logged in" in text
    assert "never submits" in text
