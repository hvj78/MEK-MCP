# MEK-MCP

MCP server and FastAPI microservice exposing the search interfaces of the
**Hungarian Electronic Library** (Magyar Elektronikus Könyvtár,
[mek.oszk.hu](https://mek.oszk.hu)) to agentic tools (Claude Code, Claude
Desktop, Codex and any MCP-capable client) and to plain REST consumers.
Documentation below is in Hungarian.

---

A projekt **két üzemmódban** használható, közös keresőmotorral
(`mek_mcp_server.py`):

1. **Lokális MCP szerver (stdio)** — közvetlenül beköthető Claude Code-ba
   vagy Claude Desktopba.
2. **Hostolt microservice (FastAPI)** — REST API `/v1/*` végpontokkal
   **és** távoli MCP végponttal a `/mcp` útvonalon; Fly.io-ra deployolható
   ebből a repóból, GitHub Actions-szel automatikusan.

## Toolok / végpontok

| MCP tool | REST végpont | Mire jó |
|---|---|---|
| `mek_simple_search` | `GET /v1/search/simple` | Gyors keresés cím / téma / szerző / MEK ID szerint (ÉS-kapcsolat) |
| `mek_advanced_search` | `POST /v1/search/advanced` | Max. 5 feltétel **és / vagy / nem** operátorokkal, 24 mező |
| `mek_fulltext_search` | `GET /v1/search/fulltext` | Szabad szavas keresés a dokumentumok teljes szövegében |
| `mek_browse_index` | `GET /v1/browse` | Kontrollált szótár (tárgyszó-, névalakok) böngészése |
| `mek_get_record` | `GET /v1/records/{id}` | Egy rekord teljes metaadata |

Interaktív API-dokumentáció futó szolgáltatásnál: `/docs`.

## 1) Lokális MCP szerver (stdio)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

claude mcp add --transport stdio mek -- \
  $PWD/.venv/bin/python $PWD/mek_mcp_server.py
```

Ellenőrzés: `claude mcp list` → a `mek` szerver ✓ Connected.

## 2) Hostolt szolgáltatás Fly.io-n, ebből a repóból

A repo tartalmazza a `Dockerfile`-t, a `fly.toml`-t és a
`.github/workflows/fly-deploy.yml` workflow-t: minden `main`-re történő
push automatikusan deployol.

Egyszeri beállítás:

```bash
# 1. App létrehozása a Fly-fiókban (a név globálisan egyedi kell legyen)
fly apps create mek-search-api

# 2. App-hatókörű deploy token generálása
fly tokens create deploy -a mek-search-api
```

A kapott tokent add hozzá a GitHub repóhoz secretként:
**Settings → Secrets and variables → Actions → New repository secret**,
név: `FLY_API_TOKEN`. Ezután egy push a `main`-re (vagy az Actions fülön a
„Fly Deploy" workflow kézi indítása) elvégzi a deployt.

Opcionális API-kulcs védelem:

```bash
fly secrets set MEK_API_KEY=valami-titok -a mek-search-api
```

Ha be van állítva, minden kérésnek `X-API-Key: <kulcs>` vagy
`Authorization: Bearer <kulcs>` fejlécet kell vinnie (kivéve `/`,
`/healthz`, `/docs`).

### Távoli MCP használat deploy után

```bash
claude mcp add --transport http mek https://mek-search-api.fly.dev/mcp
# API-kulccsal:
claude mcp add --transport http mek https://mek-search-api.fly.dev/mcp \
  --header "X-API-Key: valami-titok"
```

Így lokális telepítés nélkül, bármely gépről (vagy claude.ai custom
connectorként) használható a MEK-kereső.

## Példa promptok az agentnek

- „Keress magyar nyelvű műveket a mesterséges intelligencia témájában, de
  zárd ki a programozási tankönyveket." → `subject=mesterséges
  intelligencia` AND `language=magyar` NOT `subject=programozás`.
- „Petőfi szerzőként, témaként és közreműködőként." → három keresés az
  `author` / `subject` / `contributor` mezőkre (35 / 50 / 4 találat) — a
  `subject`-es halmaz a róla szóló (szekunder) irodalom.
- „Nézd meg, milyen tárgyszóalakok vannak a néprajz körül, és ezekre
  keress." → `mek_browse_index(subject, néprajz)` → célzott keresések.
- „Duna témájú művek, amelyek nem útikönyvek." → `geographic_subject=Duna`
  NOT `document_type=útikönyv`.
- Ékezetkezelés: 0 találatnál automatikus ékezetfüggetlen újrapróbálás,
  a válaszban `accent_fallback_used=true` jelzi a bővülést.

## Implementációs jegyzetek

- A modern `/hu/search/` végpontok UTF-8-at, a régi `/katalog/*.php3`
  CGI-k **ISO-8859-2** kódolású form-adatot várnak — a kliens ezt kezeli
  (e nélkül az ékezetes keresések némán 0 találatot adnak).
- Az összetett kereső oldalanként max. 100 találatot ad; lapozás
  `offset`-tel (100, 200, ...). Az egyszerű és teljes szövegű kereső
  10/50/100-as lapmérettel lapozható.
- Tárgyszavak, típusok, névalakok kontrollált szótárból jönnek; a
  `mek_browse_index` `search_value` mezője a kereshető alak.
- Névformátum: „Családnév Utónév" (`Petőfi Sándor`), külföldi szerzőknél
  gyakran `Vezetéknév, Utónév` (`Verne, Jules`). Csonkolás: `*`.
- A szolgáltatás stateless, nem igényel persistent volume-ot; a
  `fly.toml` `auto_stop_machines` beállításával üresjáratban leáll.

## Tesztelés

Élő integrációs tesztek a MEK ellen (keresők, NOT-operátor,
ékezet-fallback, index, rekord, lapozás):

```bash
.venv/bin/python test_live.py
```
