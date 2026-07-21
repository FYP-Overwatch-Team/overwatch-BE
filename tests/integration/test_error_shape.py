import pytest

from app.core.exceptions import AppError, NotFoundError


@pytest.fixture
def app_with_error_routes(app):
    @app.get("/boom-domain")
    async def boom_domain():
        raise NotFoundError("repo not found")

    @app.get("/boom-unhandled")
    async def boom_unhandled():
        raise ValueError("secret internal detail")

    @app.get("/boom-custom")
    async def boom_custom():
        raise AppError("rate limited", error_code="rate_limited", status_code=429)

    return app


async def test_domain_error_shape(app_with_error_routes, client):
    resp = await client.get("/boom-domain")
    assert resp.status_code == 404
    assert resp.json() == {"error_code": "not_found", "message": "repo not found"}


async def test_unhandled_error_never_leaks_details(app_with_error_routes, client):
    resp = await client.get("/boom-unhandled")
    assert resp.status_code == 500
    body = resp.json()
    assert body == {"error_code": "internal_error", "message": "An internal error occurred"}
    assert "secret internal detail" not in resp.text


async def test_custom_error_code_and_status(app_with_error_routes, client):
    resp = await client.get("/boom-custom")
    assert resp.status_code == 429
    assert resp.json()["error_code"] == "rate_limited"


async def test_404_route_uses_error_shape(client):
    resp = await client.get("/nope")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "http_error"


async def test_request_id_header_present(client):
    resp = await client.get("/health")
    assert "x-request-id" in resp.headers
