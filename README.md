# MEK-MCP

MCP server and FastAPI microservice exposing the search interfaces of two
Hungarian digital libraries — the **Hungarian Electronic Library** (Magyar
Elektronikus Könyvtár, [mek.oszk.hu](https://mek.oszk.hu)) and the
**OSZK Digitális Könyvtár** ([oszkdk.oszk.hu](https://oszkdk.oszk.hu)) — to
agentic tools (Claude Code, Claude Desktop, Codex and any MCP-capable
client) and to plain REST consumers. Documentation below is in Hungarian.

---

A projekt **két üzemmódban** használható, két önálló keresőmotorral
(`mek_mcp_server.py` a MEK-hez, `oszkdk_mcp_server.py` az OSZKDK-hoz):

1. **Lokális MCP szerver (stdio)** — a két könyvtár egymástól függetlenül
   is bekötető Claude Code-ba vagy Claude Desktopba.
2. **Hostolt microservice (FastAPI)** — REST API `/v1/*` és `/v1/oszkdk/*`
   végpontokkal **és** egyetlen közös távoli MCP végponttal a `/mcp`
   útvonalon, amely mind a kilenc toolt kínálja (öt MEK + négy OSZKDK);
   Fly.io-ra deployolható ebből a repóból, GitHub Actions-szel
   automatikusan.

Miért két könyvtár? A MEK és az OSZKDK **csak részben fedik egymást**: a
MEK inkább klasszikus/régebbi magyar irodalmat és szürke irodalmat
gyűjt, az OSZKDK viszont ISBN-es, modern könyvekre és monográfiákra
súlyoz. Ha az egyikben nincs találat egy modern, ISBN-es magyar könyvre,
érdemes a másikban is megnézni — ezért érdemes mindkét toolkészletet
egyszerre elérhetővé tenni egy agent számára.

## Toolok / végpontok

### MEK (Magyar Elektronikus Könyvtár)

| MCP tool | REST végpont | Mire jó |
|---|---|---|
| `mek_simple_search` | `GET /v1/search/simple` | Gyors keresés cím / téma / szerző / MEK ID szerint (ÉS-kapcsolat) |
| `mek_advanced_search` | `POST /v1/search/advanced` | Max. 5 feltétel **és / vagy / nem** operátorokkal, 24 mező |
| `mek_fulltext_search` | `GET /v1/search/fulltext` | Szabad szavas keresés a dokumentumok teljes szövegében |
| `mek_browse_index` | `GET /v1/browse` | Kontrollált szótár (tárgyszó-, névalakok) böngészése |
| `mek_get_record` | `GET /v1/records/{id}` | Egy rekord teljes metaadata |

### OSZKDK (OSZK Digitális Könyvtár)

| MCP tool | REST végpont | Mire jó |
|---|---|---|
| `oszkdk_simple_search` | `GET /v1/oszkdk/search/simple` | Gyors, szabad szavas keresés az összes indexelt mezőben |
| `oszkdk_advanced_search` | `POST /v1/oszkdk/search/advanced` | Max. 3 feltétel **és / vagy / nem** operátorokkal, cím / szerző / bármely mező |
| `oszkdk_get_record` | `GET /v1/oszkdk/records/{id}` | Rekord metaadata + letölthető fájlok listája (formátum, méret, hozzáférés) |
| `oszkdk_top_list` | `GET /v1/oszkdk/top` | Legolvasottabb címek (hónap / év / minden idők) |

Interaktív API-dokumentáció futó szolgáltatásnál: `/docs`.

## 1) Lokális MCP szerver (stdio)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# csak a MEK
claude mcp add --transport stdio mek -- \
  $PWD/.venv/bin/python $PWD/mek_mcp_server.py

# csak az OSZKDK
claude mcp add --transport stdio oszkdk -- \
  $PWD/.venv/bin/python $PWD/oszkdk_mcp_server.py
```

Ellenőrzés: `claude mcp list` → a szerverek ✓ Connected állapotban.

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
claude mcp add --transport http mek-oszkdk https://mek-search-api.fly.dev/mcp
# API-kulccsal:
claude mcp add --transport http mek-oszkdk https://mek-search-api.fly.dev/mcp \
  --header "X-API-Key: valami-titok"
```

Egyetlen URL mögött mind a kilenc tool elérhető (`mek_*` és `oszkdk_*`
előtaggal, névütközés nélkül), így lokális telepítés nélkül, bármely
gépről (vagy claude.ai custom connectorként) használható mindkét
könyvtár.

## Példa promptok az agentnek

**MEK:**
- „Keress magyar nyelvű műveket a mesterséges intelligencia témájában, de
  zárd ki a programozási tankönyveket." → `subject=mesterséges
  intelligencia` AND `language=magyar` NOT `subject=programozás`.
- „Petőfi szerzőként, témaként és közreműködőként." → három keresés az
  `author` / `subject` / `contributor` mezőkre (35 / 50 / 4 találat) — a
  `subject`-es halmaz a róla szóló (szekunder) irodalom.
- „Nézd meg, milyen tárgyszóalakok vannak a néprajz körül, és ezekre
  keress." → `mek_browse_index(subject, néprajz)` → célzott keresések.
- Ékezetkezelés: 0 találatnál automatikus ékezetfüggetlen újrapróbálás,
  a válaszban `accent_fallback_used=true` jelzi a bővülést.

**OSZKDK:**
- „Keress Petőfitől szerzőként műveket, de zárd ki az Ibolyák címűt." →
  `author=Petőfi Sándor` NOT `title=Ibolyák (exact_phrase)`.
- „Mi a legnépszerűbb könyv az OSZK digitális könyvtárban idén?" →
  `oszkdk_top_list(period=year)`.
- „Ez a könyv szabadon olvasható, vagy csak a könyvtárban?" →
  `oszkdk_get_record` → `files[].access` (`Nyilvános` vs. `Dedikált
  hálózaton belül` = csak OSZK-pontokon).
- Ha a MEK-ben nincs találat egy modern, ISBN-es könyvre, próbáld az
  OSZKDK-ban (és fordítva) — a két gyűjtemény kiegészíti egymást.

## Implementációs jegyzetek

**MEK:**
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

**OSZKDK:**
- Az összes végpont sima UTF-8-at használ, nincs szükség speciális
  kódolás-kezelésre (szemben a MEK legacy `/katalog` végpontjával).
- A találati oldalak fix, 10-es lapmérettel dolgoznak; nincs
  lapméret-paraméter, csak `offset` (0-alapú).
- Az összetett keresőnek pontosan 3 sora van (ennyit enged a saját UI is);
  csak 3 mező érhető el ténylegesen: cím (`dc.title`), szerző
  (`dc.author`), bármely mező (`cql.serverChoice`) — más `dc.*` nevek
  (pl. `dc.subject`) csendben 0 találatot adnak, mert a backend nem
  támogatja őket, hiába tűnne logikusnak.
- A dokumentumtípus-szűrő (`document_type`) csak globálisan, az ELSŐ
  feltételről érvényesül — ez a hivatalos UI valódi korlátja, nem a
  kliens hibája.
- `any_word` és `all_words` egyezési mód a jelenlegi backenden minden
  tesztelt esetben azonos találati halmazt adott; `exact_phrase` az
  egyetlen mód, ami megbízhatóan szűkít.
- Egyes rekordok csak „Dedikált hálózaton belül" (OSZK-pontokon)
  érhetők el, nem szabadon letölthetők — ezt a `files[].access` mező
  jelzi minden fájlnál.

**Közös / hosztolás:**
- A hostolt szolgáltatás stateless, nem igényel persistent volume-ot; a
  `fly.toml` `auto_stop_machines` beállításával üresjáratban leáll.
- A `/mcp` végpont a két modul tooljait egyetlen kombinált MCP szerverbe
  gyűjti (`combined_mcp` az `app.py`-ban); stdio módban viszont a két
  modul továbbra is teljesen önállóan futtatható.

## Tesztelés

Élő integrációs tesztek mindkét könyvtár ellen (keresők, NOT-operátor,
ékezet-fallback, index, rekord, lapozás, top-lista, hibakezelés):

```bash
.venv/bin/python test_live.py
```

