"""HTTP contracts without application lifespan or external service calls."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi.testclient import TestClient
import pytest

from apps.backend import api
from packages.core.services import export_service


@pytest.mark.asyncio
async def test_api_restart_does_not_duplicate_daily_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setattr(api.logger, "_LOGS_DIR", tmp_path)
    monkeypatch.setenv("TRADELENS_RELEASE_SHA", "b5262e2")
    root = logging.getLogger()
    previous_level = root.level
    try:
        for _ in range(2):
            await api.startup_event()
            handler = api.app.state.stdlib_log_handler
            try:
                logging.getLogger("audit-lifespan").warning("daily audit diagnostic")
            finally:
                await api.shutdown_event()
            assert handler not in root.handlers
    finally:
        root.setLevel(previous_level)

    records = [
        json.loads(line)
        for path in tmp_path.glob("*.log")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert sum(record["msg"] == "daily audit diagnostic" for record in records) == 2
    assert [record["release_sha"] for record in records if record["msg"] == "startup"] == ["b5262e2"] * 2


@pytest.fixture
def offline_client(monkeypatch):
    async def forbidden_startup():
        raise AssertionError("These HTTP contracts must not start the application")

    monkeypatch.setattr(api.app.router, "on_startup", [forbidden_startup])
    # No context manager: Starlette's lifespan (MT5/model/DB startup) never runs.
    client = TestClient(api.app)
    yield client
    client.close()


@pytest.mark.parametrize("path,method,key", [
    ("/api/trigger-checks/XAUUSD", "get_trigger_checks_by_asset", "checks"),
    ("/api/gpt-call-summaries/XAUUSD", "get_gpt_call_summaries_by_asset", "summaries"),
])
def test_asset_routes_preserve_pagination_defaults(offline_client, monkeypatch, path, method, key):
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(api.gpt_repo, method, fetch)
    response = offline_client.get(path)
    assert response.status_code == 200
    assert response.json() == {key: [], "count": 0}
    fetch.assert_awaited_once_with(asset="XAUUSD", limit=50, skip=0, start_time=None, end_time=None)


@pytest.mark.parametrize("path,method,key", [
    ("/api/trigger-checks/XAUUSD", "get_trigger_checks_by_asset", "checks"),
    ("/api/gpt-call-summaries/XAUUSD", "get_gpt_call_summaries_by_asset", "summaries"),
])
def test_asset_routes_preserve_filters_and_response(offline_client, monkeypatch, path, method, key):
    timestamp = datetime(2026, 7, 1, 10, tzinfo=timezone.utc)
    row = {"_id": "sample", "timestamp": timestamp}
    if key == "checks":
        row.update(_id=ObjectId("000000000000000000000001"), trigger_time=timestamp)
    fetch = AsyncMock(return_value=[deepcopy(row)])
    monkeypatch.setattr(api.gpt_repo, method, fetch)
    response = offline_client.get(path, params={
        "limit": 7, "skip": 2, "start_time": "2026-07-01T10:00:00Z", "end_time": "2026-07-02T10:00:00+00:00",
    })
    assert response.status_code == 200
    expected = {"_id": str(row["_id"]), "timestamp": timestamp.isoformat()}
    if key == "checks":
        expected["trigger_time"] = timestamp.isoformat()
    assert response.json() == {key: [expected], "count": 1}
    fetch.assert_awaited_once_with(asset="XAUUSD", limit=7, skip=2,
                                  start_time=timestamp, end_time=datetime(2026, 7, 2, 10, tzinfo=timezone.utc))


@pytest.mark.parametrize("path,method,prefix", [
    ("/api/trigger-checks/XAUUSD", "get_trigger_checks_by_asset", "Error fetching trigger checks"),
    ("/api/gpt-call-summaries/XAUUSD", "get_gpt_call_summaries_by_asset", "Error fetching GPT call summaries"),
])
def test_asset_routes_preserve_repository_failure(offline_client, monkeypatch, path, method, prefix):
    monkeypatch.setattr(api.gpt_repo, method, AsyncMock(side_effect=RuntimeError("offline fixture failure")))
    response = offline_client.get(path)
    assert response.status_code == 500
    assert response.json() == {"detail": f"{prefix}: offline fixture failure"}


@pytest.mark.parametrize("path,method", [
    ("/api/trigger-checks/XAUUSD", "get_trigger_checks_by_asset"),
    ("/api/gpt-call-summaries/XAUUSD", "get_gpt_call_summaries_by_asset"),
])
def test_asset_routes_preserve_query_validation(offline_client, monkeypatch, path, method):
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(api.gpt_repo, method, fetch)
    for params in ({"limit": 0}, {"limit": 1001}, {"skip": -1}):
        assert offline_client.get(path, params=params).status_code == 422
    # Existing string-date parsing happens in the handler, so this stays a 500
    # during route cleanup rather than silently adopting the dead copy's 422.
    assert offline_client.get(path, params={"start_time": "invalid-date"}).status_code == 500
    fetch.assert_not_awaited()


@pytest.mark.parametrize("path,method,key", [
    ("/api/trigger-checks", "get_all_trigger_checks", "checks"),
    ("/api/gpt-call-summaries", "get_all_gpt_call_summaries", "summaries"),
])
def test_collection_routes_remain_accessible(offline_client, monkeypatch, path, method, key):
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(api.gpt_repo, method, fetch)
    response = offline_client.get(path, params={"asset": "ALL"})
    assert response.status_code == 200
    assert response.json() == {key: [], "count": 0}
    fetch.assert_awaited_once_with(limit=50, skip=0, start_time=None, end_time=None)


def test_export_list_preserves_served_shape(offline_client, monkeypatch):
    def forbidden_directory_read():
        raise AssertionError("Current served route does not read export_dir")

    service = SimpleNamespace(list_exports=lambda: [{"filename": "sample.csv"}], get_export_dir=forbidden_directory_read)
    monkeypatch.setattr(export_service, "get_export_service", lambda: service)
    response = offline_client.get("/api/export/list")
    assert response.status_code == 200
    assert response.json() == {"exports": [{"filename": "sample.csv"}], "count": 1}


def test_export_list_preserves_failure(offline_client, monkeypatch):
    def failed_service():
        raise RuntimeError("offline fixture failure")

    monkeypatch.setattr(export_service, "get_export_service", failed_service)
    response = offline_client.get("/api/export/list")
    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to list exports: offline fixture failure"}


def test_each_reporting_route_has_one_registration_and_matching_schema():
    api.app.openapi_schema = None
    schema = api.app.openapi()
    for path in ("/api/trigger-checks/{asset}", "/api/gpt-call-summaries/{asset}", "/api/export/list"):
        routes = [route for route in api.app.routes if getattr(route, "path", None) == path and "GET" in getattr(route, "methods", ())]
        assert len(routes) == 1
        operation = schema["paths"][path]["get"]
        assert operation["operationId"] == routes[0].unique_id
        if path != "/api/export/list":
            parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
            assert parameters["limit"]["schema"]["default"] == 50
            assert parameters["start_time"]["schema"]["anyOf"][0] == {"type": "string"}
