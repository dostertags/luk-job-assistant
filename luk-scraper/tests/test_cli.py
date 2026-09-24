"""CLI: argument parsing, exit codes and a full crawl with a fake fetcher (offline)."""
from __future__ import annotations

import pytest

from conftest import BASE, FakeFetcher, listing_page
from luk_scraper import __version__, cli, config
from luk_scraper.fetcher import Blocked, FetchError
from luk_scraper.output import read_csv, read_jsonl


@pytest.fixture
def use_fake(monkeypatch):
    """Make the CLI build a FakeFetcher; returns a holder with the instance and its kwargs."""
    holder = {}

    def install(fake: FakeFetcher):
        def factory(**kwargs):
            holder["kwargs"] = kwargs
            holder["fetcher"] = fake
            return fake
        monkeypatch.setattr(cli, "Fetcher", factory)
        return holder
    return install


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"luk-scraper {__version__}"


def test_parse_crawl_arguments():
    args = cli.build_parser().parse_args([
        "crawl", "--out", "o.csv", "--countries", "Chile, Perú", "--date", "3d",
        "--max-pages", "5", "--no-enrich", "--enrich-limit", "7", "--delay", "3"])
    assert args.out == "o.csv"
    assert args.countries == ["Chile", "Perú"]
    assert args.date == "last_3_days"
    assert (args.max_pages, args.no_enrich, args.enrich_limit, args.delay) == (5, True, 7, 3.0)


def test_defaults():
    args = cli.build_parser().parse_args(["crawl", "--out", "o.jsonl"])
    assert args.countries is None and args.date is None and args.max_pages is None
    assert args.no_enrich is False and args.delay == config.RATE_LIMIT_S


@pytest.mark.parametrize("argv", [
    ["crawl"],                                            # --out is required
    ["crawl", "--out", "o.xlsx"],                         # unsupported extension
    ["crawl", "--out", "o.csv", "--date", "yesterday"],
    ["crawl", "--out", "o.csv", "--max-pages", "0"],
    ["crawl", "--out", "o.csv", "--delay", "inf"],
    ["crawl", "--out", "o.csv", "--countries", " , "],
    [],                                                   # a command is required
])
def test_usage_errors(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def test_countries_are_normalized_to_the_site_names():
    args = cli.build_parser().parse_args(["crawl", "--out", "o.csv", "--countries",
                                          "peru,CHILE,mexico"])
    assert args.countries == ["Perú", "Chile", "México"]


def test_unknown_country_is_a_usage_error_listing_the_valid_names(tmp_path, use_fake, capsys):
    holder = use_fake(FakeFetcher({1: listing_page(["a-1"])}))   # never the network
    out = tmp_path / "o.csv"
    with pytest.raises(SystemExit) as exc:
        cli.main(["crawl", "--out", str(out), "--countries", "Chile,Narnia"])
    assert exc.value.code == 2
    assert "fetcher" not in holder and not out.exists()           # rejected before any crawl
    err = capsys.readouterr().err
    assert "Narnia" in err
    for name in config.COUNTRIES:
        assert name in err


def test_countries_help_shows_the_valid_names_and_an_example(capsys):
    with pytest.raises(SystemExit):
        cli.main(["crawl", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "Chile,Perú" in out
    for name in config.COUNTRIES:
        assert name in out


def test_raw_site_date_value_is_accepted():
    assert cli.build_parser().parse_args(
        ["crawl", "--out", "o.csv", "--date", "last_week"]).date == "last_week"


def test_crawl_writes_jsonl_and_exits_0(tmp_path, use_fake, capsys):
    holder = use_fake(FakeFetcher({1: listing_page(["a-1", "a-2"])}))
    out = tmp_path / "offers.jsonl"
    assert cli.main(["crawl", "--out", str(out), "--max-pages", "1"]) == 0
    rows = read_jsonl(out)
    assert [r["slug"] for r in rows] == ["a-1", "a-2"]
    assert rows[0]["external_id"] == "1"                  # enriched by default
    err = capsys.readouterr().err
    assert "luk-scraper: OK: 2 offers from 1 pages" in err
    assert holder["kwargs"]["delay_s"] == config.RATE_LIMIT_S


def test_crawl_writes_csv_without_enrichment(tmp_path, use_fake):
    holder = use_fake(FakeFetcher({1: listing_page(["a-1"])}))
    out = tmp_path / "offers.csv"
    assert cli.main(["crawl", "--out", str(out), "--no-enrich", "--countries", "Chile,Perú",
                     "--date", "1sem", "-q"]) == 0
    assert [r["slug"] for r in read_csv(out)] == ["a-1"]
    fake = holder["fetcher"]
    assert "detail" not in fake.kinds()
    assert ("list", 1, ("Chile", "Perú"), "last_week") in fake.calls


def test_low_delay_is_raised_to_the_floor(tmp_path, use_fake, capsys):
    holder = use_fake(FakeFetcher({1: listing_page(["a-1"])}))
    cli.main(["crawl", "--out", str(tmp_path / "o.jsonl"), "--delay", "0.1", "--no-enrich"])
    assert holder["kwargs"]["delay_s"] == 0.1              # the Fetcher clamps it...
    assert config.clamp_delay(0.1) == config.MIN_DELAY_S    # ...to the 1 s floor
    assert "below the 1.0s floor" in capsys.readouterr().err


def test_robots_disallow_exits_2_and_writes_nothing(tmp_path, use_fake, capsys):
    use_fake(FakeFetcher({1: listing_page(["a-1"])},
                         robots_txt="User-agent: *\nDisallow: /job_offers\n"))
    out = tmp_path / "o.jsonl"
    assert cli.main(["crawl", "--out", str(out)]) == 2
    assert not out.exists()
    assert "STOP" in capsys.readouterr().err


def test_blocked_exits_2(tmp_path, use_fake):
    use_fake(FakeFetcher({1: Blocked("HTTP 403", status=403)}))
    out = tmp_path / "o.jsonl"
    assert cli.main(["crawl", "--out", str(out)]) == 2
    assert not out.exists()                                 # nothing collected, nothing written


def test_blocked_after_some_pages_writes_partial_and_exits_2(tmp_path, use_fake):
    use_fake(FakeFetcher({1: listing_page(["a-1"]), 2: Blocked("HTTP 429", status=429)}))
    out = tmp_path / "o.jsonl"
    assert cli.main(["crawl", "--out", str(out)]) == 2
    assert [r["slug"] for r in read_jsonl(out)] == ["a-1"]


def test_partial_exits_1(tmp_path, use_fake):
    use_fake(FakeFetcher({1: listing_page(["a-1", "a-2"])},
                         {f"{BASE}/job_offers/a-1": FetchError("HTTP 404", status=404)}))
    out = tmp_path / "o.jsonl"
    assert cli.main(["crawl", "--out", str(out)]) == 1
    assert len(read_jsonl(out)) == 2


def test_error_without_offers_exits_1_and_keeps_old_file(tmp_path, use_fake):
    out = tmp_path / "o.jsonl"
    out.write_text("previous\n", encoding="utf-8")
    use_fake(FakeFetcher({1: FetchError("HTTP 500", status=500)}))
    assert cli.main(["crawl", "--out", str(out)]) == 1
    assert out.read_text(encoding="utf-8") == "previous\n"


def test_logging_handler_is_removed_after_main(tmp_path, use_fake):
    import logging
    use_fake(FakeFetcher({1: listing_page(["a-1"])}))
    before = list(logging.getLogger("luk_scraper").handlers)
    cli.main(["crawl", "--out", str(tmp_path / "o.jsonl"), "--no-enrich"])
    assert logging.getLogger("luk_scraper").handlers == before


def test_module_entry_point_exists():
    import importlib.util
    assert importlib.util.find_spec("luk_scraper.__main__") is not None
