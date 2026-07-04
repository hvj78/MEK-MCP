"""Integration tests: call the tool functions directly against the live MEK."""
import asyncio, json, sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import mek_mcp_server as S

def show(name, res, keys=("total", "accent_fallback_used")):
    info = {k: res[k] for k in keys if k in res}
    print(f"\n=== {name} === {info}")
    for h in res.get("hits", res.get("entries", []))[:3]:
        print("   ", json.dumps(h, ensure_ascii=False)[:160])

async def main():
    # 1) Egyszerű keresés
    r = await S.mek_simple_search(subject="néprajz", limit=10)
    show("simple: néprajz", r)
    assert r["total"] and r["hits"], "simple search failed"

    # 2) Összetett: AI témájú magyar művek, programozás kizárva
    conds = [
        S.SearchCondition(field="subject", value="mesterséges intelligencia"),
        S.SearchCondition(field="language", value="magyar", operator="and"),
        S.SearchCondition(field="subject", value="programozás", operator="not"),
    ]
    r = await S.mek_advanced_search(conditions=conds)
    show("advanced: AI magyar NOT programozás", r)

    # 3) Orwell szerzőként vs. témaként
    r1 = await S.mek_advanced_search(conditions=[S.SearchCondition(field="author", value="Orwell*")])
    r2 = await S.mek_advanced_search(conditions=[S.SearchCondition(field="subject", value="Orwell*")])
    show("advanced: Orwell mint szerző", r1)
    show("advanced: Orwell mint téma", r2)

    # 4) Ékezet-fallback: 'Petofi Sandor' ékezet nélkül
    r = await S.mek_advanced_search(conditions=[S.SearchCondition(field="author", value="Petofi Sandor")])
    show("advanced: Petofi Sandor (ékezet-fallback)", r)
    assert r["accent_fallback_used"] and r["total"] > 0

    # 5) Duna, de nem útikönyv és nem szépirodalom (NOT lánc)
    conds = [
        S.SearchCondition(field="geographic_subject", value="Duna"),
        S.SearchCondition(field="document_type", value="útikönyv", operator="not"),
    ]
    r = await S.mek_advanced_search(conditions=conds)
    show("advanced: Duna NOT útikönyv", r)

    # 6) Index-böngészés: néprajz tárgyszóalakok
    r = await S.mek_browse_index(field="subject", term="néprajz")
    show("browse: subject/néprajz", r, keys=())
    assert any("néprajz" in e["display"] for e in r["entries"])

    # 7) Teljes szövegű keresés
    r = await S.mek_fulltext_search(query="gépi tanulás", limit=10)
    show("fulltext: gépi tanulás", r)

    # 8) Rekord metaadat
    r = await S.mek_get_record(mek_id_or_url="9439")
    print("\n=== get_record 9439 ===")
    print(json.dumps(r, ensure_ascii=False, indent=1)[:600])
    assert r["title"] and r["subjects"]

    # 9) Lapozás az összetett keresőben
    r = await S.mek_advanced_search(conditions=[S.SearchCondition(field="subject", value="magyar irodalom")], offset=100)
    show("advanced offset=100: magyar irodalom", r)
    assert r["offset"] == 100 and r["hits"]

    print("\nALL TESTS PASSED")

asyncio.run(main())
