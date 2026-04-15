import importlib
import json

import pytest
from starlette.requests import Request
from starlette.responses import Response

import main as main_module


def _reload_main_module():
    return importlib.reload(main_module)


def _read_cors_kwargs(module):
    for middleware in module.app.user_middleware:
        if middleware.cls.__name__ == "CORSMiddleware":
            return dict(middleware.kwargs)
    raise AssertionError("CORS middleware not configured")


def _build_request(
    *,
    client_host: str = "127.0.0.1",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "headers": headers or [],
        "client": (client_host, 50000),
        "server": ("127.0.0.1", 8000),
    }
    return Request(scope)


def _coerce_health_payload(value):
    if isinstance(value, Response):
        return json.loads(value.body.decode("utf-8")), value.status_code
    return value, 200


def test_cors_defaults_use_restricted_local_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    monkeypatch.delenv("CORS_ALLOW_CREDENTIALS", raising=False)

    module = _reload_main_module()
    cors_kwargs = _read_cors_kwargs(module)

    assert cors_kwargs["allow_origins"] == list(module._DEFAULT_CORS_ALLOW_ORIGINS)
    assert cors_kwargs["allow_credentials"] is True


def test_cors_allows_credentials_with_explicit_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://a.example, https://b.example")
    monkeypatch.setenv("CORS_ALLOW_CREDENTIALS", "true")

    module = _reload_main_module()
    cors_kwargs = _read_cors_kwargs(module)

    assert cors_kwargs["allow_origins"] == ["https://a.example", "https://b.example"]
    assert cors_kwargs["allow_credentials"] is True


def test_cors_disables_credentials_for_explicit_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
    monkeypatch.setenv("CORS_ALLOW_CREDENTIALS", "true")

    module = _reload_main_module()
    cors_kwargs = _read_cors_kwargs(module)

    assert cors_kwargs["allow_origins"] == ["*"]
    assert cors_kwargs["allow_credentials"] is False


@pytest.mark.asyncio
async def test_health_hides_internal_exception_details(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _reload_main_module()

    def _raise_client_error():
        raise RuntimeError("secret_token_should_not_leak")

    monkeypatch.setattr(module, "get_sqlite_client", _raise_client_error)
    raw = await module.health(request=_build_request())
    payload, status_code = _coerce_health_payload(raw)

    assert status_code == 503
    assert payload["status"] == "degraded"
    assert payload["index"]["reason"] == "internal_error"
    assert payload["runtime"]["write_lanes"]["reason"] == "internal_error"
    assert "secret_token_should_not_leak" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_health_hides_runtime_details_for_untrusted_remote_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _reload_main_module()

    def _raise_client_error():
        raise RuntimeError("secret_token_should_not_leak")

    monkeypatch.setattr(module, "get_sqlite_client", _raise_client_error)
    raw = await module.health(request=_build_request(client_host="203.0.113.10"))
    payload, status_code = _coerce_health_payload(raw)

    assert status_code == 200
    assert payload["status"] == "ok"
    assert "index" not in payload
    assert "runtime" not in payload
    assert "secret_token_should_not_leak" not in json.dumps(payload)


def test_health_request_allows_details_for_valid_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _reload_main_module()
    monkeypatch.setenv("MCP_API_KEY", "health-secret")

    allowed = module._health_request_allows_details(
        _build_request(client_host="203.0.113.10"),
        x_mcp_api_key="health-secret",
    )

    assert allowed is True


def test_mount_embedded_sse_apps_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _reload_main_module()
    fake_stream = object()
    fake_message = object()
    monkeypatch.setattr(
        module,
        "create_embedded_sse_apps",
        lambda: (fake_stream, fake_message),
    )

    module._mount_embedded_sse_apps(module.app)
    first_count = len(module.app.routes)
    module._mount_embedded_sse_apps(module.app)

    assert getattr(module.app.state, "embedded_sse_mounted", False) is True
    assert len(module.app.routes) == first_count
    mounted_paths = [getattr(route, "path", None) for route in module.app.routes]
    assert "/sse" in mounted_paths
    assert "/messages" in mounted_paths
    assert "/sse/messages" in mounted_paths
