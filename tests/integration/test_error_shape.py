import pytest

from app.core.exceptions import AppError, NotFoundError
from tests.integration.test_auth_flow import auth_headers, login


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
    assert resp.json() == {
        "status": "error", "code": 404, "data": None,
        "meta": {"error_code": "not_found", "message": "repo not found"},
    }


async def test_unhandled_error_never_leaks_details(app_with_error_routes, client):
    resp = await client.get("/boom-unhandled")
    assert resp.status_code == 500
    assert resp.json()["meta"] == {"error_code": "internal_error", "message": "An internal error occurred"}
    assert "secret internal detail" not in resp.text


async def test_custom_error_code_and_status(app_with_error_routes, client):
    resp = await client.get("/boom-custom")
    assert resp.status_code == 429
    assert resp.json()["meta"]["error_code"] == "rate_limited"


async def test_validation_errors_use_the_envelope(client):
    access = await login(client)
    resp = await client.get("/work-items?page=0", headers=auth_headers(access))
    assert resp.status_code == 422
    assert resp.json() == {
        "status": "error", "code": 422, "data": None,
        "meta": {"error_code": "validation_error", "message": "Request validation failed"},
    }


async def test_404_route_uses_error_shape(client):
    resp = await client.get("/nope")
    assert resp.status_code == 404
    assert resp.json()["meta"]["error_code"] == "http_error"


async def test_request_id_header_present(client):
    resp = await client.get("/health")
    assert "x-request-id" in resp.headers


async def test_routes_are_documented_with_the_envelope(client):
    schema = (await client.get("/openapi.json")).json()
    me = schema["paths"]["/me"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert me["$ref"].endswith("ApiResponse_Me_")
