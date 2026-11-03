"""The authorization server metadata (RFC 8414) at its three paths, beside the protected-resource
metadata (RFC 9728); both from configuration only, for any origin, and never cached."""

import pytest
from fastapi.testclient import TestClient

from grove.api import wellknown
from grove.api.app import create_app
from grove.api.config import ApiSettings

SERVER_PATHS = (
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/mcp",
    "/.well-known/openid-configuration",
)
RESOURCE_PATHS = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
)


def _client(base: str) -> TestClient:
    # Metadata reads no store: the app gets no driver, and no request opens one.
    return TestClient(create_app(ApiSettings(public_base_url=base), driver=object()))


@pytest.mark.parametrize("path", SERVER_PATHS)
def test_the_server_metadata_names_every_endpoint_and_rule(path):
    response = _client("https://grove.example").get(path)
    assert response.status_code == 200
    methods = ["none", "client_secret_post", "client_secret_basic"]
    assert response.json() == {
        "issuer": "https://grove.example",
        "authorization_endpoint": "https://grove.example/authorize",
        "token_endpoint": "https://grove.example/token",
        "registration_endpoint": "https://grove.example/register",
        "revocation_endpoint": "https://grove.example/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": methods,
        "revocation_endpoint_auth_methods_supported": methods,
        "scopes_supported": ["grove.read", "grove.write", "grove.sensitive_write"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    }
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", RESOURCE_PATHS)
def test_the_resource_metadata_is_for_any_origin_and_never_cached(path):
    response = _client("https://grove.example").get(path)
    assert response.json()["resource"] == "https://grove.example/mcp"
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cache-control"] == "no-store"


def test_the_server_metadata_follows_the_configured_host_only():
    for base in ("https://one.example", "https://two.example:8443"):
        client = _client(base)
        body = client.get("/.well-known/oauth-authorization-server", headers={"Host": "x.test"})
        assert body.json()["issuer"] == base
        assert body.json()["token_endpoint"] == f"{base}/token"
        resource = client.get("/.well-known/oauth-protected-resource").json()
        assert resource["authorization_servers"] == [body.json()["issuer"]]


def test_the_placeholder_mcp_route_is_gone_from_the_discovery_router():
    paths = {route.path for route in wellknown.router.routes}
    assert "/mcp" not in paths
    assert set(RESOURCE_PATHS) | set(SERVER_PATHS) == paths
