"""DB-free tests for the exchange-partner key: registration is conditional on the setting, and the
middleware confines the principal to ``GET /exchange/*``.

Uses a minimal FastAPI app with AuthMiddleware and the exchange router; the exchange service is mocked
so no Postgres is needed (same pattern as test_tenant_endpoints.py).
"""

# database_setup constructs a SQLAlchemy engine at import time from the
# POSTGRESQL_* env vars. These tests never touch the engine (the session
# dependency is overridden below), but the URL still has to parse.
import os

os.environ.setdefault("POSTGRESQL_USER", "test")
os.environ.setdefault("POSTGRESQL_PASSWORD", "test")
os.environ.setdefault("POSTGRESQL_HOST", "localhost")
os.environ.setdefault("POSTGRESQL_PORT", "5432")
os.environ.setdefault("POSTGRESQL_DB", "test")

from unittest import mock  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from lif.mdr_auth import core as auth_core  # noqa: E402
from lif.mdr_dto.exchange_dto import ExchangeCatalogDTO  # noqa: E402
from lif.mdr_restapi import exchange_endpoints  # noqa: E402
from lif.mdr_utils.config import Settings  # noqa: E402


PARTNER_KEY = "partner-read-only-key"
SERVICE_KEY = "changeme1"  # settings.mdr__auth__service_api_key__graphql default


class TestApiKeyRegistration:
    def test_partner_key_registered_only_when_configured(self):
        unset = auth_core._build_api_keys(Settings(mdr__auth__service_api_key__exchange_partner=None))
        empty = auth_core._build_api_keys(Settings(mdr__auth__service_api_key__exchange_partner=""))
        configured = auth_core._build_api_keys(Settings(mdr__auth__service_api_key__exchange_partner="k1"))

        assert auth_core.EXCHANGE_PARTNER_SERVICE_NAME not in unset.values()
        assert auth_core.EXCHANGE_PARTNER_SERVICE_NAME not in empty.values()
        assert "" not in empty and None not in empty
        assert configured["k1"] == auth_core.EXCHANGE_PARTNER_SERVICE_NAME
        assert configured[SERVICE_KEY] == "graphql-service"  # existing keys untouched


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(auth_core.AuthMiddleware)

    async def fake_session():
        yield mock.MagicMock()

    app.dependency_overrides[exchange_endpoints.get_session] = fake_session
    app.include_router(exchange_endpoints.router, prefix="/exchange")

    @app.get("/datamodels/")
    async def datamodels():
        return []

    return app


@pytest.fixture
def mock_catalog(monkeypatch):
    fake = mock.AsyncMock(return_value=ExchangeCatalogDTO(publisher="p", dataModels=[], transformationGroups=[]))
    monkeypatch.setattr(exchange_endpoints.exchange_service, "get_catalog", fake)
    return fake


@pytest.fixture
def mock_receive(monkeypatch):
    fake = mock.AsyncMock()
    monkeypatch.setattr(exchange_endpoints.exchange_service, "receive_bundle", fake)
    return fake


@pytest.fixture
async def client(mock_catalog, mock_receive):
    with mock.patch.dict(auth_core.API_KEYS, {PARTNER_KEY: auth_core.EXCHANGE_PARTNER_SERVICE_NAME}):
        async with AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://test") as c:
            yield c


class TestExchangePartnerPathRestriction:
    async def test_partner_key_can_read_exchange_catalog(self, client, mock_catalog):
        resp = await client.get("/exchange/catalog", headers={"X-API-Key": PARTNER_KEY})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"publisher": "p", "dataModels": [], "transformationGroups": []}
        mock_catalog.assert_awaited_once()

    async def test_partner_key_cannot_post_to_exchange(self, client, mock_receive):
        resp = await client.post("/exchange/receive", json={"manifest": {}}, headers={"X-API-Key": PARTNER_KEY})
        assert resp.status_code == 403
        assert resp.json() == {"detail": "Exchange partner keys are read-only and limited to /exchange"}
        mock_receive.assert_not_awaited()

    async def test_partner_key_cannot_read_outside_exchange(self, client):
        resp = await client.get("/datamodels/", headers={"X-API-Key": PARTNER_KEY})
        assert resp.status_code == 403
        assert resp.json() == {"detail": "Exchange partner keys are read-only and limited to /exchange"}

    async def test_regular_service_key_is_unrestricted(self, client, mock_receive):
        mock_receive.return_value = {"dataModels": [], "transformationGroup": None}
        resp = await client.get("/datamodels/", headers={"X-API-Key": SERVICE_KEY})
        assert resp.status_code == 200
        resp = await client.post("/exchange/receive", json={"manifest": {}}, headers={"X-API-Key": SERVICE_KEY})
        assert resp.status_code == 200, resp.text
        mock_receive.assert_awaited_once()
        # Defaults of the receive query params reach the service unchanged.
        kwargs = mock_receive.await_args.kwargs
        assert kwargs["data_model_type"] == "SourceSchema"
        assert kwargs["contributor_organization"] is None
        assert kwargs["allow_missing_paths"] is True

    async def test_no_key_is_401(self, client):
        resp = await client.get("/exchange/catalog")
        assert resp.status_code == 401
