"""
MEK + OSZKDK Search API — FastAPI microservice around two Hungarian
digital-library MCP servers.

Exposes both the Hungarian Electronic Library (MEK, mek.oszk.hu) and the
OSZK Digitális Könyvtár (OSZKDK, oszkdk.oszk.hu) searches two ways:

1. REST API (this file):
     GET  /v1/search/simple            – MEK simple search
     POST /v1/search/advanced          – MEK fielded search, AND/OR/NOT, 24 fields
     GET  /v1/search/fulltext          – MEK free-text search in document bodies
     GET  /v1/browse                   – MEK controlled-vocabulary index browsing
     GET  /v1/records/{mek_id}         – MEK single record metadata
     GET  /v1/fields                   – MEK advanced-search field list
     GET  /v1/oszkdk/search/simple     – OSZKDK simple search
     POST /v1/oszkdk/search/advanced   – OSZKDK fielded search, AND/OR/NOT
     GET  /v1/oszkdk/records/{id}      – OSZKDK single record metadata + files
     GET  /v1/oszkdk/top               – OSZKDK most-read titles
     GET  /healthz                     – liveness probe
   Interactive docs at /docs (OpenAPI at /openapi.json).

2. Remote MCP endpoint (streamable HTTP) at /mcp — all nine tools from both
   libraries in one place (mek_* and oszkdk_* prefixes avoid name clashes),
   usable from Claude Code / claude.ai without any local install:
     claude mcp add --transport http mek-oszkdk https://<app>.fly.dev/mcp

   Each library's stdio server also still runs standalone if only one is
   wanted locally: `python mek_mcp_server.py` / `python oszkdk_mcp_server.py`.

Optional auth: set the MEK_API_KEY environment variable (Fly secret) and
every request except /, /healthz, /docs, /openapi.json must carry it in an
`X-API-Key: <key>` or `Authorization: Bearer <key>` header. If the variable
is unset, the service is open.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import mek_mcp_server as core
from mek_mcp_server import SearchCondition, FIELD_NAMES

import oszkdk_mcp_server as oszkdk_core
from oszkdk_mcp_server import SearchCondition as OszkdkSearchCondition

API_VERSION = "1.1.0"

# ---------------------------------------------------------------------------
# Remote MCP (streamable HTTP) served at /mcp — combines both libraries'
# tools into a single server so an agent only needs one connector.
# ---------------------------------------------------------------------------

combined_mcp = FastMCP(
    "mek-oszkdk-search",
    instructions=(
        "Search tools for two independent Hungarian digital libraries:\n"
        "- mek_* tools: Magyar Elektronikus Könyvtár (MEK, mek.oszk.hu) — "
        "older/classic Hungarian literature, textbooks and grey literature.\n"
        "- oszkdk_* tools: OSZK Digitális Könyvtár (OSZKDK, oszkdk.oszk.hu) — "
        "skews toward ISBN-registered modern books and monographs.\n"
        "The two collections only partially overlap: if one returns nothing "
        "useful for a modern/ISBN-bearing Hungarian book, try the other. "
        "See each tool's own description for field/search details."
    ),
)
for _name in (
    "mek_simple_search", "mek_advanced_search", "mek_fulltext_search",
    "mek_browse_index", "mek_get_record",
):
    combined_mcp.add_tool(getattr(core, _name))
for _name in (
    "oszkdk_simple_search", "oszkdk_advanced_search",
    "oszkdk_get_record", "oszkdk_top_list",
):
    combined_mcp.add_tool(getattr(oszkdk_core, _name))

# The MCP SDK's DNS-rebinding protection validates the Host header and is
# designed for LOCAL servers (it only allows localhost by default, so a
# hosted deployment would answer 421 Misdirected Request). For a public
# host either list the allowed hostnames in MEK_MCP_ALLOWED_HOSTS
# (comma-separated, e.g. "mek-mcp.fly.dev") or leave it unset to disable
# the check entirely — appropriate for a TLS-terminated public service.
_allowed_hosts = [
    h.strip()
    for h in os.environ.get("MEK_MCP_ALLOWED_HOSTS", "").split(",")
    if h.strip()
]
combined_mcp.settings.transport_security = (
    TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=_allowed_hosts
    )
    if _allowed_hosts
    else TransportSecuritySettings(enable_dns_rebinding_protection=False)
)

combined_mcp.settings.stateless_http = True  # no per-session state -> Fly-friendly
combined_mcp.settings.json_response = True
combined_mcp.settings.streamable_http_path = "/mcp"  # served directly, no redirect

mcp_asgi = combined_mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    async with combined_mcp.session_manager.run():
        yield


app = FastAPI(
    title="MEK + OSZKDK Search API",
    version=API_VERSION,
    description=(
        "REST + remote-MCP gateway to the search interfaces of two "
        "Hungarian digital libraries: the Hungarian Electronic Library "
        "(Magyar Elektronikus Könyvtár, https://mek.oszk.hu) and the "
        "OSZK Digitális Könyvtár (https://oszkdk.oszk.hu)."
    ),
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Optional API-key auth (set MEK_API_KEY as a Fly secret to enable)
# ---------------------------------------------------------------------------

_OPEN_PATHS = {"/", "/healthz", "/docs", "/openapi.json", "/redoc"}


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    required = os.environ.get("MEK_API_KEY")
    if required and request.url.path not in _OPEN_PATHS:
        supplied = request.headers.get("x-api-key")
        if not supplied:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
        if supplied != required:
            return JSONResponse(
                {"detail": "Invalid or missing API key."}, status_code=401
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    return {
        "service": "MEK + OSZKDK Search API",
        "version": API_VERSION,
        "docs": "/docs",
        "mcp_endpoint": "/mcp",
        "source_libraries": {
            "MEK": "https://mek.oszk.hu",
            "OSZKDK": "https://oszkdk.oszk.hu",
        },
    }


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/search/simple", summary="Simple search (title/subject/author/MEK ID)")
async def search_simple(
    title: str = Query("", description="Words from the title (AND, * truncation)"),
    subject: str = Query("", description="Subject / type words in Hungarian"),
    author: str = Query("", description="Author / editor / translator name words"),
    mek_id: str = Query("", description="Numeric MEK identifier"),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    if not any([title, subject, author, mek_id]):
        raise HTTPException(400, "Provide at least one of title/subject/author/mek_id.")
    return await core.mek_simple_search(
        title=title, subject=subject, author=author,
        mek_id=mek_id, limit=limit, offset=offset,
    )


class AdvancedSearchRequest(BaseModel):
    conditions: list[SearchCondition] = Field(
        min_length=1, max_length=5,
        description="1-5 conditions; each links to the previous with "
                    "operator and/or/not. Fields: " + ", ".join(FIELD_NAMES),
    )
    accent_insensitive: bool = False
    auto_accent_fallback: bool = True
    offset: int = Field(0, ge=0)


@app.post("/v1/search/advanced", summary="Advanced fielded search (AND/OR/NOT)")
async def search_advanced(req: AdvancedSearchRequest) -> dict[str, Any]:
    try:
        return await core.mek_advanced_search(
            conditions=req.conditions,
            accent_insensitive=req.accent_insensitive,
            auto_accent_fallback=req.auto_accent_fallback,
            offset=req.offset,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/v1/search/fulltext", summary="Free-text search in document bodies")
async def search_fulltext(
    q: str = Query(..., min_length=1, description="Free-text query (Hungarian)"),
    broad_topic: Literal[
        "all", "science_math", "technology_economy",
        "social_sciences", "humanities_literature", "reference_other",
    ] = "all",
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    return await core.mek_fulltext_search(
        query=q, broad_topic=broad_topic, limit=limit, offset=offset,
    )


@app.get("/v1/browse", summary="Browse a field's controlled-vocabulary index")
async def browse(
    field: str = Query(..., description="One of: " + ", ".join(FIELD_NAMES)),
    term: str = Query(..., min_length=1),
) -> dict[str, Any]:
    try:
        return await core.mek_browse_index(field=field, term=term)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/v1/records/{mek_id}", summary="Metadata of a single MEK record")
async def get_record(mek_id: str) -> dict[str, Any]:
    try:
        return await core.mek_get_record(mek_id_or_url=mek_id)
    except Exception as exc:  # noqa: BLE001 — upstream 404 etc.
        raise HTTPException(404, f"Record not found: {exc}") from exc


@app.get("/v1/fields", summary="List of searchable MEK advanced-search fields")
async def fields() -> dict[str, Any]:
    return {"fields": FIELD_NAMES}


# ---------------------------------------------------------------------------
# OSZKDK REST endpoints
# ---------------------------------------------------------------------------

@app.get("/v1/oszkdk/search/simple", summary="OSZKDK simple keyword search")
async def oszkdk_search_simple(
    q: str = Query(..., min_length=1, description="Free-text query (Hungarian)"),
    offset: int = Query(0, ge=0, description="0-based offset; page size fixed at 10"),
) -> dict[str, Any]:
    return await oszkdk_core.oszkdk_simple_search(query=q, offset=offset)


class OszkdkAdvancedSearchRequest(BaseModel):
    conditions: list[OszkdkSearchCondition] = Field(
        min_length=1, max_length=3,
        description="1-3 conditions; each links to the previous with "
                    "operator and/or/not. Fields: title, author, any_field.",
    )
    document_type: Literal["all", "books", "images"] = "all"
    offset: int = Field(0, ge=0)


@app.post("/v1/oszkdk/search/advanced", summary="OSZKDK advanced fielded search (AND/OR/NOT)")
async def oszkdk_search_advanced(req: OszkdkAdvancedSearchRequest) -> dict[str, Any]:
    try:
        return await oszkdk_core.oszkdk_advanced_search(
            conditions=req.conditions,
            document_type=req.document_type,
            offset=req.offset,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/v1/oszkdk/records/{record_id}", summary="Metadata of a single OSZKDK record")
async def oszkdk_get_record_endpoint(record_id: str) -> dict[str, Any]:
    try:
        return await oszkdk_core.oszkdk_get_record(record_id_or_url=record_id)
    except Exception as exc:  # noqa: BLE001 — upstream 404 etc.
        raise HTTPException(404, f"Record not found: {exc}") from exc


@app.get("/v1/oszkdk/top", summary="OSZKDK most-read titles (month/year/all-time)")
async def oszkdk_top(
    period: Literal["month", "year", "alltime"] = "month",
) -> dict[str, Any]:
    return await oszkdk_core.oszkdk_top_list(period=period)


# Mounted last so every FastAPI route above takes precedence; the MCP
# sub-app serves POST/GET /mcp directly (no trailing-slash redirect).
app.mount("/", mcp_asgi)
