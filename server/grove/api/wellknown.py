"""The protected-resource metadata (RFC 9728) and the 401 that points to it.

A placeholder until `hosts-a` builds the authorization server and `/mcp`: the metadata names the
resource and its scopes, and every call to `/mcp` gets 401 with the metadata URL, the answer an
unauthenticated client must get (A.3).
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from grove.api.config import SCOPES, ApiSettings

router = APIRouter()

# Clients fetch both paths: the bare one and the one with the resource path appended.
METADATA_PATHS = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
)


def protected_resource_metadata(settings: ApiSettings) -> dict:
    return {
        "resource": settings.mcp_url,
        "authorization_servers": [settings.public_base_url],
        "scopes_supported": list(SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "Grove",
    }


def metadata(request: Request) -> JSONResponse:
    return JSONResponse(protected_resource_metadata(request.app.state.settings))


for _path in METADATA_PATHS:
    router.add_api_route(_path, metadata, methods=["GET"], include_in_schema=False)


@router.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
def mcp(request: Request) -> JSONResponse:
    settings: ApiSettings = request.app.state.settings
    challenge = f'Bearer resource_metadata="{settings.resource_metadata_url}"'
    return JSONResponse(
        {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": challenge}
    )
