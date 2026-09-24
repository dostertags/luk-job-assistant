"""Luk HTML -> :class:`~luk_scraper.models.Offer` (pure functions, no network).

Markup (verified live 2026-09-23):

- Listing ``/job_offers``: one ``div.job-offer-card`` per offer (15 per page), with
  ``data-scroll-restore-slug`` and an ``<a href="/job_offers/{slug}">``. Inside:
  ``h2.job-offer-card__title`` (title), ``p.item-title`` (company), ``span.break-words``
  (location "Comuna, Provincia, Región, País" / "Comuna, Región, País" / just "País"),
  ``span.tag-navy`` tags (workday such as "Jornada Completa", modality such as "Presencial") and
  a ``<span>Hace ...</span>`` relative date. The card has no absolute date. A loading skeleton
  card without a slug may appear and is skipped. A "save offer" ``<form>`` inside the card also
  points at ``/job_offers/{slug}/save_later``; only the ``<a>`` is used.
- Result count in ``.job-offers-results-count b``; page links ``?page=N`` in ``nav.pagination``.
- Detail ``/job_offers/{slug}``: JSON-LD blocks (``BreadcrumbList`` + ``JobPosting``), escaped
  Rails-style (``\\u003c``). ``baseSalary`` is omitted when the salary is hidden and its shape
  varies, so it is read defensively. ``employmentType`` (FULL_TIME/PART_TIME) is the workday,
  not a contract type, and is not exported. ``hiringOrganization.identifier`` is the employer's
  tax id and is deliberately NOT read; ``identifier.value`` (top level) is the offer id.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterator, Optional
from urllib.parse import unquote

from bs4 import BeautifulSoup

from . import config
from .models import DETAIL_FIELDS, Offer, utc_now_iso
from .text import (COUNTRY_KEYS, html_to_text, key, norm_region, normalize_space,
                   parse_date_any, strip_region_word, to_int)

log = logging.getLogger(__name__)

_SLUG_IN_HREF = re.compile(r"/job_offers/([^/?#\s]+)")
_VALID_SLUG = re.compile(r"\w[\w.~-]*")
_MODALITY_KEYS = ("presencial", "remoto", "hibrido", "teletrabajo", "semipresencial")
_LD_JSON = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)


def _clean_slug(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    slug = unquote(raw.strip())
    return slug if _VALID_SLUG.fullmatch(slug) else None


def slug_from_href(href: Optional[str]) -> Optional[str]:
    """``'/job_offers/analista-demo'`` -> ``'analista-demo'`` (query and fragment dropped)."""
    m = _SLUG_IN_HREF.search(href or "")
    return _clean_slug(m.group(1)) if m else None


def parse_location(text: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Card location -> ``(region, comuna, country)``.

    ``'Las Condes, Santiago, Región Metropolitana, Chile'`` -> ``('Metropolitana', 'Las Condes', 'Chile')``
    ``'Valdivia, Los Ríos, Chile'`` -> ``('Los Ríos', 'Valdivia', 'Chile')``
    ``'Chile'`` -> ``(None, None, 'Chile')``

    For Chile (or no country) the region is the canonical name when :func:`norm_region`
    recognises it; otherwise, and for other countries, it is the site's text without "Región".
    """
    s = normalize_space(text)
    if not s:
        return None, None, None
    parts = [p.strip() for p in s.split(",") if p.strip()]
    country = None
    if parts and key(parts[-1]) in COUNTRY_KEYS:
        country = parts.pop()
    if not parts:
        return None, None, country
    chilean = country is None or key(country) == "chile"
    if len(parts) == 1:
        only = parts[0]
        if re.match(r"(?i)regi[oó]n\b", only):          # "Región de X, Chile": no comuna
            region = norm_region(only) if chilean else None
            return region or strip_region_word(only) or None, None, country
        return None, only, country
    raw_region = parts[-1]
    region = norm_region(raw_region) if chilean else None
    return region or strip_region_word(raw_region) or None, parts[0], country


def _posted_relative(card) -> Optional[str]:
    el = card.find("span", string=lambda s: bool(s) and s.strip().lower().startswith("hace"))
    return normalize_space(el.get_text(" ", strip=True)) if el else None


def _split_tags(tags: list[str]) -> tuple[Optional[str], Optional[str]]:
    """Tags -> (workday, modality). Modality is recognised by keyword; the workday is the first
    other tag. Falls back to the site's order (1st workday, 2nd modality)."""
    modality = next((t for t in tags if any(m in key(t) for m in _MODALITY_KEYS)), None)
    rest = [t for t in tags if t is not modality]
    workday = rest[0] if rest else None
    if modality is None and len(tags) > 1:
        workday, modality = tags[0], tags[1]
    return workday, modality


def parse_card(card, *, fetched_at: Optional[str] = None) -> Optional[Offer]:
    """One ``div.job-offer-card`` (a bs4 Tag) -> Offer, or None when it has no slug."""
    link = card.find("a", href=_SLUG_IN_HREF)
    slug = slug_from_href(link["href"]) if link else None
    slug = slug or _clean_slug(card.get("data-scroll-restore-slug"))
    if not slug:
        return None

    title_el = card.select_one("h2.job-offer-card__title, h2.card-title")
    company_el = card.select_one("p.item-title")
    loc_el = card.select_one("span.break-words")
    location_text = normalize_space(loc_el.get_text(" ", strip=True)) if loc_el else ""
    region, comuna, country = parse_location(location_text)
    tags = [normalize_space(t.get_text(" ", strip=True)) for t in card.select("span.tag-navy")]
    workday, modality = _split_tags([t for t in tags if t])

    return Offer(
        slug=slug,
        title=normalize_space(title_el.get_text(" ", strip=True)) if title_el else "",
        company=(normalize_space(company_el.get_text(" ", strip=True)) or None) if company_el else None,
        location_text=location_text or None,
        region=region,
        comuna=comuna,
        country=country,
        workday=workday,
        modality=modality,
        posted_relative=_posted_relative(card),
        url=config.detail_url(slug),
        fetched_at=fetched_at or utc_now_iso(),
    )


def parse_listing(page_html: Optional[str], *, fetched_at: Optional[str] = None) -> list[Offer]:
    """A listing page -> its offers, in page order (repeats included; the crawler dedupes)."""
    soup = BeautifulSoup(page_html or "", "html.parser")
    stamp = fetched_at or utc_now_iso()
    offers = []
    for card in soup.select("div.job-offer-card"):
        offer = parse_card(card, fetched_at=stamp)
        if offer:
            offers.append(offer)
    return offers


def extract_meta(page_html: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """``(total_results, last_page)`` of a listing page; None when absent.

    The last page is the highest ``page=N`` among the pagination links (all links when the
    page has no ``nav.pagination``).
    """
    soup = BeautifulSoup(page_html or "", "html.parser")
    count = soup.select_one(".job-offers-results-count b")
    total = to_int(count.get_text(strip=True)) if count else None
    links = soup.select("nav.pagination a[href]") or soup.find_all("a", href=True)
    last = None
    for a in links:
        m = re.search(r"[?&]page=(\d+)", a["href"])
        if m:
            n = int(m.group(1))
            last = n if last is None else max(last, n)
    return total, last


# --- detail -------------------------------------------------------------------------------

def _ld_objects(page_html: str) -> Iterator[dict]:
    """Every JSON object in the page's JSON-LD blocks (lists and ``@graph`` flattened)."""
    for m in _LD_JSON.finditer(page_html or ""):
        try:
            data = json.loads(m.group(1).strip(), strict=False)
        except ValueError:
            log.debug("skipping malformed JSON-LD block")
            continue
        stack = [data]
        while stack:
            item = stack.pop(0)
            if isinstance(item, list):
                stack[:0] = item
            elif isinstance(item, dict):
                yield item
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack[:0] = graph


def _is_job_posting(obj: dict) -> bool:
    t = obj.get("@type")
    return t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t)


def _salary(base_salary) -> tuple[Optional[int], Optional[int]]:
    """``baseSalary`` in any shape seen in the wild -> ``(min, max)``, only amounts > 0.

    Shapes: MonetaryAmount with ``value`` = QuantitativeValue ``{minValue, maxValue}`` or
    ``{value}``; ``value`` as a bare Number or Text; MonetaryAmount with ``minValue`` /
    ``maxValue`` directly; a bare scalar; or a list of any of these (the first one wins).
    """
    bs = base_salary
    if isinstance(bs, list):
        bs = next((x for x in bs if x not in (None, "", {}, [])), None)
    if isinstance(bs, dict):
        value = bs.get("value")
        if value is None and ("minValue" in bs or "maxValue" in bs):
            value = bs
    else:
        value = bs
    if isinstance(value, dict):
        low = value.get("minValue")
        if low is None:
            low = value.get("value")
        smin, smax = to_int(low), to_int(value.get("maxValue"))
    else:
        smin, smax = to_int(value), None
    smin = smin if smin and smin > 0 else None
    smax = smax if smax and smax > 0 else None
    if smin and smax and smax < smin:
        smin, smax = smax, smin
    return smin, smax


def _identifier(ident) -> Optional[str]:
    if isinstance(ident, list):
        ident = next((x for x in ident if x), None)
    if isinstance(ident, dict):
        ident = ident.get("value")
    if isinstance(ident, bool) or not isinstance(ident, (str, int)):
        return None
    s = str(ident).strip()
    return s or None


def parse_detail(page_html: Optional[str]) -> dict:
    """Detail page -> the fields it adds, from the first JSON-LD ``JobPosting``.

    Keys (only present when non-empty): ``description`` (plain text), ``salary_min``,
    ``salary_max``, ``published_at`` (datePosted), ``valid_through`` (validThrough),
    ``external_id`` (identifier.value). Returns ``{}`` when there is no JobPosting.
    """
    job = next((o for o in _ld_objects(page_html or "") if _is_job_posting(o)), None)
    if job is None:
        return {}
    out: dict = {}
    desc = job.get("description")
    if isinstance(desc, str):
        out["description"] = html_to_text(desc)
    out["salary_min"], out["salary_max"] = _salary(job.get("baseSalary"))
    out["published_at"] = parse_date_any(job.get("datePosted"))
    out["valid_through"] = parse_date_any(job.get("validThrough"))
    out["external_id"] = _identifier(job.get("identifier"))
    return {k: v for k, v in out.items() if v not in (None, "") and k in DETAIL_FIELDS}
