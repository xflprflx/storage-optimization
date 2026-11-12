"""Ajuda nos campos: todo campo de toda tela tem explicação e desenho no catálogo (services/field_help.py)."""
import collections
import html
import io
import re
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

from sqlalchemy import select

from models import (BayStructure, BinType, Branch, BranchLayout, LocationBin, PickHistory, Product, ShelfLevel,
                    SiteProject, StockItem, StorageLocation, StructureType, Warehouse)
from services.field_help import RULES, find

ROOT = Path(__file__).resolve().parent.parent


class _Fields(HTMLParser):
    """Campos de uma página, com a mesma chave do field_help.js (keyOf): name, #id, #check-all ou #row-check."""

    def __init__(self):
        super().__init__()
        self.fields, self.links = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and (a.get("href") or "").startswith("/"):
            self.links.append(a["href"])
        if tag not in ("input", "select", "textarea") or a.get("type") in ("hidden", "submit", "button"):
            return
        if a.get("name"):
            key = a["name"]
        elif a.get("id"):
            key = "#" + a["id"]
        elif "data-check-all" in a:
            key = "#check-all"
        elif a.get("type") == "checkbox":
            key = "#row-check"
        else:
            key = ""
        self.fields.append(key)


def _seed(db) -> dict:
    wh = Warehouse(code="HW", name="Armazém da ajuda")
    br = Branch(code="HB", name="Filial da ajuda")
    br.warehouses.append(wh)
    db.add_all([br, wh])
    db.flush()
    st = StructureType(code="H-EST", name="Estante", kind="Estante", width=Decimal(100), depth=Decimal(60), height=Decimal(200),
                       levels=4, level_height=Decimal(40), golden_from=Decimal(60), golden_to=Decimal(140))
    bt = BinType(code="H-CX", name="Caixa", width=Decimal(30), height=Decimal(20), length=Decimal(40))
    pr = Product(sku="H-SKU1", name="Produto da ajuda", height=Decimal(5), width=Decimal(5), length=Decimal(5))
    db.add_all([st, bt, pr])
    db.flush()
    locs = []
    for aisle in ("A", "B"):
        for bay in ("01", "02"):
            db.add(BayStructure(branch_id=br.id, warehouse_id=wh.id, aisle=aisle, bay=bay, structure_type_id=st.id))
            for lv in ("1", "2"):
                db.add(ShelfLevel(branch_id=br.id, warehouse_id=wh.id, aisle=aisle, bay=bay, level=lv, notes=""))
                loc = StorageLocation(branch_id=br.id, warehouse_id=wh.id, aisle=aisle, bay=bay, level=lv, position="1",
                                      compartment="", address=f"{aisle}-{bay}-{lv}-1")
                db.add(loc)
                locs.append(loc)
    db.flush()
    for loc in locs:
        db.add(LocationBin(location_id=loc.id, bin_type_id=bt.id, quantity=2))
    db.add(StockItem(product_id=pr.id, location_id=locs[0].id, quantity=Decimal(10)))
    from services.branch_layout import auto_layout, clean, scope_bays

    db.flush()
    db.add(BranchLayout(branch_id=br.id, warehouse_id=wh.id, data=clean(auto_layout(scope_bays(db, br.id, wh.id)))))
    db.add(PickHistory(branch_id=br.id, filename="h.xlsx", data={
        "kind": "orders", "orders": 1, "lines": 1, "skus": {"H-SKU1": [1, 2.0]}, "pairs": [],
        "sample": [["P1", ["H-SKU1"], [2.0]]]}))
    prj = SiteProject(name="Projeto da ajuda", config={})
    db.add(prj)
    db.commit()
    return {"b": br.id, "w": wh.id, "st": st.id, "bt": bt.id, "p": pr.id, "loc": locs[0].id, "prj": prj.id}


def _pages(client, ids) -> dict[str, str]:
    """HTML das telas: as de formulário, as listas (seguindo os links) e as que só aparecem depois de um envio."""
    b, w = ids["b"], ids["w"]
    start = ["/", "/branches/new", f"/branches/{b}/edit", "/warehouses/new", f"/warehouses/{w}/edit", "/locations/new",
             f"/locations/{ids['loc']}/edit", "/locations/batch", "/bin-types/new", f"/bin-types/{ids['bt']}/edit",
             "/structures/new", f"/structures/{ids['st']}/edit", "/products/new", f"/products/{ids['p']}/edit",
             f"/products/{ids['p']}", "/settings", "/stock", "/stock/new", "/imports", "/imports/history",
             f"/levels?branch_id={b}&warehouse_id={w}", f"/layout?branch_id={b}&warehouse_id={w}",
             f"/layout/plan?branch_id={b}&warehouse_id={w}", f"/layout/plan/3d?branch_id={b}&warehouse_id={w}",
             f"/optimization?branch_id={b}&warehouse_id={w}&run=1&slot=1", f"/optimization/pool?branch_id={b}&warehouse_id={w}&run=1&slot=1",
             f"/optimization/slotting/routes?branch_id={b}&warehouse_id={w}", f"/optimization/slotting/skus?branch_id={b}",
             "/optimization/site", f"/optimization/site/{ids['prj']}", f"/optimization/site/{ids['prj']}/3d",
             "/optimization/runs"]
    skip = re.compile(r"\.(xlsx|pdf|csv|json)|/delete|/export|template|seed\.xlsx|examples/|run=1|^/lang/|^/units/")
    out: dict[str, str] = {}
    queue = collections.deque(start)
    while queue and len(out) < 140:
        url = queue.popleft()
        if url in out:
            continue
        r = client.get(url)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            continue
        out[url] = r.text
        p = _Fields()
        p.feed(r.text)
        for h in p.links:
            h = html.unescape(h)
            if h not in out and not skip.search(h):
                queue.append(h)
    # telas que vêm de um envio: mapeamento de colunas das importações, do histórico e a confirmação da exclusão
    csv = "SKU;Descricao;Filial;Armazem;Posicao;Qtd;Corredor;Estante;Nivel;Codigo;Nome\nX1;x;HB;HW;A-01-1-1;1;A;01;1;C1;n\n"
    for kind in ("products", "stock", "branches", "warehouses", "bin_types", "locations", "levels"):
        r = client.post(f"/imports/{kind}/upload", data={"has_header": "1"},
                        files={"file": (f"{kind}.csv", io.BytesIO(csv.encode()), "text/csv")})
        assert r.status_code == 200, kind
        out[f"/imports/{kind}/upload"] = r.text
    hist = "Pedido;SKU;Quantidade\nP1;H-SKU1;2\nP2;H-SKU1;1\n"
    r = client.post("/imports/history/upload", data={"branch_id": str(b), "abs": "1", "back": "/imports/history"},
                    files={"file": ("h.csv", io.BytesIO(hist.encode()), "text/csv")})
    assert r.status_code == 200
    out["/imports/history/upload"] = r.text
    r = client.post("/bulk-delete/branches/preview", data={"ids": [str(b)], "back": "/branches"})
    assert r.status_code == 200
    out["/bulk-delete/branches/preview"] = r.text
    return out


def test_every_field_has_help(client, db):
    pages = _pages(client, _seed(db))
    assert len(pages) > 40
    missing, total = set(), 0
    for url, body in pages.items():
        path = re.sub(r"\?.*", "", url)
        p = _Fields()
        p.feed(body)
        for key in p.fields:
            total += 1
            if not key or find(path, key) is None:
                missing.add((path, key or "(campo sem name nem id)"))
    assert total > 400
    assert not missing, "campos sem ajuda:\n" + "\n".join(f"  {p}  {k}" for p, k in sorted(missing))


def test_dynamic_fields_have_help():
    """Campos criados pelo JavaScript (não aparecem no HTML do servidor)."""
    for path, key in [("/optimization/site/1/upload", "sheet_skus"), ("/optimization/site/1/upload", "col_skus_sku"),
                      ("/optimization/site/1/upload", "orders_abs"), ("/imports/history/upload", "col_order"),
                      ("/layout/plan/3d", "#nkA-A"), ("/optimization/site/1/3d", "#nkA-B"),
                      ("/optimization/site/1", "z_name_0"), ("/optimization/site/1", "o_x_m_2"),
                      ("/optimization/site/1", "sec_area_m2_1"), ("/locations/batch", "exc_cond_bay_3"),
                      ("/products/new", "pol_min_2"), ("/optimization/site/1", "nkdims1"),
                      ("/optimization/site/1", "nkdims2p"), ("/optimization/site/1", "nk-tool")]:
        assert find(path, key) is not None, (path, key)


def test_help_rules_are_valid():
    js = (ROOT / "static" / "field_help.js").read_text(encoding="utf-8")
    diagrams = set(re.findall(r"^\s*D\.(\w+) = ", js, flags=re.M))
    assert len(diagrams) >= 15
    for r in RULES:
        re.compile(r["p"])
        re.compile(r["k"])
        assert r["t"].strip() and len(r["d"]) >= 20, r["k"]
        name, _, hi = r["g"].partition(":")
        assert name in diagrams, (r["k"], r["g"])
        if hi:  # o destaque pedido existe no desenho
            body = js[js.index(f"D.{name} = "):]
            body = body[:body.index("\n  };") if "\n  };" in body else len(body)]
            assert f"'{hi}'" in body or f"{hi}:" in body, (r["k"], r["g"])
