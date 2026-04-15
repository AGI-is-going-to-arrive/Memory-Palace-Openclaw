import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import browse as browse_api
from api import review as review_api
from api.maintenance_common import require_maintenance_api_key


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(review_api.router)
    app.include_router(browse_api.router)
    return TestClient(app)


def test_review_requires_api_key_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "review-secret")
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)
    with _build_client() as client:
        response = client.get("/review/sessions")
    assert response.status_code == 401


def test_browse_write_requires_api_key_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "browse-secret")
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)
    with _build_client() as client:
        response = client.post(
            "/browse/node",
            json={
                "parent_path": "",
                "title": "test-node",
                "content": "test-content",
                "priority": 1,
                "domain": "core",
            },
        )
    assert response.status_code == 401


def test_browse_read_requires_api_key_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "browse-secret")
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)
    with _build_client() as client:
        response = client.get("/browse/node")
    assert response.status_code == 401


def test_review_rejects_invalid_session_id_with_api_key(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "review-secret")
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)
    headers = {"X-MCP-API-Key": "review-secret"}
    with _build_client() as client:
        response = client.delete("/review/sessions/%2E%2E", headers=headers)
    assert response.status_code == 400
    assert "Invalid session_id" in str(response.json().get("detail"))


@pytest.mark.asyncio
async def test_insecure_local_bypass_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When insecure local bypass activates, a warning must be logged."""
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.setenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", "true")

    from starlette.requests import Request

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/maintenance/health",
        "raw_path": b"/maintenance/health",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    request = Request(scope)

    with caplog.at_level(logging.WARNING, logger="api.maintenance_common"):
        await require_maintenance_api_key(request)

    assert any(
        "Insecure local auth bypass" in record.message
        for record in caplog.records
    ), f"Expected 'Insecure local auth bypass' warning, got: {[r.message for r in caplog.records]}"
