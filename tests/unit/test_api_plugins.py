from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.health import router


def test_plugins_endpoint_returns_legacy_lists_and_details():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/plugins")

    assert response.status_code == 200
    data = response.json()

    assert isinstance(data["sources"], list)
    assert isinstance(data["sinks"], list)
    assert isinstance(data["serializers"], list)
    assert isinstance(data["transforms"], list)

    assert "details" in data
    assert isinstance(data["details"]["sources"], list)
    assert isinstance(data["details"]["sinks"], list)
    assert isinstance(data["details"]["serializers"], list)
    assert isinstance(data["details"]["transforms"], list)

    sftp_source = next(item for item in data["details"]["sources"] if item["name"] == "sftp")
    assert "remote_path" in sftp_source["required_fields"]
    assert "password" in sftp_source["common_optional_fields"]
    assert any(field["name"] == "password" for field in sftp_source["fields"])
    assert sftp_source["summary"]

    json_serializer = next(item for item in data["details"]["serializers"] if item["name"] == "json")
    assert json_serializer["summary"]
    assert json_serializer["class_name"] == "JsonSerializer"
    assert any(field["name"] == "ensure_ascii" for field in json_serializer["fields"])
