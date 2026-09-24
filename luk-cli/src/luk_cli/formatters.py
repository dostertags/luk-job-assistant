"""Output formats for stdout (spec §5.3, §5.4): exactly one of a JSON document, CSV or a table.

Warnings, progress and the `Ubicación:` line go to stderr (`Console(stderr=True)`), never stdout.
Text from Luk is always printed as plain `Text` (never rich markup), so a title like "[Remoto]" stays
literal, and never with control characters: the models strip them (`models.plain_text`), and cells and
warnings are stripped again on the way out (e.g. a warning appended after validation). When stdout is
not a terminal the table is plain: no colour or styles (rich drops them by itself), no box lines, and
`stdout_console()` never wraps or truncates at 80 columns.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Sequence
from datetime import date
from operator import attrgetter
from typing import Any

from pydantic import BaseModel
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from luk_cli.errors import InvalidArgument, LukError
from luk_cli.models import (
    Application, Area, CompanyCard, CookieInfo, Cv, Envelope, ErrorEnvelope, JobCard, JobPosting, ListMeta, Salary,
    Suggestion, plain_text,
)
from luk_cli.redact import redact

_JOB_CSV = (
    "slug", "url", "title", "company", "location", "salary", "salary_currency", "salary_min", "salary_max",
    "salary_period", "employment_type", "modality", "labels", "posted_ago", "posted_at_approx",
)


def _job_row(card: JobCard) -> dict[str, Any]:
    salary = card.salary or Salary()
    return {
        **_plain_row(card), "salary": salary.raw, "salary_currency": salary.currency, "salary_min": salary.min,
        "salary_max": salary.max, "salary_period": salary.period,
    }


def _plain_row(model: BaseModel) -> dict[str, Any]:
    return {k: "; ".join(v) if isinstance(v, list) else v for k, v in model.model_dump(mode="json").items()}


_CSV: dict[str, tuple[Sequence[str], Callable[[Any], dict[str, Any]]]] = {
    "job_search": (_JOB_CSV, _job_row),
    "job_list": (_JOB_CSV, _job_row),
    "company_list": (tuple(CompanyCard.model_fields), _plain_row),
    "application_list": (tuple(Application.model_fields), _plain_row),
}
CSV_KINDS = frozenset(_CSV)
_PLAIN_WIDTH = 100_000  # a piped table is never wrapped or truncated

Column = tuple[str, Callable[[Any], object]]


def _columns(*pairs: tuple[str, str]) -> tuple[Column, ...]:
    return tuple((header, attrgetter(name)) for name, header in pairs)


_JOB_COLUMNS = _columns(("title", "Title"), ("company", "Company"), ("location", "Location"), ("salary", "Salary"),
                        ("modality", "Modality"), ("posted_ago", "Posted"), ("slug", "Slug"))
_COLUMNS: dict[type, tuple[Column, ...]] = {
    JobCard: _JOB_COLUMNS,
    JobPosting: _columns(("title", "Title"), ("company", "Company"), ("salary", "Salary"),
                         ("valid_through", "Valid through"), ("status", "Status"), ("slug", "Slug")),
    CompanyCard: _columns(("name", "Name"), ("location", "Location"), ("sector", "Sector"), ("size", "Size"),
                          ("active_offers", "Offers"), ("slug", "Slug")),
    Area: _columns(("id", "Id"), ("display_path", "Area"), ("area_type_label", "Type"), ("offer_count", "Offers")),
    Suggestion: _columns(("query", "Query"), ("popularity", "Popularity")),
    Application: _columns(("title", "Title"), ("company", "Company"), ("applied_at_text", "Applied"),
                          ("status_text", "Status"), ("slug", "Slug")),
    Cv: _columns(("name", "Name"), ("updated_at_text", "Updated")),
    CookieInfo: _columns(("name", "Cookie"), ("domain", "Domain"), ("expires", "Expires")),
    str: (("Role", str),),
}
# kind → (field holding the rows, row type); every other kind is shown as a field/value table.
_LISTS: dict[str, tuple[str, type]] = {
    "job_search": ("results", JobCard), "job_list": ("results", JobCard), "jobs": ("results", JobPosting),
    "company_list": ("results", CompanyCard), "application_list": ("results", Application),
    "cv_list": ("results", Cv), "suggestion_list": ("results", Suggestion), "area_list": ("results", Area),
    "similar_roles": ("items", str),
}
# fields rendered as a table of their own under the main one
_SUB_TABLES: dict[str, type] = {"details": JobPosting, "jobs": JobCard, "cookies": CookieInfo,
                                "suggestions": Suggestion}


def json_document(envelope: Envelope[Any]) -> str:
    """The envelope as one JSON document (`ensure_ascii=False`, every key present)."""
    return json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False, indent=2)


def error_document(err: LukError) -> str:
    """`{"schema_version": 2, "kind": "error", "error": {code, exit_code, message, hint}}` (redacted)."""
    return redact(json.dumps(ErrorEnvelope.from_error(err).model_dump(mode="json"), ensure_ascii=False, indent=2))


def csv_document(envelope: Envelope[Any]) -> str:
    """UTF-8 BOM + header row + one row per result, for CSV_KINDS only (else InvalidArgument).

    Lines end in "\\n"; a text stdout turns them into the platform's line ending.
    """
    if envelope.kind not in _CSV:
        raise InvalidArgument(f"CSV output is not available for {envelope.kind}")
    headers, row = _CSV[envelope.kind]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(headers), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for item in envelope.data.results:
        writer.writerow(row(item))
    return "\ufeff" + buffer.getvalue()


def stdout_console() -> Console:
    """The console for tables on stdout: rich's own when a terminal, else a plain one that never wraps."""
    console = Console()
    return console if console.is_terminal else Console(width=_PLAIN_WIDTH)


def render_table(envelope: Envelope[Any], console: Console) -> None:
    """A rich table (plain when not a TTY) of the envelope's data on the stdout console."""
    data = envelope.data
    lines = box.SIMPLE_HEAD if console.is_terminal else None
    rows_field = _LISTS.get(envelope.kind)
    if rows_field is None:
        console.print(_fields_table(data, lines))
    else:
        name, row_type = rows_field
        console.print(_rows_table(getattr(data, name), row_type, lines, caption=_caption(data)))
    for name, row_type in _SUB_TABLES.items():
        rows = getattr(data, name, None)
        if rows and (rows_field is None or rows_field[0] != name):
            console.print(_rows_table(rows, row_type, lines, title=name))


def render_warnings(warnings: Sequence[str], console: Console) -> None:
    """One line per warning on the stderr console."""
    for warning in warnings:
        console.print(plain_text(warning), markup=False, highlight=False, emoji=False, soft_wrap=True)


def _cell(value: object) -> str:
    return plain_text(_cell_text(value))


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Salary):
        if value.raw:
            return value.raw
        amounts = " - ".join(str(v) for v in (value.min, value.max) if v is not None)
        return " ".join(p for p in (value.currency, amounts, value.period) if p)
    if isinstance(value, BaseModel):
        return "; ".join(f"{k}: {_cell(v)}" for k, v in value if v not in (None, [], ""))
    if isinstance(value, list):
        return ", ".join(_cell(v) for v in value)
    return str(value)


def _rows_table(rows: Sequence[Any], row_type: type, lines: box.Box | None, *, title: str | None = None,
                caption: Text | None = None) -> Table:
    columns = _COLUMNS[row_type]
    table = Table(box=lines, title=Text(title) if title else None, caption=caption)
    for header, _ in columns:
        table.add_column(header)
    for row in rows:
        table.add_row(*(Text(_cell(get(row))) for _, get in columns))
    return table


def _fields_table(data: BaseModel, lines: box.Box | None) -> Table:
    table = Table(box=lines, show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    for name, value in data:
        if name not in _SUB_TABLES and value not in (None, [], ""):
            table.add_row(Text(name), Text(_cell(value)))
    return table


def _caption(data: BaseModel) -> Text | None:
    if not isinstance(data, ListMeta):
        return None
    total = f"{len(getattr(data, 'results', []))} shown" if data.total is None else f"{data.total} results"
    last = data.last_fetched_page
    pages = f"page {data.page}" if last == data.page else f"pages {data.page}-{last}"
    of = f" of {data.last_page}" if data.last_page else ""
    more = f"; more: --page {data.next_page} --offset {data.next_offset}" if data.has_more else ""
    return Text(f"{total}; {pages}{of}{more}")
