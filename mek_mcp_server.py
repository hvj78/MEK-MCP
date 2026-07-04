#!/usr/bin/env python3
"""
MEK MCP Server
==============

MCP (Model Context Protocol) server that exposes the search interfaces of the
Hungarian Electronic Library (Magyar Elektronikus Könyvtár, MEK,
https://mek.oszk.hu) to agentic tools such as Claude Code, Codex, etc.

Exposed tools
-------------
- mek_simple_search    : simple search (title / subject / author / MEK ID)
- mek_advanced_search  : advanced catalogue search, up to 5 conditions with
                         AND / OR / NOT operators over 24 metadata fields
- mek_fulltext_search  : free-text search in the full text of the documents
- mek_browse_index     : browse the controlled-vocabulary index of any field
                         (e.g. list subject-heading variants around a term)
- mek_get_record       : fetch metadata of a single MEK record

Transport: stdio (run the file directly, or register it in Claude Code with
`claude mcp add`). See README.md for details.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Annotated, Any, Literal, Optional

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://mek.oszk.hu"
SIMPLE_SEARCH_URL = f"{BASE_URL}/hu/search/elfull/"
FULLTEXT_SEARCH_URL = f"{BASE_URL}/hu/search/elfulltext/"
ADVANCED_SEARCH_URL = f"{BASE_URL}/katalog/kataluj.php3"
BROWSE_INDEX_URL = f"{BASE_URL}/katalog/browsuj.php3"

USER_AGENT = "MEK-MCP/1.0 (+https://mek.oszk.hu; MCP search client)"
TIMEOUT = httpx.Timeout(60.0, connect=15.0)

# The legacy /katalog/ CGI endpoints expect ISO-8859-2 (Latin-2) encoded
# form data; the modern /hu/search/ endpoints expect UTF-8.
LEGACY_ENCODING = "iso-8859-2"

# Friendly field name -> internal field identifier of the advanced search.
# The Hungarian label (as shown on the MEK website) is kept as a comment.
ADVANCED_FIELDS: dict[str, str] = {
    "main_title": "dc_title main",                    # Főcím
    "subtitle": "dc_title subtitle",                  # Alcím
    "collection_title": "dc_title PartOf",            # Összefoglaló cím
    "part_title": "dc_title parts",                   # Részcím
    "parallel_title": "dc_title alternative",         # Párhuzamos cím
    "original_title": "dc_title original",            # Eredeti cím
    "series": "dc_title series",                      # Sorozat
    "author": "dc_creator_o FamilyGivenName",         # Szerző
    "author_role": "dc_creator_o role",               # Szerzői minőség
    "corporate_author": "CorporateAuthor Cauth_name", # Testületi szerző
    "contributor": "dc_contributor_o FamilyGivenName",# Közreműködő
    "contributor_role": "dc_contributor_o role",      # Közreműködői minőség
    "publisher": "dc_publisher pub_name",             # Digitális kiadó
    "subject": "dc_subject keyword",                  # Tárgyszó
    "geographic_subject": "dc_subject geographic",    # Földrajzi tárgyszó
    "period_subject": "dc_subject period",            # Időszak tárgyszó
    "document_type": "dc_type dc_type",               # Dokumentumtípus
    "format": "dc_format format_name",                # Formátum
    "language": "dc_language m_lang",                 # Nyelv
    "original_language": "dc_language original",      # Eredeti nyelv
    "printed_source": "PrintedSource PrintedSource",  # Eredeti kiadvány
    "rights_owner": "dc_rights owner",                # Copyright tulajdonos
    "rights_note": "dc_rights other",                 # Jogi megjegyzés
    "creative_commons": "dc_rights dc_cc",            # Creative Commons
}

FIELD_NAMES = sorted(ADVANCED_FIELDS)

BROAD_TOPICS: dict[str, str] = {
    "all": "",
    "science_math": "természettudományok és matematika",
    "technology_economy": "műszaki tudományok, gazdasági ágazatok",
    "social_sciences": "társadalomtudományok",
    "humanities_literature": "humán területek, kultúra, irodalom",
    "reference_other": "kézikönyvek és egyéb műfajok",
}

MEK_URL_RE = re.compile(r"https?://mek\.oszk\.hu/(\d{5})/(\d{5})")
HIT_COUNT_RE = re.compile(r"tal[áa]latok\s+sz[áa]ma:?\s*(\d+)", re.IGNORECASE)


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


async def _post_form(url: str, params: dict[str, str], encoding: str) -> str:
    """POST an application/x-www-form-urlencoded body in the given encoding
    and decode the response with the same (or declared) encoding."""
    from urllib.parse import urlencode

    body = urlencode(params, encoding=encoding, errors="replace")
    client = _get_client()
    resp = await client.post(
        url,
        content=body.encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()
    if encoding == LEGACY_ENCODING:
        return resp.content.decode(LEGACY_ENCODING, errors="replace")
    return resp.text


async def _get(url: str) -> str:
    client = _get_client()
    resp = await client.get(url)
    resp.raise_for_status()
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        return resp.content.decode(LEGACY_ENCODING, errors="replace")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _extract_total(page_html: str) -> Optional[int]:
    m = HIT_COUNT_RE.search(page_html)
    return int(m.group(1)) if m else None


def _mek_id_from_url(url: str) -> Optional[str]:
    m = MEK_URL_RE.search(url)
    return str(int(m.group(2))) if m else None


def _record_url_from_id(mek_id: str) -> str:
    """MEK record URL layout: /{ddddd rounded down to hundreds}/{ddddd}.
    E.g. id 9439 -> https://mek.oszk.hu/09400/09439"""
    num = int(re.sub(r"\D", "", mek_id))
    return f"{BASE_URL}/{num // 100 * 100:05d}/{num:05d}"


def _parse_modern_hits(page_html: str) -> list[dict[str, Any]]:
    """Parse hits of the modern /hu/search/ pages (simple search)."""
    soup = BeautifulSoup(page_html, "html.parser")
    hits: list[dict[str, Any]] = []
    for hit in soup.select("div.hit"):
        link = hit.select_one("a.itemlink[href]")
        if not link:
            continue
        url = link["href"]
        author_el = hit.select_one("div.dcauthor")
        title_el = hit.select_one("div.dctitle")
        date_el = hit.select_one("div.rkdate")
        hits.append(
            {
                "mek_id": _mek_id_from_url(url),
                "url": url,
                "authors": _clean(author_el.get_text()) if author_el else "",
                "title": _clean(title_el.get_text()) if title_el else "",
                "date_added": _clean(date_el.get_text()) if date_el else "",
            }
        )
    return hits


def _parse_fulltext_hits(page_html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(page_html, "html.parser")
    hits: list[dict[str, Any]] = []
    for item in soup.select("a.etitem[href]"):
        url = item["href"]
        author_el = item.select_one("div.dcauthor")
        title_el = item.select_one("div.dctitle")
        snippet_el = item.select_one("div.foundtext")
        found = item.find_next_sibling("a", class_="mekfound")
        hits.append(
            {
                "mek_id": _mek_id_from_url(url),
                "url": url,
                "authors": _clean(author_el.get_text()) if author_el else "",
                "title": _clean(title_el.get_text()) if title_el else "",
                "snippet": _clean(snippet_el.get_text()) if snippet_el else "",
                "match_location_url": (BASE_URL + found["href"])
                if found and found.get("href", "").startswith("/")
                else (found["href"] if found else None),
            }
        )
    return hits


def _parse_legacy_hits(page_html: str) -> list[dict[str, Any]]:
    """Parse hits of the legacy /katalog/kataluj.php3 result page.

    Hit shape (malformed HTML, so we use regexes on the raw markup):
        <div class="hit "><form ... >
          <b><a ...>Author1, Author2:&nbsp;Title</b></a>
          <span class=allis><a ...>https://mek.oszk.hu/06100/06126</a></span>
          ... <span class=allis>2008-07-08</span></div></form>
    """
    hits: list[dict[str, Any]] = []
    blocks = re.split(r'<div class="hit\s*"', page_html)[1:]
    for block in blocks:
        url_m = MEK_URL_RE.search(block)
        if not url_m:
            continue
        url = url_m.group(0)
        label_m = re.search(r"<b>\s*<a[^>]*>(.*?)</b>", block, re.S)
        label = _clean(re.sub(r"<[^>]+>", " ", label_m.group(1))) if label_m else ""
        # Author and title are separated by ":&nbsp;" (non-breaking space).
        authors, title = "", label
        parts = re.split(r":\s*\xa0|:\xa0", html_lib.unescape(label_m.group(1)) if label_m else "")
        if len(parts) >= 2:
            authors = _clean(parts[0])
            title = _clean(":".join(parts[1:]))
        date_m = re.findall(r"\d{4}-\d{2}-\d{2}", block)
        hits.append(
            {
                "mek_id": _mek_id_from_url(url),
                "url": url,
                "authors": authors,
                "title": title,
                "date_added": date_m[-1] if date_m else "",
            }
        )
    return hits


def _page_size_normalize(limit: int) -> int:
    """The modern search endpoints only accept 10 / 50 / 100 items per page."""
    if limit <= 10:
        return 10
    if limit <= 50:
        return 50
    return 100


# ---------------------------------------------------------------------------
# Advanced search request builder
# ---------------------------------------------------------------------------

class SearchCondition(BaseModel):
    """One condition of the advanced search."""

    field: str = Field(
        description="Field to search in. One of: " + ", ".join(FIELD_NAMES)
    )
    value: str = Field(
        description=(
            "Search value. Use Hungarian terms as stored in the MEK catalogue "
            "(e.g. subject='mesterséges intelligencia', language='magyar', "
            "document_type='regény'). A trailing * truncates (prefix search), "
            "e.g. 'Petőfi*'."
        )
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
    conditions: list[SearchCondition],
    accent_insensitive: bool,
    offset: int,
) -> dict[str, str]:
    params: dict[str, str] = {"szerint": "szerzosz", "mod": "keres"}
    # The CGI expects exactly 5 field/value slots and 4 operators.
    defaults = ["dc_title main"] * 5
    for i in range(5):
        if i < len(conditions):
            cond = conditions[i]
            params[f"s{i + 1}"] = ADVANCED_FIELDS[cond.field]
            params[f"m{i + 1}"] = cond.value
        else:
            params[f"s{i + 1}"] = defaults[i]
            params[f"m{i + 1}"] = ""
    for i in range(4):
        # muvN links condition N with condition N+1.
        op = conditions[i + 1].operator if i + 1 < len(conditions) else "and"
        params[f"muv{i + 1}"] = op
    if accent_insensitive:
        params["ekezet"] = "ektelen"
    if offset:
        params["offset"] = str(offset)
    return params


# ---------------------------------------------------------------------------
# MCP server & tools
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "mek-search",
    instructions=(
        "Search tools for the Hungarian Electronic Library (Magyar "
        "Elektronikus Könyvtár, MEK, https://mek.oszk.hu), a free digital "
        "library of ~30 000 Hungarian and Hungary-related documents.\n\n"
        "Usage tips:\n"
        "- Catalogue values are Hungarian: search with Hungarian subject "
        "terms, language names ('magyar', 'angol', 'német'), and document "
        "types ('regény', 'útikönyv', 'tanulmány'). Translate the user's "
        "intent into Hungarian terms first.\n"
        "- Prefer mek_advanced_search for anything involving specific "
        "fields, multiple criteria, OR-logic or exclusions (NOT).\n"
        "- Subject headings are controlled vocabulary: if a subject search "
        "returns few/zero hits, call mek_browse_index first to discover the "
        "exact term variants (e.g. around 'néprajz'), then search on those.\n"
        "- Person names are stored as 'Family Given' (e.g. 'Petőfi Sándor'). "
        "To find works ABOUT a person, search them as subject; to find works "
        "BY them, search as author; contributor covers editors/translators.\n"
        "- Accents matter in the advanced search; set accent_insensitive=true "
        "or rely on the automatic fallback the tool performs on zero hits.\n"
        "- Use * for prefix truncation: 'Petőfi*' matches all name forms."
    ),
)


@mcp.tool()
async def mek_simple_search(
    title: Annotated[str, Field(description=(
        "Words from the title (main/sub/series title). All words must occur "
        "(AND). Truncation with *, accents optional. Empty = not filtered."
    ))] = "",
    subject: Annotated[str, Field(description=(
        "Words from the topic: subject headings, sub-collection or document "
        "type, in Hungarian (e.g. 'néprajz', 'történelem', 'regény')."
    ))] = "",
    author: Annotated[str, Field(description=(
        "Author / editor / translator name words, e.g. 'Petőfi Sándor' or "
        "'Orwell'."
    ))] = "",
    mek_id: Annotated[str, Field(description=(
        "Numeric MEK identifier of a specific document, e.g. '9439'."
    ))] = "",
    limit: Annotated[int, Field(ge=1, le=100, description=(
        "Max hits per page. Server supports 10, 50 or 100; the value is "
        "rounded up to the nearest supported size."
    ))] = 10,
    offset: Annotated[int, Field(ge=0, description=(
        "Result offset for paging (0-based, in items)."
    ))] = 0,
) -> dict[str, Any]:
    """Simple search in the MEK catalogue by title, subject, author and/or
    MEK ID. All given words are combined with AND. Words may be typed in
    lowercase and without accents: if nothing is found, the MEK server
    automatically retries accent-free and then stemmed.

    Good for quick, broad lookups. For field-precise queries, OR-logic,
    exclusions (NOT) or language/type filtering use mek_advanced_search.

    Returns: {total, offset, limit, hits: [{mek_id, url, authors, title,
    date_added}], has_more}.
    """
    size = _page_size_normalize(limit)
    params = {
        "dc_title": title,
        "dc_subject": subject,
        "dc_creator": author,
        "id": mek_id,
        "size": str(size),
        "sort": "",
        "from": str(offset) if offset else "",
    }
    page = await _post_form(SIMPLE_SEARCH_URL, params, encoding="utf-8")
    total = _extract_total(page)
    hits = _parse_modern_hits(page)
    return {
        "search_type": "simple",
        "query": {k: v for k, v in
                  {"title": title, "subject": subject, "author": author,
                   "mek_id": mek_id}.items() if v},
        "total": total if total is not None else len(hits),
        "offset": offset,
        "limit": size,
        "hits": hits,
        "has_more": total is not None and offset + len(hits) < total,
    }


@mcp.tool()
async def mek_advanced_search(
    conditions: Annotated[list[SearchCondition], Field(description=(
        "1 to 5 search conditions. Each condition has a field, a value and "
        "an operator ('and'/'or'/'not') that links it to the previous "
        "condition. Example (Hungarian-language AI works excluding "
        "programming textbooks):\n"
        "[{'field':'subject','value':'mesterséges intelligencia'},\n"
        " {'field':'language','value':'magyar','operator':'and'},\n"
        " {'field':'subject','value':'programozás','operator':'not'}]"
    ))],
    accent_insensitive: Annotated[bool, Field(description=(
        "If true, accented and unaccented letter forms are treated as equal "
        "(MEK 'ékezet nélküli keresés'). Useful when the exact accented "
        "form of a name/term is unknown."
    ))] = False,
    offset: Annotated[int, Field(ge=0, description=(
        "Result offset for paging. The server returns up to 100 hits per "
        "page; pass 100, 200, ... for further pages."
    ))] = 0,
    auto_accent_fallback: Annotated[bool, Field(description=(
        "If true (default) and the search yields 0 hits, the search is "
        "automatically retried with accent-insensitive matching; the "
        "response then contains accent_fallback_used=true so you can tell "
        "the user the hit set was widened this way."
    ))] = True,
) -> dict[str, Any]:
    """Advanced (fielded) search in the MEK catalogue with up to 5
    conditions combined via AND / OR / NOT over 24 metadata fields.

    Available fields: main_title, subtitle, collection_title, part_title,
    parallel_title, original_title, series, author, author_role,
    corporate_author, contributor, contributor_role, publisher, subject,
    geographic_subject, period_subject, document_type, format, language,
    original_language, printed_source, rights_owner, rights_note,
    creative_commons.

    Typical patterns:
    - Works BY a person: field=author, value='Petőfi Sándor'.
    - Works ABOUT a person: field=subject, value='Petőfi Sándor'.
    - Person in any role: run separate author / subject / contributor
      searches (OR across different fields of different records is best
      done client-side by merging results).
    - Exclusions: add a condition with operator='not'
      (e.g. document_type='útikönyv' with 'not' to drop travel guides).
    - Language filter: field=language, value='magyar' / 'angol' / ...
    - Values are matched against controlled vocabulary; use
      mek_browse_index to discover exact term forms, and * for prefixes.

    Returns: {total, offset, hits: [{mek_id, url, authors, title,
    date_added}], has_more, accent_fallback_used}.
    """
    if not conditions:
        raise ValueError("At least one search condition is required.")
    if len(conditions) > 5:
        raise ValueError("At most 5 search conditions are supported.")
    for cond in conditions:
        if cond.field not in ADVANCED_FIELDS:
            raise ValueError(
                f"Unknown field '{cond.field}'. Valid fields: "
                + ", ".join(FIELD_NAMES)
            )

    async def run(accents_off: bool) -> tuple[Optional[int], list[dict[str, Any]]]:
        params = _build_advanced_params(conditions, accents_off, offset)
        page = await _post_form(ADVANCED_SEARCH_URL, params, LEGACY_ENCODING)
        return _extract_total(page), _parse_legacy_hits(page)

    total, hits = await run(accent_insensitive)
    fallback_used = False
    if (
        auto_accent_fallback
        and not accent_insensitive
        and (total or 0) == 0
    ):
        total2, hits2 = await run(True)
        if (total2 or 0) > 0:
            total, hits = total2, hits2
            fallback_used = True

    return {
        "search_type": "advanced",
        "conditions": [c.model_dump() for c in conditions],
        "accent_insensitive": accent_insensitive or fallback_used,
        "accent_fallback_used": fallback_used,
        "total": total if total is not None else len(hits),
        "offset": offset,
        "hits": hits,
        "has_more": total is not None and offset + len(hits) < total,
    }


@mcp.tool()
async def mek_fulltext_search(
    query: Annotated[str, Field(description=(
        "Free-text query searched in the FULL TEXT of the documents (not "
        "just metadata). Use Hungarian words for Hungarian documents, e.g. "
        "'mesterséges intelligencia'."
    ))],
    broad_topic: Annotated[
        Literal["all", "science_math", "technology_economy",
                "social_sciences", "humanities_literature", "reference_other"],
        Field(description=(
            "Restrict to a broad MEK collection: all (default), "
            "science_math (természettudományok és matematika), "
            "technology_economy (műszaki tudományok, gazdasági ágazatok), "
            "social_sciences (társadalomtudományok), humanities_literature "
            "(humán területek, kultúra, irodalom), reference_other "
            "(kézikönyvek és egyéb műfajok)."
        )),
    ] = "all",
    limit: Annotated[int, Field(ge=1, le=100, description=(
        "Max hits per page (10 / 50 / 100)."
    ))] = 10,
    offset: Annotated[int, Field(ge=0, description="Paging offset.")] = 0,
) -> dict[str, Any]:
    """Free-text search in the full text of MEK documents. Returns matching
    documents with a text snippet around the match and a direct link to the
    match location. Use this when the query concerns document CONTENT
    rather than catalogue metadata, or as a fallback when metadata searches
    find nothing.

    Returns: {total, offset, limit, hits: [{mek_id, url, authors, title,
    snippet, match_location_url}], has_more}.
    """
    size = _page_size_normalize(limit)
    params = {
        "body": query,
        "broadtopic": BROAD_TOPICS[broad_topic],
        "size": str(size),
        "sort": "",
        "from": str(offset) if offset else "",
    }
    page = await _post_form(FULLTEXT_SEARCH_URL, params, encoding="utf-8")
    total = _extract_total(page)
    hits = _parse_fulltext_hits(page)
    return {
        "search_type": "fulltext",
        "query": query,
        "broad_topic": broad_topic,
        "total": total if total is not None else len(hits),
        "offset": offset,
        "limit": size,
        "hits": hits,
        "has_more": total is not None and offset + len(hits) < total,
    }


@mcp.tool()
async def mek_browse_index(
    field: Annotated[str, Field(description=(
        "Field whose controlled-vocabulary index to browse. One of: "
        + ", ".join(FIELD_NAMES)
        + ". Most useful: subject, geographic_subject, document_type, "
        "author, language."
    ))],
    term: Annotated[str, Field(description=(
        "Term to position the index at; the browser returns the vocabulary "
        "entries around/containing it (e.g. 'néprajz' lists 'magyar "
        "néprajz', 'tárgyi néprajz', 'vallási néprajz', ...)."
    ))],
) -> dict[str, Any]:
    """Browse the controlled-vocabulary index of a catalogue field around a
    given term. Use this BEFORE subject/type/name searches to discover the
    exact term forms stored in the catalogue, then run mek_advanced_search
    with the returned search_value strings.

    Returns: {field, term, entries: [{display, search_value}]}. Pass
    search_value (not display) as the value in mek_advanced_search.
    """
    if field not in ADVANCED_FIELDS:
        raise ValueError(
            f"Unknown field '{field}'. Valid fields: " + ", ".join(FIELD_NAMES)
        )
    internal = ADVANCED_FIELDS[field]
    params = {
        "s1": internal, "m1": term, "muv1": "and",
        "s2": "dc_subject keyword", "m2": "", "muv2": "and",
        "s3": "dc_subject geographic", "m3": "", "muv3": "and",
        "s4": "dc_type dc_type", "m4": "", "muv4": "and",
        "s5": "dc_language m_lang", "m5": "",
        "szerint": "szerzosz",
    }
    from urllib.parse import quote

    qs = (
        f"tablefield={quote(internal)}&par=0&indindex=0"
        "&muv1index=0&muv2index=0&muv3index=0&muv4index=0&figyel=MCP"
    )
    page = await _post_form(f"{BROWSE_INDEX_URL}?{qs}", params, LEGACY_ENCODING)
    entries = [
        {"display": _clean(m.group(2)), "search_value": _clean(m.group(1))}
        for m in re.finditer(
            r"<option[^>]*value='([^']*)'[^>]*>([^<]+)</option>", page
        )
        if _clean(m.group(1)) and _clean(m.group(2)).upper() != "ÜRES LISTA"
    ]
    result: dict[str, Any] = {"field": field, "term": term, "entries": entries}
    if not entries:
        result["note"] = (
            "No index entries found around this term; it likely does not "
            "occur in this field's vocabulary. Try a shorter prefix, an "
            "accented/unaccented variant, or a different field."
        )
    return result


@mcp.tool()
async def mek_get_record(
    mek_id_or_url: Annotated[str, Field(description=(
        "MEK identifier (e.g. '9439') or record URL "
        "(e.g. 'https://mek.oszk.hu/09400/09439')."
    ))],
) -> dict[str, Any]:
    """Fetch the metadata of a single MEK record: title, authors, themes
    (topic hierarchy), subject headings, description, dates, identifiers.
    Use it to inspect / classify individual hits (e.g. to decide whether a
    work is fiction, history or ethnography, or whether it is BY or ABOUT a
    person).

    Returns: {mek_id, url, title, themes, subjects, description,
    date_added, urn}.
    """
    m = MEK_URL_RE.search(mek_id_or_url)
    url = m.group(0) if m else _record_url_from_id(mek_id_or_url)
    page = await _get(url)
    soup = BeautifulSoup(page, "html.parser")

    def metas(name: str) -> list[str]:
        return [
            _clean(t.get("content", ""))
            for t in soup.find_all("meta", attrs={"name": name})
            if _clean(t.get("content", ""))
        ]

    title = (metas("dc.title") or [""])[0]
    creators = metas("dc.creator")
    subjects = metas("dc.subject")
    identifiers = metas("dc.identifier")
    dates = [d for d in metas("dc.date") if re.match(r"\d{4}-\d{2}-\d{2}", d)]
    urn = next((i for i in identifiers if i.startswith("urn:")), None)

    # Theme hierarchy (topic / subtopic) is only in the visible page body,
    # marked up as <div class="tosk"><div class="topic">..</div>
    # <div class="subtopic">..</div>..</div>. A record may have several rows.
    themes: list[str] = []
    for row in soup.select("div.tosk"):
        topic = row.select_one("div.topic")
        subtopic = row.select_one("div.subtopic")
        path = " / ".join(
            _clean(el.get_text()) for el in (topic, subtopic) if el
        )
        if path:
            themes.append(path)
    # Keyword links on the page complement the dc.subject metas.
    for kw in soup.select("div.keywords a"):
        val = _clean(kw.get_text())
        if val and val not in subjects:
            subjects.append(val)

    desc_el = soup.select_one(".dcdescription, .description, blockquote")
    description = _clean(desc_el.get_text()) if desc_el else None
    if not description:
        dm = re.search(r"[\u201e\"]([^\u201d\"]{40,600})[\u201d\"]", soup.get_text())
        description = _clean(dm.group(1)) if dm else None

    return {
        "mek_id": _mek_id_from_url(url),
        "url": url,
        "title": title,
        "creators": creators,
        "themes": themes,
        "subjects": subjects,
        "description": description,
        "date_added": dates[0] if dates else None,
        "urn": urn,
    }


# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
