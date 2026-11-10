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
