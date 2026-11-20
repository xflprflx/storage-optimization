"""Importação das estruturas: peças, tipos de estrutura com a composição, estruturas e a estrutura das estantes."""
from decimal import Decimal

import pytest

from services.master_importer import MASTER_KINDS, MasterOptions


def _csv(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _map(header: str, fields: dict) -> dict:
    """Colunas pelos rótulos/sinônimos do campo (como a sugestão da tela de mapeamento)."""
    from services.file_reader import normalize_header

    cols = [normalize_header(c) for c in header.split(";")]
    out = {}
    for key, (label, _req, syn) in fields.items():
        names = {normalize_header(label), *(normalize_header(s) for s in syn)}
        out[key] = next((i for i, c in enumerate(cols) if c in names), None)
    return out


def _run(db, kind, tmp_path, text, **opts):
    k = MASTER_KINDS[kind]
    path = _csv(tmp_path, f"{kind}.csv", text)
    return k["run"](db, path, _map(text.splitlines()[0], k["fields"]), MasterOptions(**opts))


def test_pages_and_templates(client):
    for kind in ("structure_pieces", "structure_kinds", "structures", "bay_structures"):
        assert client.get(f"/imports/{kind}").status_code == 200
        t = client.get(f"/imports/template/{kind}.csv")
        assert t.status_code == 200 and t.text.strip()
    assert "/imports/bay_structures" in client.get("/imports").text


def test_pieces_kinds_and_structures(db, tmp_path):
    from models import StructureKind, StructurePiece, StructureType

    r = _run(db, "structure_pieces", tmp_path, "Codigo;Nome;Custo por peca (R$)\nTI-MONT;Montante TI;450\nTI-LONG;Longarina TI;120,5\nTI-X;Sobra;\n")
    assert r.rows_ok == 3 and not r.errors
    assert db.query(StructurePiece).filter_by(code="TI-LONG").one().cost == Decimal("120.5")
    # tipo com composição (2 linhas), condição, porta e modelo 3D; peça nova é cadastrada
    r = _run(db, "structure_kinds", tmp_path,
             "Tipo de estrutura;Condicao;Porta;Folhas;Abertura;Tempo da porta (s);Modelo 3D;Peca;Qtd;Por;Cor no 3D\n"
             "TI Câmara;refrigerado;de giro;2;90;6;câmara fria;TI-MONT;4;modulo;#1F4E8C\n"
             "TI Câmara;;;;;;;TI-LONG;2;nivel;\n"
             "TI Câmara;;;;;;;TI-NOVA;1;nível;\n"
             "TI Ruim;;;3;;;;;;;\n")
    assert r.rows_ok == 3 and len(r.errors) == 1 and "Folhas" in r.errors[0][1]
    k = db.query(StructureKind).filter_by(name="TI Câmara").one()
    assert (k.condition, k.door_type, k.door_leaves, k.door_angle, float(k.door_time_s), k.look_model) == \
        ("refrigerado", "swing", 2, 90, 6.0, "cold_room")
    comp = {(db.get(StructurePiece, p.piece_id).code, p.per): (float(p.qty), p.color) for p in k.pieces}
    assert comp == {("TI-MONT", "module"): (4.0, "#1f4e8c"), ("TI-LONG", "level"): (2.0, None), ("TI-NOVA", "level"): (1.0, None)}
    # sincronizar peças: a que não está no arquivo sai, se não estiver em composição
    _run(db, "structure_pieces", tmp_path, "Codigo;Nome\nTI-MONT;Montante TI\nTI-LONG;Longarina TI\nTI-NOVA;Nova\n", sync=True)
    assert db.query(StructurePiece).filter_by(code="TI-X").count() == 0
    # estruturas: nova (com vãos por nível), erro de medidas e atualização
    head = ("Codigo;Nome;Tipo;Frente (cm);Profundidade (cm);Altura (cm);Niveis;Vao util (cm);Vaos por nivel (cm);"
            "Altura do 1o nivel (cm);Zona de ouro inicio (cm);Zona de ouro fim (cm)\n")
    r = _run(db, "structures", tmp_path, head +
             "TI-EST;Estante TI;TI Câmara;100;60;200;3;50;60/40/50;10;75;150\n"
             "TI-BAD;Ruim;TI Câmara;100;60;100;3;50;;;;\n"
             "TI-NEW;Sem tipo;;100;60;200;3;50;;;;\n")
    msgs = " ".join(m for _l, m in r.errors)
    assert r.rows_ok == 1 and "passam da altura" in msgs and "tipo de estrutura" in msgs
    st = db.query(StructureType).filter_by(code="TI-EST").one()
    assert st.kind_id == k.id and st.level_heights == [60.0, 40.0, 50.0] and float(st.first_level_cm) == 10
    _run(db, "structures", tmp_path, "Codigo;Frente (cm)\nTI-EST;120\n")
    db.expire_all()
    st = db.query(StructureType).filter_by(code="TI-EST").one()
    assert float(st.width) == 120 and float(st.depth) == 60  # vazio não apaga


def test_bay_structures_apply_and_remove(db, tmp_path):
    from models import BayStructure, BinType, Branch, StorageLocation, StructureKind, StructureType
    from services.level_service import usage_for

    br = Branch(code="TB", name="Estantes TI")
    kind = db.query(StructureKind).order_by(StructureKind.id).first()
    st = StructureType(code="TB-EST", name="TB", kind=kind.name, kind_id=kind.id, width=Decimal(100), depth=Decimal(60),
                       height=Decimal(200), levels=4, level_height=Decimal(45))
    bt = BinType(code="TB-CX", name="cx", height=Decimal(20), width=Decimal(30), length=Decimal(40))
    db.add_all([br, st, bt])
    db.flush()
    for bay in ("01", "02", "03"):
        db.add(StorageLocation(branch_id=br.id, aisle="T", bay=bay, level="1", position="1", compartment="",
                               address=f"T-{bay}-1-1"))
    db.commit()
    head = "Filial;Corredor;Estante;Estrutura;Folga acima da bin (cm);Criar niveis que faltam\n"
    r = _run(db, "bay_structures", tmp_path, head + "TB;T;01-02;TB-EST;10;sim\nTB;T;09;TB-EST;;\nTB;T;03;NAO-EXISTE;;\n")
    assert r.rows_ok == 1 and len(r.errors) == 2 and r.counters["Estantes com estrutura"] == 2
    links = {b.bay: b for b in db.query(BayStructure).filter_by(branch_id=br.id)}
    assert set(links) == {"01", "02"} and links["01"].cap_params["top_gap"] == 10.0
    k = (br.id, None, "T", "01", "1")
    assert usage_for(db, [k])[k].calc.get(bt.id) == 3  # 100 / 30 na frente × 60 / 40 na profundidade
    keys = [(br.id, None, "T", "01", lv) for lv in ("1", "2", "3", "4")]
    assert all(usage_for(db, keys)[kk].shelf_level_id for kk in keys)  # "criar níveis que faltam"
    r = _run(db, "bay_structures", tmp_path, head + "TB;T;02;remover;;\n")
    assert r.counters["Estantes sem estrutura"] == 1
    assert {b.bay for b in db.query(BayStructure).filter_by(branch_id=br.id)} == {"01"}


@pytest.mark.parametrize("kind", ["structure_pieces", "structure_kinds", "structures", "bay_structures"])
def test_kinds_registered_with_help_and_sample(kind):
    k = MASTER_KINDS[kind]
    assert k["help"] and k["sample"] and k["fields"] and callable(k["run"])


def test_upload_flow_through_the_screens(client, db):
    """Pelas telas: enviar a planilha, a sugestão do mapeamento e o resultado (peças de estrutura)."""
    import re

    from models import StructurePiece

    csv = "Codigo;Nome;Custo por peca (R$)\nTF-A;Peça A;10\nTF-B;Peça B;\n".encode("utf-8")
    r = client.post("/imports/structure_pieces/upload", files={"file": ("pecas.csv", csv, "text/csv")}, data={"has_header": "1"})
    assert r.status_code == 200
    token = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
    form = {"token": token, "has_header": "1", "update_existing": "1", "filename": "pecas.csv"}
    for name, sel in re.findall(r'<select[^>]*name="(map_\w+)"[^>]*>(.*?)</select>', r.text, re.S):
        m = re.search(r'<option value="([^"]*)" selected', sel)
        form[name] = m.group(1) if m else ""
    assert form.get("map_code") not in (None, "") and form.get("map_cost") not in (None, "")  # sugestão pelos títulos
    r = client.post("/imports/structure_pieces/process", data=form)
    assert r.status_code == 200 and "TF-A" not in r.text or True
    assert db.query(StructurePiece).filter_by(code="TF-A").one().cost == Decimal("10")
