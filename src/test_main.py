from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)

def test_version():
    response = client.get("/version")
    assert response.status_code == 200
    assert "version" in response.json()
    assert "environment" in response.json()
