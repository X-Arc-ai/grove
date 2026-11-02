"""The API's settings. The public host name is configuration, never code."""

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from grove.config import ConfigError

# A local run or a test with no public host. Production has no default: the name is configuration.
LOCAL_BASE_URL = "http://127.0.0.1:8000"
# A.3's scopes, in the order the metadata lists them.
SCOPES = ("grove.read", "grove.write", "grove.sensitive_write")
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class ApiSettings:
    public_base_url: str
    build_sha: str | None = None
    google_oauth_file: str | None = None

    @property
    def secure_cookies(self) -> bool:
        """A loopback run has no TLS, and a browser drops a Secure cookie there."""
        return self.public_base_url.startswith("https://")

    @property
    def google_callback_url(self) -> str:
        return f"{self.public_base_url}/v1/auth/google/callback"

    @property
    def mcp_url(self) -> str:
        return f"{self.public_base_url}/mcp"

    @property
    def resource_metadata_url(self) -> str:
        return f"{self.public_base_url}/.well-known/oauth-protected-resource"


def parse_base_url(value: str | None) -> str:
    """An origin: https://host[:port] with no path, or http on a loopback host for a local run."""
    if not value:
        raise ConfigError("GROVE_PUBLIC_BASE_URL is not set")
    parts = urlsplit(value.strip())
    has_extra = parts.path not in ("", "/") or parts.query or parts.fragment
    has_user = parts.username is not None or parts.password is not None
    if parts.scheme not in ("https", "http") or not parts.hostname or has_extra or has_user:
        raise ConfigError("GROVE_PUBLIC_BASE_URL must be an origin, such as https://host")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        raise ConfigError("GROVE_PUBLIC_BASE_URL must use https outside a loopback host")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def parse_build_sha(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if _SHA.fullmatch(value) is None:
        raise ConfigError("GROVE_BUILD_SHA must be a full 40-character commit sha")
    return value


def load_api_settings() -> ApiSettings:
    base_url = os.environ.get("GROVE_PUBLIC_BASE_URL")
    if not base_url and os.environ.get("GROVE_ENV") != "production":
        base_url = LOCAL_BASE_URL
    return ApiSettings(
        public_base_url=parse_base_url(base_url),
        build_sha=parse_build_sha(os.environ.get("GROVE_BUILD_SHA")),
        google_oauth_file=os.environ.get("GROVE_GOOGLE_OAUTH_FILE") or None,
    )
