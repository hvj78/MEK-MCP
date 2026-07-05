#!/usr/bin/env python3
"""
OSZKDK MCP Server
=================

MCP server exposing the search interfaces of the **OSZK Digitális Könyvtár**
(Országos Széchényi Könyvtár / National Széchényi Library digital repository,
https://oszkdk.oszk.hu) to agentic tools such as Claude Code, Codex, etc.

This is a companion to mek_mcp_server.py: MEK (Magyar Elektronikus Könyvtár)
and OSZKDK are two independent Hungarian digital-library catalogues with
only partial overlap, so between them an agent can find works that either
one alone might miss.

Exposed tools
-------------
- oszkdk_simple_search   : quick keyword search across all indexed fields
- oszkdk_advanced_search : fielded search, up to 3 conditions with
                           AND / OR / NOT over title / author / any field
- oszkdk_get_record      : fetch metadata + downloadable files of one record
- oszkdk_top_list        : most-read titles this month / this year / all-time

Transport: stdio (run the file directly, or register it in Claude Code with
`claude mcp add`). See README.md for details.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Annotated, Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://oszkdk.oszk.hu"
SIMPLE_SEARCH_URL = f"{BASE_URL}/search/simple"
ADVANCED_SEARCH_URL = f"{BASE_URL}/search/advanced"
TOPS_URL = f"{BASE_URL}/tops"

USER_AGENT = "OSZKDK-MCP/1.0 (+https://oszkdk.oszk.hu; MCP search client)"
TIMEOUT = httpx.Timeout(60.0, connect=15.0)

# Unlike MEK's legacy /katalog CGI, OSZKDK's endpoints are plain UTF-8 —
# no special form encoding is required here.
PAGE_SIZE = 10  # fixed by the backend; there is no page-size parameter

# Friendly field name -> internal CQL/Dublin-Core index used by "Hol" (Where).
# These three are the only indices the site itself exposes and the only ones
# empirically confirmed to filter correctly (dc.subject and other dc.* names
# were tried and silently return zero hits, i.e. unsupported by the backend).
ADVANCED_FIELDS: dict[str, str] = {
    "title": "dc.title",           # Cím mező
    "author": "dc.author",         # Szerző mező
    "any_field": "cql.serverChoice",  # Bármely mező
}
FIELD_NAMES = sorted(ADVANCED_FIELDS)

# "Hogyan" (How) match mode.
# NOTE (empirically observed): any_word and all_words currently return the
# IDENTICAL result set on the live backend for every case tested — the
# distinction does not appear to be enforced server-side. exact_phrase does
# reliably narrow further. Exposed as-is (mirroring the site's own UI) with
# this caveat documented for the caller.
MATCH_MODES: dict[str, str] = {
    "any_word": "any",       # Bármely szó
    "all_words": "all",      # Minden szó
    "exact_phrase": "=",     # Ez a kifejezés
}

# "DokTip" (document type), applied globally from the FIRST condition only —
# empirically confirmed that a type set on condition 2 or 3 has no effect.
DOCUMENT_TYPES: dict[str, str] = {
    "all": "",
    "books": "text",     # matches the value the site's own row-1 dropdown sends
    "images": "image",   # a small, largely empty collection on this backend
}

RECORD_URL_RE = re.compile(r"/DRJ/(\d+)")
HIT_COUNT_RE = re.compile(r"(\d+)\s*</b>\s*tal[áa]lat", re.IGNORECASE)
NO_HITS_RE = re.compile(r"Nincs tal[áa]lat", re.IGNORECASE)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
    return _client


async def _post_form(url: str, data: dict[str, str]) -> str:
    client = _get_client()
    resp = await client.post(url, data=data)
    resp.raise_for_status()
    return resp.text


async def _get(url: str) -> str:
    client = _get_client()
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def _extract_total(page_html: str) -> int:
    if NO_HITS_RE.search(page_html):
        return 0
    m = HIT_COUNT_RE.search(page_html)
    return int(m.group(1)) if m else 0


def _record_id_from_href(href: str) -> Optional[str]:
    m = RECORD_URL_RE.search(href)
    return m.group(1) if m else None


def _parse_coins(title_attr: str) -> dict[str, str]:
    """Parse an OpenURL ContextObject (COinS) query string, e.g.
    'ctx_ver=Z39.88-2004&rft.aufirst=Sándor&rft.aulast=Petőfi&rft.title=...'.
    """
    from urllib.parse import parse_qsl

    unescaped = html_lib.unescape(title_attr)
    return {k: v for k, v in parse_qsl(unescaped, keep_blank_values=False)}


def _parse_search_hits(page_html: str) -> list[dict[str, Any]]:
    """Parse hits of /search/simple or /search/advanced result pages. Each
    hit block looks like:
        <span class="Z3988" ... title="ctx_ver=...&rft.aulast=...">&nbsp;</span>
        <img alt="szabadon megtekinthető" .../>
        ...
        <a class="big" href="/DRJ/632">Petőfi&nbsp;: János vitéz</a>
    """
    hits: list[dict[str, Any]] = []
    # Split on each result link so every chunk contains one COinS span
    # (if present) followed by its link and access icon.
    for m in re.finditer(
        r'(?:<span class="Z3988"[^>]*title="([^"]*)"[^>]*>.*?</span>.*?)?'
        r'<img alt="([^"]*)"[^>]*/?>.*?'
        r'<a class="big" href="([^"]+)">([^<]+)</a>',
        page_html,
        re.S,
    ):
        coins_raw, access, href, label = m.groups()
        record_id = _record_id_from_href(href)
        if not record_id:
            continue
        coins = _parse_coins(coins_raw) if coins_raw else {}
        authors = _clean(coins.get("rft.aulast", "") + " " + coins.get("rft.aufirst", ""))
        hits.append(
            {
                "record_id": record_id,
                "url": f"{BASE_URL}/DRJ/{record_id}",
                "label": _clean(label),
                "authors": authors.strip() or None,
                "title": _clean(coins.get("rft.title", "")) or None,
                "year": coins.get("rft.date") or None,
                "publisher": coins.get("rft.pub") or None,
                "place": coins.get("rft.place") or None,
                "isbn": coins.get("rft.isbn") or None,
                "access": access,
            }
        )
    return hits


LABEL_ROW_RE = re.compile(
    r'<td[^>]*class="fieldLabel"[^>]*>(?:<span[^>]*>)?([^<]+?)\s*:?\s*(?:</span>)?</td>\s*'
    r'<td[^>]*class="fieldValue"[^>]*>(.*?)</td>',
    re.S,
)


def _parse_record_fields(record_html: str) -> dict[str, Any]:
    """Parse the generic MARC-style label/value table present on both the
    brief (/DRJ/{id}) and labeled (/DRJ/{id}/cimkes) record views. Works for
    any label the catalogue happens to use (Szerző, Megjelenés, Tárgyszavak,
    Osztályozás, Név/nevek, ISBN, Kiadói adatok, Megjegyzések, ...) without
    needing a fixed schema.
    """
    fields: dict[str, Any] = {}
    for label, value_html in LABEL_ROW_RE.findall(record_html):
        label = _clean(label)
        # Multi-valued fields (subjects, ISBNs, names) are <BR>-separated.
        parts = [
            _clean(p) for p in re.split(r"<br\s*/?>", value_html, flags=re.I)
        ]
        parts = [p for p in parts if p]
        if not parts:
            continue
        fields[label] = parts[0] if len(parts) == 1 else parts
    return fields


def _parse_record_files(record_html: str) -> list[dict[str, Any]]:
    """Parse the downloadable-files table on a record page (filename, type,
    size, access level, purpose)."""
    files: list[dict[str, Any]] = []
    for row in re.finditer(
        r"<tr><td align=center><a target=_blank href='([^']+)'>.*?</td>\s*"
        r"<td align=center>([^<]*)</td>\s*"
        r"<td align=center>([^<]*)</td>\s*"
        r"<td align=center>([^<]*)</td>\s*"
        r"<td align=center>([^<]*)</td>\s*"
        r"<td align=center>([^<]*)</td>",
        record_html,
    ):
        url, filename, filetype, size, access, purpose = row.groups()
        size_clean = _clean(size)
        size_num = re.search(r"(\d+)", size_clean)
        files.append(
            {
                "url": url,
                "filename": _clean(filename),
                "type": _clean(filetype),
                "size_bytes": int(size_num.group(1)) if size_num else None,
                "access": _clean(access),
                "purpose": _clean(purpose),
            }
        )
    return files


def _parse_permalink(record_html: str) -> Optional[str]:
    m = re.search(r"name=['\"]link['\"][^>]*value=['\"]([^'\"]+)['\"]", record_html)
    return m.group(1) if m else None


def _parse_title_line(record_html: str) -> Optional[str]:
    m = re.search(r'<td width="380" align="left"><b>\s*([^<]+?)\s*</b>', record_html)
    return _clean(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Advanced search request builder
# ---------------------------------------------------------------------------

class SearchCondition(BaseModel):
    """One condition of the advanced search (up to 3 supported by the site)."""

    field: Literal["title", "author", "any_field"] = Field(
        description="Where to search: title (Cím mező), author (Szerző "
        "mező), or any_field (Bármely mező — searches across all indexed "
        "text)."
    )
    value: str = Field(description="Search text, in Hungarian for Hungarian records.")
    match: Literal["any_word", "all_words", "exact_phrase"] = Field(
        default="all_words",
        description=(
            "any_word = OR of the words, all_words = AND of the words, "
            "exact_phrase = the exact phrase. NOTE: on the current backend "
            "any_word and all_words empirically return identical results; "
            "exact_phrase is the only mode confirmed to narrow further."
        ),
    )
    operator: Literal["and", "or", "not"] = Field(
        default="and",
        description=(
            "Logical operator linking this condition to the PREVIOUS one "
            "(ignored on the first condition). 'not' excludes records "
            "matching this condition."
        ),
    )


def _build_advanced_params(
    conditions: list[SearchCondition], document_type: str, offset: int
) -> dict[str, str]:
    params: dict[str, str] = {"submitted": "true", "limit": str(offset + 1)}
    for i in range(3):
        n = i + 1
        if i < len(conditions):
            c = conditions[i]
            params[f"query{n}"] = c.value
            params[f"relation{n}"] = MATCH_MODES[c.match]
            params[f"index{n}"] = ADVANCED_FIELDS[c.field]
        else:
            params[f"query{n}"] = ""
            params[f"relation{n}"] = "scr"
            params[f"index{n}"] = ""
        # type is only honoured on condition 1 by the backend, but the form
        # always submits all three type fields, so mirror that faithfully.
        params[f"type{n}"] = DOCUMENT_TYPES[document_type] if i == 0 else ""
    for i in range(2):
        n = i + 1
        op = conditions[i + 1].operator if i + 1 < len(conditions) else "and"
        params[f"operator{n}"] = op
    return params


# ---------------------------------------------------------------------------
# MCP server & tools
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "oszkdk-search",
    instructions=(
        "Search tools for the OSZK Digitális Könyvtár (National Széchényi "
        "Library digital repository, https://oszkdk.oszk.hu) — a Hungarian "
        "digital library skewing toward ISBN-registered modern books, "
        "textbooks and monographs, complementing MEK's older/classic "
        "and grey-literature-heavy collection.\n\n"
        "Usage tips:\n"
        "- Catalogue text is Hungarian; translate the user's intent into "
        "Hungarian search terms first.\n"
        "- Prefer oszkdk_advanced_search for field-precise queries "
        "(title vs. author), OR-logic or exclusions (NOT).\n"
        "- Only 3 fields are usable: title, author, any_field (the site's "
        "own advanced search offers no others — e.g. a dedicated subject "
        "field does not exist here, unlike in MEK).\n"
        "- Pages are fixed at 10 hits; use offset (0-based) to page "
        "further (10, 20, ...).\n"
        "- Some records are restricted to OSZK's physical access points "
        "rather than freely downloadable — check the 'access' field on "
        "hits/files before assuming a text is openly readable.\n"
        "- If MEK search comes up empty for a modern, ISBN-bearing "
        "Hungarian book, try this catalogue as a fallback (and vice versa)."
    ),
)


@mcp.tool()
async def oszkdk_simple_search(
    query: Annotated[str, Field(description=(
        "Free-text query searched across all indexed fields (title, "
        "author, subject...). Hungarian terms work best for Hungarian "
        "records, e.g. 'Petőfi Sándor' or 'AutoCAD'."
    ))],
    offset: Annotated[int, Field(ge=0, description=(
        "Result offset for paging (0-based). Page size is fixed at 10 by "
        "the server; pass 10, 20, ... for further pages."
    ))] = 0,
) -> dict[str, Any]:
    """Simple keyword search of the OSZK Digitális Könyvtár catalogue. Good
    for quick, broad lookups. For field-precise queries (title-only,
    author-only), OR-logic, exclusions (NOT) or a document-type filter, use
    oszkdk_advanced_search instead.

    Returns: {total, offset, page_size, hits: [{record_id, url, authors,
    title, year, publisher, place, isbn, access}], has_more}.
    """
    page = await _post_form(
        SIMPLE_SEARCH_URL,
        {"SEARCH": query, "submitted": "true", "limit": str(offset + 1)},
    )
    total = _extract_total(page)
    hits = _parse_search_hits(page)
    return {
        "search_type": "simple",
        "query": query,
        "total": total,
        "offset": offset,
        "page_size": PAGE_SIZE,
        "hits": hits,
        "has_more": offset + len(hits) < total,
    }


@mcp.tool()
async def oszkdk_advanced_search(
    conditions: Annotated[list[SearchCondition], Field(description=(
        "1 to 3 search conditions. Each has a field (title/author/"
        "any_field), a value, a match mode (any_word/all_words/"
        "exact_phrase) and an operator (and/or/not) linking it to the "
        "previous condition. Example (Petőfi as author, excluding a "
        "specific title):\n"
        "[{'field':'author','value':'Petőfi Sándor'},\n"
        " {'field':'title','value':'Ibolyák','match':'exact_phrase',"
        "'operator':'not'}]"
    ))],
    document_type: Annotated[Literal["all", "books", "images"], Field(
        description=(
            "Restrict to books or images (applies globally, not per "
            "condition — this mirrors a real limitation of the site's own "
            "advanced search). The images collection on this backend is "
            "very small/largely empty."
        )
    )] = "all",
    offset: Annotated[int, Field(ge=0, description=(
        "Result offset for paging (0-based). Page size fixed at 10."
    ))] = 0,
) -> dict[str, Any]:
    """Advanced (fielded) search in the OSZKDK catalogue with up to 3
    conditions combined via AND / OR / NOT, restricted to the three fields
    the site itself supports: title, author, any_field.

    Typical patterns:
    - Works BY a person: field=author, value='Petőfi Sándor'.
    - Exclusions: add a condition with operator='not'.
    - Narrowing: use match='exact_phrase' — any_word/all_words currently
      behave identically on this backend (see field docs).
    - Document type: only meaningful as the overall document_type
      parameter, not on individual conditions.

    Returns: {total, offset, page_size, hits: [{record_id, url, authors,
    title, year, publisher, place, isbn, access}], has_more}.
    """
    if not conditions:
        raise ValueError("At least one search condition is required.")
    if len(conditions) > 3:
        raise ValueError(
            "At most 3 search conditions are supported (matches the "
            "site's own advanced search form)."
        )
    params = _build_advanced_params(conditions, document_type, offset)
    page = await _post_form(ADVANCED_SEARCH_URL, params)
    total = _extract_total(page)
    hits = _parse_search_hits(page)
    return {
        "search_type": "advanced",
        "conditions": [c.model_dump() for c in conditions],
        "document_type": document_type,
        "total": total,
        "offset": offset,
        "page_size": PAGE_SIZE,
        "hits": hits,
        "has_more": offset + len(hits) < total,
    }


@mcp.tool()
async def oszkdk_get_record(
    record_id_or_url: Annotated[str, Field(description=(
        "Numeric OSZKDK record id (e.g. '632') or record URL "
        "(e.g. 'https://oszkdk.oszk.hu/DRJ/632')."
    ))],
) -> dict[str, Any]:
    """Fetch full metadata of a single OSZKDK record: all catalogue fields
    (author, title, publication, subjects, classification, notes, ISBN,
    publisher — whatever the record actually has, field names as used by
    the catalogue itself), the list of downloadable files with their
    access level, and the permanent URN link.

    Returns: {record_id, url, title, permalink_urn, fields: {label: value
    or [values]}, files: [{url, filename, type, size_bytes, access,
    purpose}]}.
    """
    m = RECORD_URL_RE.search(record_id_or_url)
    record_id = m.group(1) if m else re.sub(r"\D", "", record_id_or_url)
    if not record_id:
        raise ValueError(f"Could not extract a record id from '{record_id_or_url}'.")
    url = f"{BASE_URL}/DRJ/{record_id}"
    # The "cimkes" (labeled/full) view exposes more fields than the brief
    # default view (publication, classification, notes, ISBN, publisher).
    page = await _get(f"{url}/cimkes")
    fields = _parse_record_fields(page)
    if not fields:
        raise ValueError(f"No record found for id '{record_id}'.")
    return {
        "record_id": record_id,
        "url": url,
        "title": _parse_title_line(page),
        "permalink_urn": _parse_permalink(page),
        "fields": fields,
        "files": _parse_record_files(page),
    }


@mcp.tool()
async def oszkdk_top_list(
    period: Annotated[Literal["month", "year", "alltime"], Field(
        description=(
            "month = this month's most-read titles (A hónap legjobbjai), "
            "year = this year's (Az év legjobbjai), alltime = all-time "
            "(Valaha volt legjobbak)."
        )
    )] = "month",
) -> dict[str, Any]:
    """Most-read / most-downloaded OSZKDK titles for the given period.
    Useful for 'what's popular' style questions rather than topical search.

    Returns: {period, items: [{rank, record_id, url, label, count}]}.
    """
    page = await _get(TOPS_URL)
    section_titles = {
        "month": "A hónap legjobbjai",
        "year": "Az év legjobbjai",
        "alltime": "Valaha volt legjobbak",
    }
    target = section_titles[period]
    i = page.find(f"<b>{target}</b>")
    if i < 0:
        return {"period": period, "items": []}
    hr_i = page.find("<hr/>", i)
    if hr_i < 0:
        return {"period": period, "items": []}
    next_b = page.find("<b>", hr_i)
    segment = page[hr_i:next_b] if next_b > hr_i else page[hr_i : hr_i + 3000]
    items = []
    for m in re.finditer(
        r"(\d+),\s*<a href='(/DRJ/\d+)'>(.*?)\s*\((\d+)\)</a>", segment
    ):
        rank, href, label, count = m.groups()
        items.append(
            {
                "rank": int(rank),
                "record_id": _record_id_from_href(href),
                "url": BASE_URL + href,
                "label": _clean(label),
                "count": int(count),
            }
        )
    return {"period": period, "items": items}


# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
