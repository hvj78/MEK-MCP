"""Integration tests: call the tool functions directly against the live
MEK and OSZKDK services."""
import asyncio, json, sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import mek_mcp_server as S
import oszkdk_mcp_server as O

def show(name, res, keys=("total", "accent_fallback_used")):
    info = {k: res[k] for k in keys if k in res}
    print(f"\n=== {name} === {info}")
    for h in res.get("hits", res.get("entries", res.get("items", [])))[:3]:
        print("   ", json.dumps(h, ensure_ascii=False)[:160])

async def main():
    # ============================== MEK ==================================
    # 1) Egyszerű keresés
    r = await S.mek_simple_search(subject="néprajz", limit=10)
    show("MEK simple: néprajz", r)
    assert r["total"] and r["hits"], "simple search failed"

    # 2) Összetett: AI témájú magyar művek, programozás kizárva
    conds = [
        S.SearchCondition(field="subject", value="mesterséges intelligencia"),
        S.SearchCondition(field="language", value="magyar", operator="and"),
        S.SearchCondition(field="subject", value="programozás", operator="not"),
    ]
    r = await S.mek_advanced_search(conditions=conds)
    show("MEK advanced: AI magyar NOT programozás", r)

    # 3) Orwell szerzőként vs. témaként
    r1 = await S.mek_advanced_search(conditions=[S.SearchCondition(field="author", value="Orwell*")])
    r2 = await S.mek_advanced_search(conditions=[S.SearchCondition(field="subject", value="Orwell*")])
    show("MEK advanced: Orwell mint szerző", r1)
    show("MEK advanced: Orwell mint téma", r2)

    # 4) Ékezet-fallback: 'Petofi Sandor' ékezet nélkül
    r = await S.mek_advanced_search(conditions=[S.SearchCondition(field="author", value="Petofi Sandor")])
    show("MEK advanced: Petofi Sandor (ékezet-fallback)", r)
    assert r["accent_fallback_used"] and r["total"] > 0

    # 5) Duna, de nem útikönyv és nem szépirodalom (NOT lánc)
    conds = [
        S.SearchCondition(field="geographic_subject", value="Duna"),
        S.SearchCondition(field="document_type", value="útikönyv", operator="not"),
    ]
    r = await S.mek_advanced_search(conditions=conds)
    show("MEK advanced: Duna NOT útikönyv", r)

    # 6) Index-böngészés: néprajz tárgyszóalakok
    r = await S.mek_browse_index(field="subject", term="néprajz")
    show("MEK browse: subject/néprajz", r, keys=())
    assert any("néprajz" in e["display"] for e in r["entries"])

    # 7) Teljes szövegű keresés
    r = await S.mek_fulltext_search(query="gépi tanulás", limit=10)
    show("MEK fulltext: gépi tanulás", r)

    # 8) Rekord metaadat
    r = await S.mek_get_record(mek_id_or_url="9439")
    print("\n=== MEK get_record 9439 ===")
    print(json.dumps(r, ensure_ascii=False, indent=1)[:600])
    assert r["title"] and r["subjects"]

    # 9) Lapozás az összetett keresőben
    r = await S.mek_advanced_search(conditions=[S.SearchCondition(field="subject", value="magyar irodalom")], offset=100)
    show("MEK advanced offset=100: magyar irodalom", r)
    assert r["offset"] == 100 and r["hits"]

    # ============================== OSZKDK ================================
    OC = O.SearchCondition

    # 10) Egyszerű keresés
    r = await O.oszkdk_simple_search(query="Petőfi")
    show("OSZKDK simple: Petőfi", r)
    assert r["total"] and r["hits"]

    # 11) Lapozás
    r = await O.oszkdk_simple_search(query="Petőfi", offset=10)
    show("OSZKDK simple offset=10", r)
    assert r["offset"] == 10 and r["hits"]

    # 12) Összetett: szerző Petőfi, NEM Ibolyák című
    r = await O.oszkdk_advanced_search(conditions=[
        OC(field="author", value="Petőfi Sándor"),
        OC(field="title", value="Ibolyák", match="exact_phrase", operator="not"),
    ])
    show("OSZKDK advanced: Petőfi szerző NOT Ibolyák cím", r)
    assert r["total"] > 0

    # 13) document_type szűrés (images -> gyakorlatilag üres gyűjtemény)
    r = await O.oszkdk_advanced_search(
        conditions=[OC(field="author", value="Petőfi Sándor")], document_type="images")
    show("OSZKDK advanced: Petőfi + document_type=images", r)
    assert r["total"] == 0

    # 14) Rekord metaadat + letölthető fájlok
    r = await O.oszkdk_get_record(record_id_or_url="632")
    print("\n=== OSZKDK get_record 632 ===")
    print(json.dumps(r, ensure_ascii=False, indent=1)[:700])
    assert r["permalink_urn"] and r["files"]

    # 15) Többszerzős rekord (nincs "Szerző" mező, csak "Név/nevek")
    r = await O.oszkdk_get_record(record_id_or_url="https://oszkdk.oszk.hu/DRJ/28621")
    show("OSZKDK get_record 28621 (többszerzős)", {"fields": [], "total": len(r["fields"])})
    assert "Név/nevek" in r["fields"]

    # 16) Korlátozott hozzáférésű rekord
    r = await O.oszkdk_get_record(record_id_or_url="3")
    assert r["files"][0]["access"] == "Dedikált hálózaton belül"

    # 17) Top lista mindhárom időszakra
    for period in ("month", "year", "alltime"):
        r = await O.oszkdk_top_list(period=period)
        show(f"OSZKDK top_list: {period}", r)
        assert len(r["items"]) == 10

    # 18) Érvénytelen rekord-ID hibakezelése
    try:
        await O.oszkdk_get_record(record_id_or_url="999999999")
        raise AssertionError("expected ValueError for invalid record id")
    except ValueError:
        pass

    print("\nALL TESTS PASSED (MEK + OSZKDK)")

asyncio.run(main())
