import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DATABASE_PATH", os.path.join(tempfile.mkdtemp(), "import.db"))
os.environ["DEV_MODE"] = "1"


@pytest.fixture
def client(tmp_path):
    import app as app_module
    app_module.DEV_MODE = True
    application = app_module.create_app(str(tmp_path / "test.db"))
    application.config["TESTING"] = True
    return application.test_client()


@pytest.fixture
def user(client):
    r = client.post("/api/signup", json={"email": "a@example.com", "password": "password123"})
    assert r.status_code == 200
    return r.get_json()["user"]
