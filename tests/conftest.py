"""Testes de regressão: cada sessão de testes usa um banco SQLite próprio, em pasta temporária."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
_TMP = tempfile.mkdtemp(prefix="neokioski-tests-")
os.environ["NEOKIOSKI_DB_URL"] = f"sqlite:///{_TMP}/test.db"  # antes de importar a aplicação

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db(client):
    from database import SessionLocal

    with SessionLocal() as session:
        yield session


@pytest.fixture()
def settings(db):
    """Configurações de endereço; voltam ao padrão no fim do teste."""
    from services import app_settings

    yield app_settings
    app_settings.save(db, dict(app_settings.DEFAULTS))


@pytest.fixture()
def seeded(db):
    """Dados de exemplo das telas (filial HB com endereços, planta, histórico e projeto), criados uma vez por sessão."""
    from models import Branch, BinType, Product, SiteProject, StorageLocation, StructureType, Warehouse
    from tests.test_field_help import _seed

    br = db.query(Branch).filter_by(code="HB").first()
    if br is None:
        return _seed(db)
    return {"b": br.id, "w": db.query(Warehouse).filter_by(code="HW").one().id,
            "st": db.query(StructureType).filter_by(code="H-EST").one().id, "bt": db.query(BinType).filter_by(code="H-CX").one().id,
            "p": db.query(Product).filter_by(sku="H-SKU1").one().id,
            "loc": db.query(StorageLocation).filter_by(branch_id=br.id).order_by(StorageLocation.id).first().id,
            "prj": db.query(SiteProject).filter_by(name="Projeto da ajuda").one().id}
