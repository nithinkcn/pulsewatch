"""API tests against a real database."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

VALID = {
    "name": "hospital-a-camera-1",
    "address": "https://example.com/health",
    "check_type": "http",
    "group": "hospital-a",
    "interval_seconds": 60,
    "timeout_seconds": 5,
    "failure_threshold": 3,
    "recovery_threshold": 2,
}


async def create(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/targets", json={**VALID, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


class TestCreate:
    async def test_new_target_starts_unknown_and_unchecked(self, client: AsyncClient) -> None:
        body = await create(client, name="fresh")

        # A target that has never been probed must not claim to be healthy.
        assert body["status"] == "unknown"
        assert body["last_checked_at"] is None
        assert body["consecutive_failures"] == 0

    async def test_defaults_are_applied(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/targets", json={"name": "minimal", "address": "https://example.com"}
        )

        assert response.status_code == 201
        body = response.json()
        assert body["check_type"] == "http"
        assert body["interval_seconds"] == 60
        assert body["enabled"] is True


class TestValidation:
    async def test_timeout_may_not_exceed_the_interval(self, client: AsyncClient) -> None:
        # Would queue faster than it drains.
        response = await client.post(
            "/api/v1/targets", json={**VALID, "interval_seconds": 10, "timeout_seconds": 30}
        )

        assert response.status_code == 422
        assert "timeout_seconds must be less than interval_seconds" in response.text

    async def test_http_target_requires_a_url_scheme(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/targets", json={**VALID, "address": "example.com"})

        assert response.status_code == 422
        assert "http:// or https://" in response.text

    async def test_tcp_target_requires_host_and_port(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/targets", json={**VALID, "check_type": "tcp", "address": "camera.local"}
        )

        assert response.status_code == 422
        assert "host:port" in response.text

    async def test_tcp_target_with_host_and_port_is_accepted(self, client: AsyncClient) -> None:
        body = await create(client, check_type="tcp", address="camera.local:554")

        assert body["check_type"] == "tcp"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("interval_seconds", 1),  # below the 5s floor
            ("failure_threshold", 0),
            ("recovery_threshold", 0),
            ("timeout_seconds", 0),
            ("expected_status", 99),
        ],
    )
    async def test_out_of_range_values_are_rejected(
        self, client: AsyncClient, field: str, value: int
    ) -> None:
        response = await client.post("/api/v1/targets", json={**VALID, field: value})

        assert response.status_code == 422


class TestReadAndFilter:
    async def test_filter_by_group(self, client: AsyncClient) -> None:
        await create(client, name="a", group="hospital-a")
        await create(client, name="b", group="hospital-b")

        response = await client.get("/api/v1/targets", params={"group": "hospital-a"})

        assert [t["name"] for t in response.json()] == ["a"]

    async def test_filter_by_status(self, client: AsyncClient) -> None:
        await create(client, name="a")

        response = await client.get("/api/v1/targets", params={"status": "unknown"})
        assert len(response.json()) == 1

        response = await client.get("/api/v1/targets", params={"status": "down"})
        assert response.json() == []

    async def test_unknown_id_is_404(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/targets/00000000-0000-0000-0000-000000000000")

        assert response.status_code == 404


class TestUpdate:
    async def test_partial_update_leaves_other_fields_alone(self, client: AsyncClient) -> None:
        target = await create(client, name="before", group="hospital-a")

        response = await client.patch(f"/api/v1/targets/{target['id']}", json={"name": "after"})

        assert response.status_code == 200
        assert response.json()["name"] == "after"
        assert response.json()["group"] == "hospital-a"

    async def test_group_can_be_explicitly_cleared(self, client: AsyncClient) -> None:
        # An explicit null must be distinguishable from an omitted field.
        target = await create(client, group="hospital-a")

        response = await client.patch(f"/api/v1/targets/{target['id']}", json={"group": None})

        assert response.json()["group"] is None

    async def test_update_cannot_create_an_invalid_timeout_interval_pair(
        self, client: AsyncClient
    ) -> None:
        target = await create(client, interval_seconds=60, timeout_seconds=5)

        response = await client.patch(
            f"/api/v1/targets/{target['id']}", json={"interval_seconds": 5}
        )

        assert response.status_code == 422

    async def test_unknown_field_is_rejected(self, client: AsyncClient) -> None:
        target = await create(client)

        response = await client.patch(f"/api/v1/targets/{target['id']}", json={"nope": 1})

        assert response.status_code == 422


class TestDelete:
    async def test_delete_removes_the_target(self, client: AsyncClient) -> None:
        target = await create(client)

        assert (await client.delete(f"/api/v1/targets/{target['id']}")).status_code == 204
        assert (await client.get(f"/api/v1/targets/{target['id']}")).status_code == 404

    async def test_deleting_twice_is_404_not_500(self, client: AsyncClient) -> None:
        target = await create(client)
        await client.delete(f"/api/v1/targets/{target['id']}")

        assert (await client.delete(f"/api/v1/targets/{target['id']}")).status_code == 404


class TestHealthEndpoints:
    async def test_livez_does_not_touch_dependencies(self, client: AsyncClient) -> None:
        response = await client.get("/livez")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_readyz_reports_the_database_as_reachable(self, client: AsyncClient) -> None:
        response = await client.get("/readyz")

        # Redis is not running in this fixture, so readiness is False overall —
        # but the database check must pass, and the endpoint must degrade with
        # a 503 rather than raise.
        assert response.status_code in (200, 503)
        assert response.json()["checks"]["database"] is True
