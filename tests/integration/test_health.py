async def test_health_endpoint(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert (body["status"], body["code"], body["meta"]) == ("success", 200, {})
    assert "env" in body["data"]
