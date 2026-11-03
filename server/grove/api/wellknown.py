"""The discovery documents: the protected-resource metadata of /mcp (RFC 9728) and the metadata of
the authorization server that issues its tokens (RFC 8414). docs/hosts.md.

Hosts read both from anywhere, a browser too, so both allow any origin. Every value comes from the
configured public base URL, never from the request. /mcp itself is grove.api.mcp, and its 401
points to the first path below. The client auth methods come through grove.oauth.registration, so
this module never imports the writer (tests/views/test_read_only.py).
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from grove.api.config import SCOPES, ApiSettings
from grove.api.oauth import AUTHORIZE_PATH, REGISTER_PATH, REVOKE_PATH, TOKEN_PATH
from grove.oauth.pkce import METHODS
from grove.oauth.registration import AUTH_METHODS

router = APIRouter()

# Clients fetch both paths: the bare one and the one with the resource path appended.
METADATA_PATHS = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
)
# The issuer has no path, so a client asks the bare paths; some append the resource path, and some
# ask the OpenID path first.
SERVER_METADATA_PATHS = (
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/mcp",
    "/.well-known/openid-configuration",
)
_HEADERS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}


def protected_resource_metadata(settings: ApiSettings) -> dict:
    return {
        "resource": settings.mcp_url,
        "authorization_servers": [settings.public_base_url],
        "scopes_supported": list(SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "Grove",
    }


def authorization_server_metadata(settings: ApiSettings) -> dict:
    base = settings.public_base_url
    return {
        "issuer": base,
        "authorization_endpoint": base + AUTHORIZE_PATH,
        "token_endpoint": base + TOKEN_PATH,
        "registration_endpoint": base + REGISTER_PATH,
        "revocation_endpoint": base + REVOKE_PATH,
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": list(METHODS),
        "token_endpoint_auth_methods_supported": list(AUTH_METHODS),
        "revocation_endpoint_auth_methods_supported": list(AUTH_METHODS),
        "scopes_supported": list(SCOPES),
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    }


def metadata(request: Request) -> JSONResponse:
    return JSONResponse(protected_resource_metadata(request.app.state.settings), headers=_HEADERS)


def server_metadata(request: Request) -> JSONResponse:
    body = authorization_server_metadata(request.app.state.settings)
    return JSONResponse(body, headers=_HEADERS)


for _path in METADATA_PATHS:
    router.add_api_route(_path, metadata, methods=["GET"], include_in_schema=False)
for _path in SERVER_METADATA_PATHS:
    router.add_api_route(_path, server_metadata, methods=["GET"], include_in_schema=False)
